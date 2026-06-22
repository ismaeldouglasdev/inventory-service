"""Mercado Livre REST API adapter.

OAuth 2.0 flow:
  1. GET /v1/mercadolivre/auth-url → redirect user to ML
  2. ML redirects to /v1/mercadolivre/callback?code=...
  3. Token stored in env / DB, adapter ready to use

API docs: https://developers.mercadolivre.com.br/
"""

from __future__ import annotations

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

_API_BASE = "https://api.mercadolibre.com"
_AUTH_BASE = "https://auth.mercadolivre.com.br"  # Brazil (authorization)
_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # Token exchange
_API_TIMEOUT = 30.0


# ── Token helpers ───────────────────────────────────────────────────────


class MLTokenStore:
    """Simple in-memory + env token store for ML OAuth tokens.

    In production replace with DB-backed storage (ChannelState table).
    """

    def __init__(self) -> None:
        self.access_token: str = settings.ml_access_token or ""
        self.refresh_token: str = settings.ml_refresh_token or ""
        self.user_id: int = settings.ml_user_id or 0
        self.expires_at: float = 0.0  # epoch seconds

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at

    def update(self, data: dict[str, Any]) -> None:
        self.access_token = data.get("access_token", self.access_token)
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        expires_in = data.get("expires_in", 0)
        self.expires_at = time.time() + expires_in - 60  # 1m buffer
        self.user_id = data.get("user_id", self.user_id)


# Module-level singleton (hack until DB-backed)
_token_store = MLTokenStore()


# ── Adapter ──────────────────────────────────────────────────────────────


class MercadoLivreAdapter(MarketplaceAdapter):
    """Adapter for Mercado Livre marketplace via their REST API."""

    @property
    def channel_name(self) -> str:
        return "mercadolivre"

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    @staticmethod
    def auth_url() -> str:
        """Return the URL a seller must visit to authorise the app."""
        params = urlencode({
            "client_id": settings.ml_client_id,
            "response_type": "code",
            "redirect_uri": settings.ml_redirect_uri,
        })
        return f"{_AUTH_BASE}/authorization?{params}"

    @staticmethod
    async def exchange_code(code: str) -> dict[str, Any]:
        """Exchange an OAuth authorisation *code* for access/refresh tokens."""
        url = _TOKEN_URL
        body = {
            "grant_type": "authorization_code",
            "client_id": settings.ml_client_id,
            "client_secret": settings.ml_client_secret,
            "code": code,
            "redirect_uri": settings.ml_redirect_uri,
        }
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.post(url, data=body)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            _token_store.update(data)
            return data

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token when it expires."""
        if not _token_store.refresh_token:
            logger.error("ML: no refresh token available")
            return False

        url = _TOKEN_URL
        body = {
            "grant_type": "refresh_token",
            "client_id": settings.ml_client_id,
            "client_secret": settings.ml_client_secret,
            "refresh_token": _token_store.refresh_token,
        }
        try:
            async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
                resp = await client.post(url, data=body)
                resp.raise_for_status()
                _token_store.update(resp.json())
                return True
        except httpx.HTTPStatusError as exc:
            logger.error("ML token refresh failed: %s", exc.response.text[:300])
            return False
        except httpx.RequestError as exc:
            logger.error("ML token refresh network error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """Verify stored credentials are still valid (try refresh if needed)."""
        if _token_store.is_authenticated:
            return True
        if _token_store.refresh_token:
            return await self._refresh_access_token()
        return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {_token_store.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request, auto-refreshing token on 401."""
        url = f"{_API_BASE}{path}"
        kwargs.setdefault("headers", self._headers())
        kwargs.setdefault("timeout", _API_TIMEOUT)

        async with httpx.AsyncClient() as client:
            resp = await client.request(method, url, **kwargs)

        if resp.status_code == 401:
            logger.info("ML token expired, refreshing…")
            refreshed = await self._refresh_access_token()
            if refreshed:
                kwargs["headers"] = self._headers()
                async with httpx.AsyncClient() as client:
                    resp = await client.request(method, url, **kwargs)
            else:
                resp.raise_for_status()

        return resp

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    async def update_stock(self, sku: str, quantity: int) -> bool:
        """Update available quantity on Mercado Livre."""
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update stock — SKU %s not found on ML", sku)
            return False

        # ML uses 'available_quantity' field
        body = {"available_quantity": quantity}
        try:
            resp = await self._request("PUT", f"/items/{external_id}", json=body)
            resp.raise_for_status()
            logger.info("ML stock updated for SKU %s (ID %s) → %d", sku, external_id, quantity)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error("ML stock update failed for SKU %s: %s", sku, exc.response.text[:400])
            return False

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------

    async def update_price(self, sku: str, price: float) -> bool:
        """Update price on Mercado Livre."""
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update price — SKU %s not found on ML", sku)
            return False

        body = {"price": price}
        try:
            resp = await self._request("PUT", f"/items/{external_id}", json=body)
            resp.raise_for_status()
            logger.info("ML price updated for SKU %s (ID %s) → %.2f", sku, external_id, price)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error("ML price update failed for SKU %s: %s", sku, exc.response.text[:400])
            return False

    # ------------------------------------------------------------------
    # Webhook parsing
    # ------------------------------------------------------------------

    async def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise a ML webhook payload to internal format.

        ML sends different topics:
          - \"items\"        → product changes
          - \"orders_v2\"    → order changes
          - \"questions\"    → buyer questions
        """
        topic = payload.get("topic", "unknown")
        resource = payload.get("resource", "")

        internal: dict[str, Any] = {
            "event_type": f"mercadolivre.{topic}",
            "channel": "mercadolivre",
            "raw": payload,
        }

        # Extract SKU from the resource URL (resource = /items/MLB123456789)
        if resource and "/items/" in resource:
            internal["sku"] = resource.split("/items/")[-1]
            # This is the ML item ID, not our SKU — but it's what we have

        return internal

    # ------------------------------------------------------------------
    # SKU → external ID resolution
    # ------------------------------------------------------------------

    async def get_external_id(self, sku: str) -> str | None:
        """Search for a product on ML by SKU (stored in seller_custom_field).

        ML doesn't have a native SKU field; we store it in
        ``seller_custom_field`` during publish.
        """
        if not _token_store.user_id:
            logger.warning("ML user_id not set — cannot search products")
            return None

        params = {
            "seller_id": str(_token_store.user_id),
            "search_type": "scan",
            "q": sku,  # searches title + description; not ideal but works
        }
        try:
            resp = await self._request("GET", "/sites/MLB/search", params=params)
            resp.raise_for_status()
            data = resp.json()

            # Try to find exact SKU match in seller_custom_field
            for result in data.get("results", []):
                if result.get("seller_custom_field") == sku:
                    return result["id"]

            # Fallback: return first result if only one matches
            if len(data.get("results", [])) == 1:
                return data["results"][0]["id"]

            logger.info("SKU %s not found on ML", sku)
            return None
        except httpx.HTTPStatusError as exc:
            logger.error("ML SKU lookup failed: %s", exc.response.text[:300])
            return None

    # ------------------------------------------------------------------
    # Product publishing
    # ------------------------------------------------------------------

    async def publish_product(self, product: dict[str, Any]) -> str:
        """Create a new product listing on Mercado Livre.

        ``product`` expects keys:
          - ``title``         → listing title
          - ``sku``           → stored as seller_custom_field
          - ``description``   → plain text or HTML
          - ``price``         → decimal
          - ``stock_quantity`` → int
          - ``category_id``   → ML category ID (e.g. \"MLB1234\")
          - ``condition``     → \"new\" or \"used\" (default: \"new\")
          - ``listing_type_id`` → \"gold_pro\", \"gold_special\", \"free\" (def: \"gold_special\")
          - ``pictures``      → list of URLs
        """
        body: dict[str, Any] = {
            "title": product.get("title", ""),
            "category_id": product.get("category_id", settings.ml_default_category),
            "price": product.get("price", 0),
            "currency_id": "BRL",
            "available_quantity": product.get("stock_quantity", 1),
            "condition": product.get("condition", "new"),
            "listing_type_id": product.get("listing_type_id", "gold_special"),
            "seller_custom_field": product.get("sku", ""),
            "sale_terms": [
                {"id": "WARRANTY_TYPE", "value_name": "Sem garantia"},
            ],
        }

        # Description
        description = product.get("description", "")
        if description:
            body["description"] = {"plain_text": description}

        # Pictures
        pictures = product.get("pictures", [])
        if pictures:
            body["pictures"] = [{"source": url} for url in pictures]

        # Attributes (brand, model, etc.)
        attributes = product.get("attributes", [])
        if attributes:
            body["attributes"] = attributes

        try:
            resp = await self._request("POST", "/items", json=body)
            resp.raise_for_status()
            created = resp.json()
            external_id = created["id"]
            logger.info(
                "ML product published: SKU %s → ID %s",
                product.get("sku"),
                external_id,
            )
            return external_id
        except httpx.HTTPStatusError as exc:
            logger.error("ML publish failed: %s", exc.response.text[:600])
            raise
