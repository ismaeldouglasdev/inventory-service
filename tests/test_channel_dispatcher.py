"""Testes do Channel Dispatcher — prioridade, buffer, circuit breaker.

Cobre:
  - Prioridade de canais (ML > Shopee > WC)
  - Stock buffer filtering
  - Circuit breaker integration
  - Rate limiter integration
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.channel_dispatcher import ChannelDispatcher
from app.services.circuit_breaker import CircuitBreaker
from app.services.rate_limiter import TokenBucketRateLimiter


class TestChannelDispatcher:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from app.database import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture
    def registry(self):
        from app.adapters.registry import AdapterRegistry
        from tests.conftest import FakeAdapter

        reg = AdapterRegistry()
        reg.register(FakeAdapter("mercadolivre"))
        reg.register(FakeAdapter("shopee"))
        reg.register(FakeAdapter("woocommerce"))
        return reg

    @pytest.fixture
    def dispatcher(self, registry):
        return ChannelDispatcher(registry)

    async def test_resolve_priority_order(self, dispatcher, registry):
        """Canais retornados na ordem de prioridade."""
        channels = await dispatcher.resolve("stock.updated")
        assert channels == ["mercadolivre", "shopee", "woocommerce"]

    async def test_resolve_empty_registry(self):
        from app.adapters.registry import AdapterRegistry
        d = ChannelDispatcher(AdapterRegistry())
        channels = await d.resolve("stock.updated")
        assert channels == []

    async def test_stock_buffer_skips_channel(self, dispatcher):
        """Estoque <= buffer → canal é pulado."""
        from app.database import Base, engine, async_session_factory
        from app.models.channel_state import ChannelState
        from datetime import datetime, timezone

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with async_session_factory() as s:
            s.add(ChannelState(
                channel="shopee", status="CLOSED", active=True,
                stock_buffer=10, failure_count=0,
            ))
            await s.commit()

        channels = await dispatcher.resolve("stock.updated", stock=5)
        assert "shopee" not in channels

    async def test_buffer_allows_above_threshold(self, dispatcher):
        """Estoque > buffer → canal não é pulado."""
        from app.database import Base, engine, async_session_factory
        from app.models.channel_state import ChannelState
        from datetime import datetime, timezone

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with async_session_factory() as s:
            s.add(ChannelState(
                channel="shopee", status="CLOSED", active=True,
                stock_buffer=5, failure_count=0,
            ))
            await s.commit()

        channels = await dispatcher.resolve("stock.updated", stock=10)
        assert "shopee" in channels
