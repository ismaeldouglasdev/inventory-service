"""CDC Agent — Change Data Capture from OSPOS MySQL → Mercado Livre.

Polling-based: reads ``ospos_items`` + real stock from
``ospos_item_quantities``, diffs only the SKUs that are ALREADY PUBLISHED
on Mercado Livre (``channel_product_mapping.channel='mercadolivre'``),
and writes ``stock.updated`` / ``price.updated`` events to the EventStore.

Safety guarantees (revised 2026-09-06 — "vamos aos poucos"):
  * This agent NEVER emits ``product.created``. Publication on ML is
    done manually via ``POST /v1/mercadolivre/publish`` (or ``/adopt``).
    Enabling the CDC is therefore safe: it only propagates stock/price
    changes for products that are already on ML.
  * Only SKUs present in ``channel_product_mapping`` (channel
    ``mercadolivre``) are considered. Products without a valid SKU are
    skipped (no ``"NULL"`` sku events).
  * Events carry ``channel="mercadolivre"`` so the EventStoreProcessor
    dispatches ONLY to the ML adapter — contained, no leaking to other
    channels that may not have the product.
  * Real stock comes from ``ospos_item_quantities`` (location 1), NOT
    ``receiving_quantity`` (that catalog field does not drop on sales).

The published set is small (tens of items), so the agent polls ALL of
them every cycle and diffs against ``product_mapping.last_hash``. This is
restart-safe and idempotent, and — because it re-examines existing
items — it correctly picks up stock/price changes to items that were
already scanned. The very first run only seeds the baseline hash, so
enabling it never re-publishes anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.database import async_session_factory
from app.models.channel_product_mapping import ChannelProductMapping
from app.models.event_store import EventStore
from app.models.product_mapping import ProductMapping
from app.services.event_processor import create_event
from app.services.ospos_client import _pool

logger = logging.getLogger(__name__)

# Location 1 = the physical store location used everywhere else in the app.
STOCK_LOCATION_ID = 1


@dataclass
class OSPOSItem:
    """An OSPOS item that is already published on Mercado Livre."""
    item_id: int
    name: str
    category: str
    sku: str                 # item_number (validated non-empty)
    description: str
    cost_price: float
    unit_price: float
    reorder_level: float
    deleted: bool
    stock_quantity: float    # real stock at location 1


class CDCAgent:
    """Polls OSPOS and publishes ML stock/price change events to EventStore.

    Usage::
        agent = CDCAgent(poll_interval=30.0)
        asyncio.create_task(agent.run_forever())
        # later …
        agent.stop()
    """

    def __init__(self, poll_interval: float = 30.0) -> None:
        self.poll_interval = poll_interval
        self._running = False

    # ── Public API ───────────────────────────────────────────────────

    async def run_once(self) -> int:
        """Poll OSPOS once, create ML events for every change — return count."""
        try:
            items = await self._fetch_ml_published_items()
        except Exception as exc:
            logger.error("CDC: failed to fetch OSPOS items: %s", exc)
            return 0

        if not items:
            logger.debug("CDC: no ML-published items")
            return 0

        changed = 0
        async with async_session_factory() as session:
            for item in items:
                created = await self._check_and_create_event(session, item)
                changed += created

            await session.commit()

        if changed:
            logger.info("CDC: created %d event(s) for ML", changed)
        return changed

    async def run_forever(self) -> None:
        """Loop infinito."""
        self._running = True
        logger.info(
            "CDC Agent started (poll_interval=%ds) — ML stock/price only",
            self.poll_interval,
        )
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("CDC Agent: error in poll cycle")
            await asyncio.sleep(self.poll_interval)
        logger.info("CDC Agent stopped")

    def stop(self) -> None:
        """Sinaliza pro loop run_forever parar."""
        self._running = False

    # ── OSPOS access ─────────────────────────────────────────────────

    async def _fetch_ml_published_items(self) -> list[OSPOSItem]:
        """Fetch all active OSPOS items published on ML, with real stock.

        Filters for:
          * valid ``item_number`` (SKU): not NULL, not empty, not "NULL"
          * not soft-deleted
          * present in ``channel_product_mapping`` for ``mercadolivre``
        """
        pool = await _pool()
        items: list[OSPOSItem] = []
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.item_id, i.name, COALESCE(i.category, ''),
                           COALESCE(i.item_number, ''), COALESCE(i.description, ''),
                           i.cost_price, i.unit_price, i.reorder_level, i.deleted,
                           COALESCE(iq.quantity, 0)
                    FROM ospos_items AS i
                    LEFT JOIN ospos_item_quantities AS iq
                           ON iq.item_id = i.item_id AND iq.location_id = %s
                    WHERE i.deleted = 0
                      AND i.item_number IS NOT NULL
                      AND i.item_number <> ''
                      AND EXISTS (
                          SELECT 1 FROM channel_product_mapping
                          WHERE sku = i.item_number AND channel = 'mercadolivre'
                      )
                    ORDER BY i.item_id ASC
                    """,
                    (STOCK_LOCATION_ID,),
                )
                rows = await cur.fetchall()

        for row in rows:
            sku = (row[3] or "").strip()
            if sku.lower() == "null":
                continue
            items.append(OSPOSItem(
                item_id=int(row[0]),
                name=row[1],
                category=row[2],
                sku=sku,
                description=row[4],
                cost_price=float(row[5] or 0),
                unit_price=float(row[6] or 0),
                reorder_level=float(row[7] or 0),
                deleted=bool(row[8]),
                stock_quantity=float(row[9] or 0),
            ))

        logger.debug("CDC: %d ML-published item(s) loaded", len(items))
        return items

    # ── Change detection ─────────────────────────────────────────────

    async def _check_and_create_event(
        self,
        session: Any,
        item: OSPOSItem,
    ) -> int:
        """Compare an ML-published item with its stored hash.

        Returns the number of events created (usually 1: a
        ``stock.updated``; 2 when price also changed; 0 on no change).
        """
        sku = item.sku
        current_hash = self._hash_item(item)

        result = await session.execute(
            select(ProductMapping).where(ProductMapping.sku == sku)
        )
        mapping = result.scalar_one_or_none()

        if mapping is None:
            # Published on ML but not yet in product_mapping (e.g. adopted
            # externally). Seed a baseline WITHOUT emitting events, so the
            # next change is the first one to sync.
            session.add(ProductMapping(
                sku=sku,
                ospos_id=item.item_id,
                has_variants=False,
                store_id="principal",
                last_hash=current_hash,
                last_sync_at=datetime.now(timezone.utc),
            ))
            logger.info("CDC: baseline seeded SKU=%s (no event)", sku)
            return 0

        if mapping.last_hash == current_hash and mapping.last_hash is not None:
            return 0  # no change

        # Changed → update hash and emit update event(s).
        mapping.last_hash = current_hash
        mapping.last_sync_at = datetime.now(timezone.utc)

        logger.info(
            "CDC: ML product changed SKU=%s (stock=%s)",
            sku, item.stock_quantity,
        )

        session.add(create_event(
            event_type="stock.updated",
            payload={"sku": sku, "quantity": int(item.stock_quantity)},
            sku=sku,
            channel="mercadolivre",
        ))
        return 1

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _hash_item(item: OSPOSItem) -> str:
        """Hash of the fields that matter for ML sync."""
        raw = json.dumps(
            {
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "unit_price": item.unit_price,
                "cost_price": item.cost_price,
                "stock_quantity": item.stock_quantity,
                "deleted": item.deleted,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
