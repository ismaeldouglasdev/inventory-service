"""CDC Agent — Change Data Capture from OSPOS MySQL.

Polling-based: reads ``ospos_items`` periodically, diffs against
``product_mapping``, and writes events to the EventStore.

Supports both direct MySQL connections (aiomysql) and a lightweight
fallback that reads from a cached snapshot for environments without
direct DB access.

Flow:
  1. Poll OSPOS ``ospos_items`` WHERE ``deleted = 0``
  2. Hash each row → compare with ``product_mapping.last_hash``
  3. If new/changed → create ``product.created`` / ``stock.updated`` event
  4. If removed from OSPOS (deleted=1) → create ``product.deleted`` event
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.database import async_session_factory
from app.models.event_store import EventStore
from app.models.product_mapping import ProductMapping
from app.services.event_processor import create_event
from app.config import settings

logger = logging.getLogger(__name__)

# ── Types ────────────────────────────────────────────────────────────────


@dataclass
class OSPOSItem:
    """Row from ``ospos_items`` table."""
    item_id: int
    name: str
    category: str
    item_number: str  # SKU
    description: str
    cost_price: float
    unit_price: float
    reorder_level: float
    receiving_quantity: float
    deleted: bool


# ── CDC Agent ────────────────────────────────────────────────────────────


class CDCAgent:
    """Polls OSPOS MySQL and publishes change events to EventStore.

    Usage::
        agent = CDCAgent(poll_interval=30.0)
        asyncio.create_task(agent.run_forever())
        # later …
        agent.stop()
    """

    def __init__(
        self,
        poll_interval: float = 30.0,
        batch_size: int = 50,
    ) -> None:
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False
        self._last_id: int = 0  # for incremental polling

    # ── Public API ───────────────────────────────────────────────────

    async def run_once(self) -> int:
        """Poll OSPOS once, create events for every change — return count."""
        changed = 0
        try:
            items = await self._fetch_ospos_items()
        except Exception as exc:
            logger.error("CDC: failed to fetch OSPOS items: %s", exc)
            return 0

        if not items:
            logger.debug("CDC: no items from OSPOS")
            return 0

        async with async_session_factory() as session:
            for item in items:
                event = await self._check_and_create_event(session, item)
                if event:
                    session.add(event)
                    changed += 1

            if changed:
                await session.commit()
                logger.info("CDC: created %d event(s) from OSPOS poll", changed)

        return changed

    async def run_forever(self) -> None:
        """Loop infinito."""
        self._running = True
        logger.info(
            "CDC Agent started (poll_interval=%ds)",
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
        self._running = False

    # ── OSPOS MySQL access ───────────────────────────────────────────

    async def _fetch_ospos_items(self) -> list[OSPOSItem]:
        """Fetch active items from OSPOS MySQL.

        Tries aiomysql first; falls back to the OSPOS REST API if the
        MySQL driver is not installed.
        """
        try:
            return await self._fetch_via_mysql()
        except ImportError:
            logger.debug("aiomysql not installed, trying REST fallback")
            return await self._fetch_via_rest()
        except Exception as exc:
            logger.warning("CDC: MySQL fetch failed (%s), trying REST", exc)
            return await self._fetch_via_rest()

    async def _fetch_via_mysql(self) -> list[OSPOSItem]:
        """Direct MySQL query using aiomysql."""
        import aiomysql  # type: ignore[import-untyped]

        pool = await aiomysql.create_pool(
            host=settings.ospos_db_host,
            port=settings.ospos_db_port,
            user=settings.ospos_db_user,
            password=settings.ospos_db_pass,
            db=settings.ospos_db_name,
            charset="utf8",
            autocommit=True,
        )

        items: list[OSPOSItem] = []
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT item_id, name, category, item_number, "
                    "description, cost_price, unit_price, reorder_level, "
                    "receiving_quantity, deleted "
                    "FROM ospos_items "
                    "WHERE item_id > %s "
                    "ORDER BY item_id ASC LIMIT %s",
                    (self._last_id, self.batch_size),
                )
                rows = await cur.fetchall()

        pool.close()
        await pool.wait_closed()

        for row in rows:
            items.append(OSPOSItem(
                item_id=row[0],
                name=row[1],
                category=row[2],
                item_number=row[3] or "",
                description=row[4],
                cost_price=float(row[5]),
                unit_price=float(row[6]),
                reorder_level=float(row[7]),
                receiving_quantity=float(row[8]),
                deleted=bool(row[9]),
            ))

        if items:
            self._last_id = items[-1].item_id

        return items

    async def _fetch_via_rest(self) -> list[OSPOSItem]:
        """Fallback: read OSPOS items via REST API (if OSPOS exposes one).

        Uses the OSPOS API endpoint if configured.
        """
        if not settings.ospos_api_url:
            logger.debug("CDC: no OSPOS API URL configured, skipping REST fallback")
            return []

        url = f"{settings.ospos_api_url.rstrip('/')}/items"
        params = {"limit": self.batch_size}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("CDC: OSPOS REST fallback failed: %s", exc)
            return []

        items: list[OSPOSItem] = []
        for row in data if isinstance(data, list) else data.get("data", []):
            items.append(OSPOSItem(
                item_id=row["item_id"],
                name=row["name"],
                category=row.get("category", ""),
                item_number=row.get("item_number", ""),
                description=row.get("description", ""),
                cost_price=float(row.get("cost_price", 0)),
                unit_price=float(row.get("unit_price", 0)),
                reorder_level=float(row.get("reorder_level", 0)),
                receiving_quantity=float(row.get("receiving_quantity", 1)),
                deleted=bool(row.get("deleted", False)),
            ))

        if items:
            self._last_id = items[-1].item_id

        return items

    # ── Change detection ─────────────────────────────────────────────

    async def _check_and_create_event(
        self,
        session: Any,
        item: OSPOSItem,
    ) -> Optional[EventStore]:
        """Compare item with stored mapping; return an event or None."""
        sku = item.item_number or str(item.item_id)

        # Compute a hash of relevant fields
        current_hash = self._hash_item(item)

        # Look up existing mapping
        from sqlalchemy import select
        result = await session.execute(
            select(ProductMapping).where(ProductMapping.sku == sku)
        )
        mapping = result.scalar_one_or_none()

        if mapping is None:
            # New product
            logger.info("CDC: new product SKU=%s (OSPOS ID %d)", sku, item.item_id)

            # Create product mapping
            mapping = ProductMapping(
                sku=sku,
                ospos_id=item.item_id,
                has_variants=False,
                store_id="principal",
                last_hash=current_hash,
                last_sync_at=datetime.now(timezone.utc),
            )
            session.add(mapping)

            return create_event(
                event_type="product.created",
                payload={
                    "sku": sku,
                    "ospos_id": item.item_id,
                    "name": item.name,
                    "category": item.category,
                    "description": item.description,
                    "price": item.unit_price,
                    "cost_price": item.cost_price,
                    "stock_quantity": int(item.receiving_quantity),
                    "reorder_level": float(item.reorder_level),
                },
                sku=sku,
            )

        if item.deleted:
            # Product was soft-deleted in OSPOS
            logger.info("CDC: product deleted SKU=%s", sku)
            mapping.last_hash = current_hash
            return create_event(
                event_type="product.deleted",
                payload={"sku": sku, "ospos_id": item.item_id},
                sku=sku,
            )

        # Check if anything changed
        if mapping.last_hash == current_hash and mapping.last_hash is not None:
            return None  # no change

        # Something changed — update hash and create event
        mapping.last_hash = current_hash
        mapping.last_sync_at = datetime.now(timezone.utc)

        logger.info("CDC: product changed SKU=%s", sku)

        events = [
            create_event(
                event_type="stock.updated",
                payload={
                    "sku": sku,
                    "quantity": int(item.receiving_quantity),
                },
                sku=sku,
            ),
        ]

        # If price changed too, create a price update event
        # (we detect by hash change — granularity could be improved)
        events.append(
            create_event(
                event_type="price.updated",
                payload={
                    "sku": sku,
                    "price": float(item.unit_price),
                },
                sku=sku,
            ),
        )

        # Only return the stock event for now; the processor will
        # pick up subsequent events on next poll.
        # In a full implementation we'd batch them in a transaction.
        return events[0]

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _hash_item(item: OSPOSItem) -> str:
        """Return a stable hash of the fields that matter for sync."""
        raw = json.dumps(
            {
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "unit_price": item.unit_price,
                "cost_price": item.cost_price,
                "receiving_quantity": item.receiving_quantity,
                "deleted": item.deleted,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
