from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1.shipping import router, shipping_quote, _FREE_THRESHOLD, _BASE_RATE


@pytest.mark.asyncio
class TestShippingQuote:
    async def test_default_params(self):
        result = await shipping_quote(subtotal=0.0, qty=1)
        assert result["subtotal"] == 0.0
        assert result["free_shipping"] is False
        assert result["free_threshold"] == _FREE_THRESHOLD
        assert len(result["options"]) == 2
        assert result["options"][0]["name"] == "Padrão"
        assert result["options"][1]["name"] == "Expresso"

    async def test_standard_price_single_item(self):
        result = await shipping_quote(subtotal=50.0, qty=1)
        assert result["free_shipping"] is False
        assert result["options"][0]["price"] == _BASE_RATE
        assert result["options"][0]["free"] is False
        assert result["options"][0]["days_min"] == 5
        assert result["options"][0]["days_max"] == 10

    async def test_standard_price_multiple_items(self):
        result = await shipping_quote(subtotal=100.0, qty=3)
        expected = round(_BASE_RATE + 2.0 * 2, 2)
        assert result["options"][0]["price"] == expected
        assert result["free_shipping"] is False

    async def test_free_shipping_above_threshold(self):
        result = await shipping_quote(subtotal=_FREE_THRESHOLD, qty=5)
        assert result["free_shipping"] is True
        assert result["options"][0]["price"] == 0.0
        assert result["options"][0]["free"] is True
        assert result["options"][1]["price"] == 0.0
        assert result["options"][1]["free"] is True

    async def test_free_shipping_above_threshold_large_order(self):
        result = await shipping_quote(subtotal=500.0, qty=10)
        assert result["free_shipping"] is True
        assert all(opt["free"] for opt in result["options"])
        assert all(opt["price"] == 0.0 for opt in result["options"])

    async def test_express_faster_than_standard(self):
        result = await shipping_quote(subtotal=50.0, qty=1)
        std = result["options"][0]
        exp = result["options"][1]
        assert exp["days_min"] < std["days_min"]
        assert exp["days_max"] < std["days_max"]

    async def test_express_price_multiplier(self):
        result = await shipping_quote(subtotal=50.0, qty=1)
        std_price = result["options"][0]["price"]
        exp_price = result["options"][1]["price"]
        assert exp_price == round(std_price * 1.6, 2)

    async def test_zero_subtotal(self):
        result = await shipping_quote(subtotal=0.0, qty=1)
        assert result["subtotal"] == 0.0
        assert result["free_shipping"] is False
        assert result["options"][0]["price"] == _BASE_RATE
