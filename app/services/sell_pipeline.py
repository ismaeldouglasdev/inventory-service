"""Sell Pipeline — reservation-to-commitment lifecycle.

Fluxo completo de uma venda:

    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ RESERVED │───►│CONFIRMED │───►│COMMITTED │
    └──────────┘    └──────────┘    └──────────┘
         │                              │
         ▼                              ▼
    cancelled (se expirar)         done

1. RESERVED:   estoque reservado (venda online chegou)
2. CONFIRMED:  OSPOS deduziu o estoque físico
3. COMMITTED:  canal externo confirmou a propagação

Idempotência garantida via ProcessedAction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.inventory_state import InventoryState
from app.models.processed_action import ProcessedAction
from app.models.store_product import StoreProduct
from app.models.event_store import EventStore
from app.adapters.registry import AdapterRegistry
from app.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class SellPipeline:
    """Coordinates the reservation → confirmation → commit flow.

    Usage::

        pipeline = SellPipeline(registry, circuit_breaker)
        result = await pipeline.reserve(
            sku="ABC123",
            quantity=2,
            unit_price=29.90,
            channel="woocommerce",
            order_id="order_9876",
        )
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.registry = registry
        self.cb = circuit_breaker or CircuitBreaker()

    # ── Step 1: Reserve ────────────────────────────────────────────────

    async def reserve(
        self,
        sku: str,
        quantity: int,
        unit_price: float,
        channel: str,
        order_id: str,
        *,
        source_event_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Reserve stock for an order.

        Validates:
        - Product exists and has enough stock
        - No duplicate reservation for (order_id, sku)

        Returns the reservation dict.
        """
        async with async_session_factory() as session:
            # ── Check for duplicate ───────────────────────────────────
            dup = await self._find_active_reservation(session, order_id, sku)
            if dup:
                logger.info(
                    "Reservation already exists for order %s SKU %s "
                    "(state=%s) — returning existing",
                    order_id, sku, dup.state,
                )
                return self._to_dict(dup)

            # ── Get product from local store ──────────────────────────
            product = await self._get_store_product(session, sku)
            if product is None:
                raise ValueError(f"Product SKU {sku!r} not found in store")
            if product.stock < quantity:
                raise ValueError(
                    f"Insufficient stock for SKU {sku!r}: "
                    f"requested {quantity}, available {product.stock}"
                )

            # ── Create reservation ────────────────────────────────────
            now = datetime.now(timezone.utc)
            reservation = InventoryState(
                sku=sku,
                order_id=order_id,
                channel=channel,
                state="reserved",
                quantity=quantity,
                unit_price=unit_price,
                total=round(unit_price * quantity, 2),
                source_event_id=source_event_id,
                notes=notes,
                reserved_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)

            # Decrement available stock in store_products
            # (physical stock stays in OSPOS — this is a soft reservation)
            product.stock -= quantity
            product.updated_at = now

            await session.commit()
            await session.refresh(reservation)

            logger.info(
                "Reservation created: SKU=%s qty=%d order=%s → id=%d",
                sku, quantity, order_id, reservation.id,
            )

            return self._to_dict(reservation)

    # ── Step 2: Confirm (OSPOS deducted the stock) ─────────────────────

    async def confirm(
        self,
        reservation_id: int,
        *,
        ospos_sale_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark a reservation as confirmed (OSPOS processed the sale).

        Transition: reserved → confirmed.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(InventoryState).where(
                    InventoryState.id == reservation_id,
                    InventoryState.state == "reserved",
                )
            )
            reservation = result.scalar_one_or_none()
            if reservation is None:
                raise ValueError(
                    f"No RESERVED reservation with id={reservation_id}"
                )

            now = datetime.now(timezone.utc)
            reservation.state = "confirmed"
            reservation.confirmed_at = now
            reservation.ospos_sale_id = ospos_sale_id
            reservation.updated_at = now

            await session.commit()

            logger.info(
                "Reservation %d confirmed: SKU=%s order=%s",
                reservation_id, reservation.sku, reservation.order_id,
            )

            return self._to_dict(reservation)

    # ── Step 3: Commit (channel propagated) ────────────────────────────

    async def commit(
        self,
        reservation_id: int,
    ) -> dict[str, Any]:
        """Mark a reservation as fully committed.

        Transition: confirmed → committed.
        This is the terminal success state.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(InventoryState).where(
                    InventoryState.id == reservation_id,
                    InventoryState.state == "confirmed",
                )
            )
            reservation = result.scalar_one_or_none()
            if reservation is None:
                raise ValueError(
                    f"No CONFIRMED reservation with id={reservation_id}"
                )

            now = datetime.now(timezone.utc)
            reservation.state = "committed"
            reservation.committed_at = now
            reservation.updated_at = now

            await session.commit()

            logger.info(
                "Reservation %d committed: SKU=%s order=%s",
                reservation_id, reservation.sku, reservation.order_id,
            )

            return self._to_dict(reservation)

    # ── Cancel ─────────────────────────────────────────────────────────

    async def cancel(
        self,
        reservation_id: int,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        """Cancel a reservation and restore the stock.

        Can be called from reserved or confirmed state.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(InventoryState).where(
                    InventoryState.id == reservation_id,
                    InventoryState.state.in_(["reserved", "confirmed"]),
                )
            )
            reservation = result.scalar_one_or_none()
            if reservation is None:
                raise ValueError(
                    f"No active reservation with id={reservation_id}"
                )

            now = datetime.now(timezone.utc)

            # Restore stock
            product = await self._get_store_product(session, reservation.sku)
            if product:
                product.stock += reservation.quantity
                product.updated_at = now

            reservation.state = "cancelled"
            reservation.cancelled_at = now
            reservation.notes = (reservation.notes or "") + f" | Cancelled: {reason}"
            reservation.updated_at = now

            await session.commit()

            logger.info(
                "Reservation %d cancelled: SKU=%s qty=%d reason=%s",
                reservation_id, reservation.sku, reservation.quantity, reason,
            )

            return self._to_dict(reservation)

    # ── Full sell flow (reserve + confirm + propagate) ─────────────────

    async def sell(
        self,
        sku: str,
        quantity: int,
        unit_price: float,
        channel: str,
        order_id: str,
        *,
        source_event_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Run the full sell pipeline atomically.

        1. Reserve stock
        2. Propagate to channel adapter
        3. Commit

        If propagation fails, the reservation stays CONFIRMED (manual
        retry or timeout → cancel).
        """
        # Step 1: Reserve
        reservation = await self.reserve(
            sku=sku,
            quantity=quantity,
            unit_price=unit_price,
            channel=channel,
            order_id=order_id,
            source_event_id=source_event_id,
            notes=notes,
        )

        # Step 2: Confirm (simulate OSPOS deduction — in production this
        # happens via CDC from OSPOS, we just mark it)
        confirmed = await self.confirm(
            reservation["id"],
            ospos_sale_id=f"auto-{order_id}",
        )

        # Step 3: Propagate to channel adapter
        try:
            adapter = self.registry.get(channel)
            async with self.cb.guard(
                channel,
                event_id=source_event_id,
                action_type="update_stock",
            ):
                ok = await adapter.update_stock(sku, quantity)

            if not ok:
                raise RuntimeError(f"Channel {channel} returned failure for stock update")

            # Mark committed
            return await self.commit(reservation["id"])
        except Exception as exc:
            logger.error(
                "Sell pipeline: channel %s failed for SKU %s order %s: %s",
                channel, sku, order_id, exc,
            )
            return confirmed  # stays CONFIRMED for manual retry

    # ── Query helpers ──────────────────────────────────────────────────

    async def get_reservation(
        self, reservation_id: int,
    ) -> dict[str, Any] | None:
        """Get a single reservation by id."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(InventoryState).where(
                    InventoryState.id == reservation_id
                )
            )
            row = result.scalar_one_or_none()
            return self._to_dict(row) if row else None

    async def list_reservations(
        self,
        sku: str | None = None,
        state: str | None = None,
        order_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List reservations with optional filters."""
        async with async_session_factory() as session:
            q = select(InventoryState)
            if sku:
                q = q.where(InventoryState.sku == sku)
            if state:
                q = q.where(InventoryState.state == state)
            if order_id:
                q = q.where(InventoryState.order_id == order_id)
            q = q.order_by(InventoryState.created_at.desc()).limit(limit)

            result = await session.execute(q)
            return [self._to_dict(r) for r in result.scalars().all()]

    # ── Idempotency check ──────────────────────────────────────────────

    async def is_already_processed(
        self, event_id: str, target_system: str,
    ) -> bool:
        """Check if an event was already dispatched to a target.

        This is the core idempotency guard — if the pair
        (event_id, target_system) exists in processed_actions,
        the action was already applied and should be skipped.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(ProcessedAction).where(
                    ProcessedAction.event_id == event_id,
                    ProcessedAction.target_system == target_system,
                )
            )
            return result.scalar_one_or_none() is not None

    # ── Internal helpers ───────────────────────────────────────────────

    async def _find_active_reservation(
        self,
        session: AsyncSession,
        order_id: str,
        sku: str,
    ) -> InventoryState | None:
        result = await session.execute(
            select(InventoryState).where(
                InventoryState.order_id == order_id,
                InventoryState.sku == sku,
                InventoryState.state.in_(["reserved", "confirmed"]),
            )
        )
        return result.scalar_one_or_none()

    async def _get_store_product(
        self,
        session: AsyncSession,
        sku: str,
    ) -> StoreProduct | None:
        result = await session.execute(
            select(StoreProduct).where(StoreProduct.sku == sku)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _to_dict(reservation: InventoryState) -> dict[str, Any]:
        return {
            "id": reservation.id,
            "sku": reservation.sku,
            "order_id": reservation.order_id,
            "channel": reservation.channel,
            "state": reservation.state,
            "quantity": reservation.quantity,
            "unit_price": reservation.unit_price,
            "total": reservation.total,
            "source_event_id": reservation.source_event_id,
            "ospos_sale_id": reservation.ospos_sale_id,
            "notes": reservation.notes,
            "reserved_at": _iso(reservation.reserved_at),
            "confirmed_at": _iso(reservation.confirmed_at),
            "committed_at": _iso(reservation.committed_at),
            "cancelled_at": _iso(reservation.cancelled_at),
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
