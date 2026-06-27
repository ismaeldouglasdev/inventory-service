"""Testes do ShopeeAdapter — HMAC signing, auth, stock, price, product.

Usa respostas HTTP mockadas para evitar chamadas reais à API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest


class _MockResponse:
    """Sync mock response that mimics httpx.Response's sync .json()."""
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=Mock(),
                response=self,
            )

    def json(self):
        return self._json

from app.adapters.implementations.shopee import (
    ShopeeAdapter,
    ShopeeTokenStore,
    _sign_request,
    _token_store,
)
from app.config import settings


@pytest.fixture(autouse=True)
def setup_settings():
    """Configure Shopee settings for tests."""
    settings.shopee_partner_id = 12345
    settings.shopee_api_key = "test-api-key"
    settings.shopee_sandbox = True
    settings.shopee_shop_id = 67890
    settings.shopee_access_token = "test-access-token"
    settings.shopee_refresh_token = "test-refresh-token"


@pytest.fixture(autouse=True)
def reset_token_store():
    """Reset token store between tests."""
    _token_store.access_token = settings.shopee_access_token
    _token_store.refresh_token = settings.shopee_refresh_token
    _token_store.shop_id = settings.shopee_shop_id
    _token_store.expires_at = 0  # force refresh


class TestSigning:
    """HMAC-SHA256 signing."""

    def test_sign_request_no_body(self):
        sig = _sign_request(12345, "key", "/api/v2/test", 1000000, "token")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA256 hex

    def test_sign_request_with_body(self):
        sig = _sign_request(12345, "key", "/api/v2/test", 1000000, "token", {"foo": "bar"})
        assert isinstance(sig, str)
        assert len(sig) == 64


class TestShopeeAdapter:
    """Shopee adapter tests with mocked HTTP."""

    @pytest.fixture
    def adapter(self):
        return ShopeeAdapter()

    @pytest.fixture
    def mock_client(self):
        with patch("httpx.AsyncClient") as mc:
            mc.return_value.__aenter__.return_value = AsyncMock()
            yield mc

    # ── Auth ──────────────────────────────────────────────────────

    def test_auth_url(self):
        url = ShopeeAdapter.auth_url()
        assert "test-stable.shopeemobile.com" in url
        assert "partner_id=12345" in url
        assert "redirect=" in url

    async def test_exchange_code_fails_without_credentials(self):
        settings.shopee_partner_id = 0
        with pytest.raises(Exception):
            await ShopeeAdapter.exchange_code("code123", 67890)

    async def test_authenticate_no_tokens(self):
        _token_store.access_token = ""
        _token_store.refresh_token = ""
        adapter = ShopeeAdapter()
        result = await adapter.authenticate()
        assert result is False

    # ── Channel name ─────────────────────────────────────────────

    def test_channel_name(self, adapter):
        assert adapter.channel_name == "shopee"

    # ── update_stock ─────────────────────────────────────────────

    async def test_update_stock_no_external_id(self, adapter):
        with patch.object(adapter, "get_external_id", AsyncMock(return_value=None)):
            result = await adapter.update_stock("SKU-001", 10)
            assert result is False

    async def test_update_stock_api_error(self, adapter):
        with (
            patch.object(adapter, "get_external_id", AsyncMock(return_value="123:0")),
            patch.object(adapter, "_request") as mock_req,
        ):
            mock_req.return_value = _MockResponse(status_code=500)

            result = await adapter.update_stock("SKU-001", 10)
            assert result is False

    # ── update_price ─────────────────────────────────────────────

    async def test_update_price_no_external_id(self, adapter):
        with patch.object(adapter, "get_external_id", AsyncMock(return_value=None)):
            result = await adapter.update_price("SKU-001", 29.90)
            assert result is False

    # ── get_external_id ──────────────────────────────────────────

    async def test_get_external_id_no_shop_id(self, adapter):
        _token_store.shop_id = 0
        result = await adapter.get_external_id("SKU-001")
        assert result is None

    async def test_get_external_id_api_error(self, adapter):
        with patch.object(adapter, "_request") as mock_req:
            mock_req.return_value = _MockResponse(status_code=500)

            result = await adapter.get_external_id("SKU-001")
            assert result is None

    # ── publish_product ──────────────────────────────────────────

    async def test_publish_product_api_error(self, adapter):
        with patch.object(adapter, "_request") as mock_req:
            mock_req.return_value = _MockResponse(status_code=500)

            with pytest.raises(httpx.HTTPStatusError):
                await adapter.publish_product({
                    "name": "Test", "sku": "T-001", "price": 10.0,
                })

    # ── parse_webhook ────────────────────────────────────────────

    async def test_parse_webhook_item_status(self, adapter):
        payload = {
            "code": "ITEM_STATUS_CHANGE",
            "data": {"item_id": 12345, "status": "NORMAL"},
        }
        result = await adapter.parse_webhook(payload)
        assert result["event_type"] == "shopee.ITEM_STATUS_CHANGE"
        assert result["channel"] == "shopee"
        assert result["sku"] == "12345"

    async def test_parse_webhook_unknown(self, adapter):
        payload = {"code": "UNKNOWN_EVENT", "data": {}}
        result = await adapter.parse_webhook(payload)
        assert result["event_type"] == "shopee.UNKNOWN_EVENT"
