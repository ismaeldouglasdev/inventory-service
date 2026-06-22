"""Fixtures compartilhadas para os testes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

# ── Force in-memory SQLite BEFORE any DB imports ──────────────────────
# This prevents tests from touching the real database file and avoids
# index conflicts with existing Alembic migrations.
from app.config import settings

settings.database_url = "sqlite+aiosqlite://"

from app.adapters.base import MarketplaceAdapter
from app.adapters.registry import AdapterRegistry


class FakeAdapter(MarketplaceAdapter):
    """Adapter fictício para testes — retorna sucesso ou falha
    conforme configurado."""

    def __init__(self, channel: str = "fake", should_fail: bool = False) -> None:
        self._channel = channel
        self._should_fail = should_fail
        self.authenticate = AsyncMock(return_value=not should_fail)
        self.update_stock = AsyncMock(return_value=not should_fail)
        self.update_price = AsyncMock(return_value=not should_fail)
        self.publish_product = AsyncMock()
        self.parse_webhook = AsyncMock(
            return_value={"event_type": "test", "channel": channel, "raw": {}}
        )
        self.get_external_id = AsyncMock(
            return_value=None if should_fail else "ext-123"
        )

    @property
    def channel_name(self) -> str:
        return self._channel

    async def authenticate(self) -> bool:
        return not self._should_fail

    async def update_stock(self, sku: str, quantity: int) -> bool:
        return not self._should_fail

    async def update_price(self, sku: str, price: float) -> bool:
        return not self._should_fail

    async def publish_product(self, product: dict[str, Any]) -> str:
        if self._should_fail:
            raise RuntimeError("Falha simulada")
        return "ext-456"

    async def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"event_type": "test", "channel": self._channel, "raw": payload}

    async def get_external_id(self, sku: str) -> str | None:
        return None if self._should_fail else "ext-123"


@pytest.fixture
def registry() -> AdapterRegistry:
    """Registry com adapter fake que sempre funciona."""
    reg = AdapterRegistry()
    reg.register(FakeAdapter(channel="woocommerce", should_fail=False))
    return reg


@pytest.fixture
def failing_registry() -> AdapterRegistry:
    """Registry com adapter que sempre falha."""
    reg = AdapterRegistry()
    reg.register(FakeAdapter(channel="woocommerce", should_fail=True))
    return reg


@pytest.fixture
def multi_channel_registry() -> AdapterRegistry:
    """Registry com múltiplos canais."""
    reg = AdapterRegistry()
    reg.register(FakeAdapter(channel="woocommerce", should_fail=False))
    reg.register(FakeAdapter(channel="shopee", should_fail=False))
    return reg
