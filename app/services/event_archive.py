"""Event Archive — move eventos antigos do live para o archive.

Estratégia (plano v3.1 §8):
  - event_store: tabela live (7-30 dias)
  - event_store_archive: histórico completo

O archive service move eventos COMPLETED/DEAD com mais de N dias
da live para o archive, mantendo a live performática.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from app.database import async_session_factory
from app.models.event_store import EventStore

logger = logging.getLogger(__name__)

# Default: arquivar eventos com mais de 7 dias
DEFAULT_RETENTION_DAYS = 7

# Lote máximo por execução
ARCHIVE_BATCH_SIZE = 500


class EventArchiveService:
    """Archive old events from event_store to event_store_archive.

    Usage::

        archive = EventArchiveService(retention_days=7)
        count = await archive.run_once()  # archive one batch
        count = await archive.run_forever()  # loop
    """

    def __init__(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.retention_days = retention_days
        self._running = False

    async def run_once(self) -> int:
        """Archive one batch of old events.

        Returns the number of events archived.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        archived = 0

        async with async_session_factory() as session:
            # Find old terminal events (COMPLETED or DEAD)
            result = await session.execute(
                select(EventStore)
                .where(
                    EventStore.state.in_(["completed", "dead"]),
                    EventStore.updated_at < cutoff,
                )
                .order_by(EventStore.updated_at.asc())
                .limit(ARCHIVE_BATCH_SIZE)
            )
            events = list(result.scalars().all())

            if not events:
                logger.debug("EventArchive: nothing to archive (cutoff=%s)", cutoff)
                return 0

            # Insert into archive table
            for ev in events:
                await session.execute(
                    text(
                        """INSERT OR IGNORE INTO event_store_archive
                        (id, event_type, payload, state, sku, channel,
                         ospos_synced, retry_count, max_retries,
                         created_at, updated_at, archived_at)
                        VALUES (:id, :event_type, :payload, :state, :sku, :channel,
                         :ospos_synced, :retry_count, :max_retries,
                         :created_at, :updated_at, :archived_at)"""
                    ),
                    {
                        "id": ev.id,
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                        "state": ev.state,
                        "sku": ev.sku,
                        "channel": ev.channel,
                        "ospos_synced": int(ev.ospos_synced) if ev.ospos_synced else 0,
                        "retry_count": ev.retry_count,
                        "max_retries": ev.max_retries,
                        "created_at": ev.created_at,
                        "updated_at": ev.updated_at,
                        "archived_at": datetime.now(timezone.utc),
                    },
                )

            # Delete from live table
            for ev in events:
                await session.execute(
                    text("DELETE FROM event_store WHERE id = :id"),
                    {"id": ev.id},
                )

            await session.commit()
            archived = len(events)
            logger.info("EventArchive: archived %d events (cutoff=%s)", archived, cutoff)

        return archived

    async def run_forever(self, interval: float = 3600.0) -> None:
        """Loop: archive every *interval* seconds."""
        self._running = True
        logger.info(
            "EventArchive started (interval=%.0fs, retention=%dd)",
            interval, self.retention_days,
        )
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("EventArchive: error in cycle")
            await __import__("asyncio").sleep(interval)

    def stop(self) -> None:
        self._running = False

    async def count_live(self) -> int:
        """Return the total number of events in the live table."""
        async with async_session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM event_store"))
            return result.scalar() or 0

    async def count_archive(self) -> int:
        """Return the total number of events in the archive table."""
        async with async_session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM event_store_archive")
            )
            return result.scalar() or 0

    async def query_archive(
        self,
        sku: str | None = None,
        event_type: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query archived events with optional filters."""
        async with async_session_factory() as session:
            conditions = ["1=1"]
            params: dict[str, Any] = {}
            if sku:
                conditions.append("sku = :sku")
                params["sku"] = sku
            if event_type:
                conditions.append("event_type = :event_type")
                params["event_type"] = event_type
            if state:
                conditions.append("state = :state")
                params["state"] = state

            sql = (
                f"SELECT * FROM event_store_archive "
                f"WHERE {' AND '.join(conditions)} "
                f"ORDER BY created_at DESC LIMIT :limit"
            )
            params["limit"] = limit
            result = await session.execute(text(sql), params)
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]
