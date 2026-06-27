"""Shopee Open Platform v2 adapter.

Autenticação:
1. GET /api/v2/shop/auth_partner -> redirect seller to Shopee
2. Shopee redirects to callback with ?code=...
3. POST /api/v2/auth/token/get -> access_token + refresh_token + shop_id

Assinatura HMAC-SHA256 exigida em TODOS os requests.

Documentação: https://open.shopee.com/documents
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.adapters.base import MarketplaceAdapter
from app.config import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

_API_BASE = "https://partner.shopeemobile.com"
_API_BASE_SANDBOX = "https://partner.test-stable.shopeemobile.com"
_API_PATH = "/api/v2"
_API_TIMEOUT = 30.0


# ── HMAC signing ───────────────────────────────────────────────────────

def _sign_request(
    partner_id: int,
    api_key: str,
    path: str,
    timestamp: int,
    access_token: str = "",
    body: dict[str, Any] | None = None,
) -> str:
    """Generate HMAC-SHA256 signature required by Shopee."""
    raw = f"{partner_id}{path}{timestamp}{access_token}"
    if body:
        raw += json.dumps(body, separators=(",", ":"))
    return hmac.new(
        api_key.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ── Token Store ─────────────────────────────────────────────────────────

class ShopeeTokenStore:
    """In-memory token store for Shopee OAuth tokens.

    Production: persist to ChannelState or DB table.
    """

    def __init__(self) -> None:
        self.access_token: str = settings.shopee_access_token or ""
        self.refresh_token: str = settings.shopee_refresh_token or ""
        self.expires_at: float = 0.0
        self.shop_id: int = settings.shopee_shop_id or 0

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at

    def update(self, data: dict[str, Any]) -> None:
        self.access_token = data.get("access_token", self.access_token)
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        expires_in = data.get("expires_in", 0)
        self.expires_at = time.time() + expires_in - 60
        self.shop_id = data.get("shop_id", self.shop_id)


_token_store = ShopeeTokenStore()


# ── Adapter ──────────────────────────────────────────────────────────────

class ShopeeAdapter(MarketplaceAdapter):
    """Adapter for Shopee marketplace via Open Platform v2 API."""

    @property
    def channel_name(self) -> str:
        return "shopee"

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    @staticmethod
    def auth_url() -> str:
        """Return the URL a seller visits to authorise the app."""
        partner_id = settings.shopee_partner_id
        redirect = settings.shopee_redirect_uri
        base = _API_BASE_SANDBOX if settings.shopee_sandbox else _API_BASE
        params = urlencode({
            "partner_id": partner_id,
            "redirect": redirect,
        })
        return f"{base}/api/v2/shop/auth_partner?{params}"

    @staticmethod
    async def exchange_code(code: str, shop_id: int) -> dict[str, Any]:
        """Exchange auth code for access & refresh tokens."""
        partner_id = settings.shopee_partner_id
        api_key = settings.shopee_api_key
        timestamp = int(time.time())
        path = f"{_API_PATH}/auth/token/get"

        body = {
            "code": code,
            "partner_id": partner_id,
            "shop_id": shop_id,
        }
        signature = _sign_request(partner_id, api_key, path, timestamp, body=body)

        url = f"{_base_url()}{path}"
        params = {
            "partner_id": partner_id,
            "timestamp": timestamp,
            "sign": signature,
        }

        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.post(url, params=params, json=body)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            if data.get("error"):
                raise RuntimeError(f"Shopee auth error: {data.get('error')} — {data.get('message', '')}")
            _token_store.update(data)
            return data

    @staticmethod
    async def refresh_access_token() -> bool:
        """Refresh the access token."""
        partner_id = settings.shopee_partner_id
        api_key = settings.shopee_api_key
        timestamp = int(time.time())
        path = f"{_API_PATH}/auth/access_token/get"

        body = {
            "partner_id": partner_id,
            "shop_id": _token_store.shop_id,
            "refresh_token": _token_store.refresh_token,
        }
        signature = _sign_request(partner_id, api_key, path, timestamp, body=body)

        url = f"{_base_url()}{path}"
        params = {
            "partner_id": partner_id,
            "timestamp": timestamp,
            "sign": signature,
        }

        try:
            async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
                resp = await client.post(url, params=params, json=body)
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    logger.error("Shopee token refresh error: %s", data.get("message"))
                    return False
                _token_store.update(data)
                return True
        except httpx.HTTPStatusError as exc:
            logger.error("Shopee token refresh failed: %s", exc.response.text[:300])
            return False
        except httpx.RequestError as exc:
            logger.error("Shopee token refresh network error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """Verify credentials are valid, try refresh if needed."""
        if _token_store.is_authenticated and _token_store.shop_id:
            return True
        if _token_store.refresh_token:
            return await self.refresh_access_token()
        return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _signed_params(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build query params with HMAC signature for a Shopee API call."""
        partner_id = settings.shopee_partner_id
        api_key = settings.shopee_api_key
        timestamp = int(time.time())
        access_token = _token_store.access_token
        signature = _sign_request(partner_id, api_key, path, timestamp, access_token, body)

        return {
            "partner_id": partner_id,
            "timestamp": timestamp,
            "access_token": access_token,
            "shop_id": _token_store.shop_id,
            "sign": signature,
        }

    async def _request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make a signed POST request to the Shopee API."""
        url = f"{_base_url()}{path}"
        params = self._signed_params(path, body)

        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.post(url, params=params, json=body or {})

        if resp.status_code == 401 or resp.status_code == 403:
            logger.info("Shopee token expired, refreshing…")
            refreshed = await self.refresh_access_token()
            if refreshed:
                params = self._signed_params(path, body)
                async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
                    resp = await client.post(url, params=params, json=body or {})
            else:
                resp.raise_for_status()

        return resp

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    async def update_stock(self, sku: str, quantity: int) -> bool:
        """Update stock for a product variation on Shopee.

        Shopee requires the ``item_id`` and ``variation_id``.
        We store these in channel_product_mapping.
        """
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update stock — SKU %s not found on Shopee", sku)
            return False

        # external_id can be "item_id" or "item_id:variation_id"
        parts = external_id.split(":")
        item_id = int(parts[0])
        variation_id = int(parts[1]) if len(parts) > 1 else 0

        body: dict[str, Any] = {
            "item_id": item_id,
            "stock_list": [{"variation_id": variation_id, "stock": quantity}],
        }
        try:
            resp = await self._request("/api/v2/product/update_stock", body)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                logger.error("Shopee stock update error for SKU %s: %s", sku, data.get("message"))
                return False
            logger.info("Shopee stock updated for SKU %s → %d", sku, quantity)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error("Shopee stock update failed for SKU %s: %s", sku, exc.response.text[:400])
            return False

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------

    async def update_price(self, sku: str, price: float) -> bool:
        """Update price for a product variation on Shopee."""
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update price — SKU %s not found on Shopee", sku)
            return False

        parts = external_id.split(":")
        item_id = int(parts[0])
        variation_id = int(parts[1]) if len(parts) > 1 else 0

        body: dict[str, Any] = {
            "item_id": item_id,
            "price_list": [{"variation_id": variation_id, "original_price": price}],
        }
        try:
            resp = await self._request("/api/v2/product/update_price", body)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                logger.error("Shopee price update error for SKU %s: %s", sku, data.get("message"))
                return False
            logger.info("Shopee price updated for SKU %s → %.2f", sku, price)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error("Shopee price update failed for SKU %s: %s", sku, exc.response.text[:400])
            return False

    # ------------------------------------------------------------------
    # Webhook parsing
    # ------------------------------------------------------------------

    async def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise a Shopee webhook payload to internal format.

        Shopee webhooks have a ``code`` and ``data`` structure.
        Topics: ITEM_STATUS_CHANGE, ORDER_STATUS_CHANGE, etc.
        """
        code = payload.get("code", "unknown")
        data = payload.get("data", {})

        internal: dict[str, Any] = {
            "event_type": f"shopee.{code}",
            "channel": "shopee",
            "raw": payload,
        }

        # Extract item_id if present
        item_id = data.get("item_id") or data.get("item_id_list", [None])[0]
        if item_id:
            internal["sku"] = str(item_id)

        return internal

    # ------------------------------------------------------------------
    # SKU → external ID resolution
    # ------------------------------------------------------------------

    async def get_external_id(self, sku: str) -> str | None:
        """Resolve SKU to Shopee ``item_id:variation_id``.

        Uses ``/api/v2/product/get_item_list`` and searches by
        ``item_sku`` / ``variation_sku``.
        """
        if not _token_store.shop_id:
            logger.warning("Shopee shop_id not set — cannot search products")
            return None

        body = {
            "shop_id": _token_store.shop_id,
            "page_size": 100,
            "offset": 0,
            "item_status": ["NORMAL"],
        }
        try:
            resp = await self._request("/api/v2/product/get_item_list", body)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                logger.warning("Shopee item list error: %s", data.get("message"))
                return None

            for item in data.get("response", {}).get("item_list", []):
                # Check item-level SKU
                if item.get("item_sku") == sku:
                    return f"{item['item_id']}:0"
                # Check variation-level SKU
                for var in item.get("variations", []):
                    if var.get("variation_sku") == sku:
                        return f"{item['item_id']}:{var['variation_id']}"

            logger.info("SKU %s not found on Shopee", sku)
            return None
        except httpx.HTTPStatusError as exc:
            logger.error("Shopee SKU lookup failed: %s", exc.response.text[:400])
            return None

    # ------------------------------------------------------------------
    # Product publishing
    # ------------------------------------------------------------------

    async def publish_product(self, product: dict[str, Any]) -> str:
        """Create a new product listing on Shopee.

        ``product`` expects:
          - ``name``             → item name
          - ``sku``              → item_sku
          - ``description``      → item description
          - ``price``            → original_price
          - ``stock_quantity``   → stock
          - ``category_id``      → Shopee category ID
          - ``images``           → list of image URLs
          - ``weight``           → kg (optional)
          - ``dimensions``       → dict with package_length, etc. (optional)
        """
        body: dict[str, Any] = {
            "item_name": product.get("name", ""),
            "item_sku": product.get("sku", ""),
            "description": product.get("description", ""),
            "category_id": product.get("category_id", 1),
            "original_price": product.get("price", 0),
            "stock": product.get("stock_quantity", 1),
            "normal_stock": product.get("stock_quantity", 1),
            "weight": product.get("weight", 0.5),
            "item_status": "NORMAL",
            "description_type": "plain",
        }

        images = product.get("images", [])
        if images:
            body["image"] = {"image_url_list": images[:9]}

        try:
            resp = await self._request("/api/v2/product/add", body)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(f"Shopee publish error: {data.get('message', '')}")
            item_id = str(data["response"]["item_id"])
            logger.info("Shopee product published: SKU %s → ID %s", product.get("sku"), item_id)
            return f"{item_id}:0"
        except httpx.HTTPStatusError as exc:
            logger.error("Shopee publish failed: %s", exc.response.text[:600])
            raise


def _base_url() -> str:
    """Return the API base URL for current environment."""
    return _API_BASE_SANDBOX if settings.shopee_sandbox else _API_BASE
