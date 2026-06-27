"""Testes do Event Archive — arquivamento e consulta.

Cobre:
  - Archive de eventos antigos
  - Nada pra arquivar
  - Consulta no archive
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.event_archive import EventArchiveService
from app.models.event_store import EventStore


class TestEventArchive:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from app.database import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        # Also create archive table manually for tests
        from app.database import async_session_factory
        async with async_session_factory() as s:
            from sqlalchemy import text
            await s.execute(text("""
                CREATE TABLE IF NOT EXISTS event_store_archive (
                    id VARCHAR(36) PRIMARY KEY,
                    event_type VARCHAR(64) NOT NULL,
                    payload TEXT NOT NULL,
                    state VARCHAR(16) NOT NULL,
                    sku VARCHAR(64),
                    channel VARCHAR(32),
                    ospos_synced BOOLEAN DEFAULT 0,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 5,
                    created_at DATETIME,
                    updated_at DATETIME,
                    archived_at DATETIME NOT NULL
                )
            """))
            await s.commit()

    @pytest.fixture
    def archive(self):
        return EventArchiveService(retention_days=1)  # 1 day cutoff for testing

    async def _insert_event(self, **overrides):
        from app.database import async_session_factory
        from app.services.event_processor import create_event

        async with async_session_factory() as s:
            ev = create_event(
                event_type=overrides.get("event_type", "stock.updated"),
                payload=overrides.get("payload", {"sku": "ABC", "quantity": 5}),
                sku=overrides.get("sku", "ABC"),
            )
            ev.state = overrides.get("state", "completed")
            # Set old date for archiving
            ev.created_at = overrides.get("created_at", datetime.now(timezone.utc) - timedelta(days=10))
            ev.updated_at = overrides.get("updated_at", datetime.now(timezone.utc) - timedelta(days=10))
            s.add(ev)
            await s.commit()

    async def test_archive_old_events(self, archive):
        await self._insert_event()
        count = await archive.run_once()
        assert count == 1

        live = await archive.count_live()
        assert live == 0

        archived = await archive.count_archive()
        assert archived == 1

    async def test_archive_nothing_to_do(self, archive):
        """Eventos recentes não são arquivados."""
        await self._insert_event(
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        count = await archive.run_once()
        assert count == 0

    async def test_query_archive(self, archive):
        await self._insert_event(
            sku="SKU-ARC", event_type="stock.updated",
        )
        await archive.run_once()

        results = await archive.query_archive(sku="SKU-ARC")
        assert len(results) == 1
        assert results[0]["sku"] == "SKU-ARC"

    async def test_query_archive_empty(self, archive):
        results = await archive.query_archive(sku="NOT-FOUND")
        assert results == []
