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
from sqlalchemy import select

from app.adapters.base import MarketplaceAdapter
from app.config import settings
from app.database import async_session_factory
from app.models.channel_product_mapping import ChannelProductMapping
from app.services.ml_pricing import compute_ml_price

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
            from app.services.ml_oauth import save_ml_tokens

            await save_ml_tokens(data)
            return data

    @staticmethod
    async def load_from_db() -> None:
        """Load persisted OAuth tokens from the DB into the in-memory store."""
        from app.services.ml_oauth import load_ml_tokens

        data = await load_ml_tokens()
        if not data:
            return
        _token_store.access_token = data.get("access_token", "")
        _token_store.refresh_token = data.get("refresh_token", "")
        _token_store.user_id = data.get("user_id", 0)
        _token_store.expires_at = data.get("expires_at", 0.0)
        if _token_store.access_token:
            logger.info(
                "ML: tokens carregados do banco (user_id=%s)", _token_store.user_id
            )

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
                data = resp.json()
                _token_store.update(data)
                from app.services.ml_oauth import save_ml_tokens

                await save_ml_tokens(data)
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
        """Resolve an internal SKU to the ML item ID.

        Checks the local ``channel_product_mapping`` first (fast, no API
        call). Falls back to scanning the seller's items for a matching
        ``seller_custom_field`` when no mapping exists.
        """
        # 1. Local mapping (fast path)
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(ChannelProductMapping).where(
                        ChannelProductMapping.sku == sku,
                        ChannelProductMapping.channel == "mercadolivre",
                    )
                )
                mapping = result.scalar_one_or_none()
                if mapping:
                    return mapping.external_id
        except Exception:
            logger.exception("ML get_external_id: local mapping lookup failed")

        # 2. Fallback: scan seller items for matching seller_custom_field
        if not _token_store.user_id:
            logger.warning("ML user_id not set — cannot search products")
            return None

        offset = 0
        limit = 50
        while True:
            params = {
                "seller_id": str(_token_store.user_id),
                "search_type": "scan",
                "limit": str(limit),
                "offset": str(offset),
            }
            try:
                resp = await self._request("GET", "/sites/MLB/search", params=params)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error("ML SKU lookup failed: %s", exc.response.text[:300])
                return None

            results = data.get("results", [])
            for result in results:
                if result.get("seller_custom_field") == sku:
                    return result["id"]

            paging = data.get("paging", {})
            total = paging.get("total", 0)
            offset += limit
            if offset >= total or not results:
                break

        logger.info("SKU %s not found on ML", sku)
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
          - ``price``         → decimal (PDV / store price — base do markup)
          - ``stock_quantity`` → int
          - ``category_id``   → ML category ID (e.g. \"MLB1234\")
          - ``condition``     → \"new\" or \"used\" (default: \"new\")
          - ``listing_type_id`` → \"gold_pro\", \"gold_special\", \"free\" (def: \"gold_special\")
          - ``pictures``      → list of URLs
        """
        pricing = compute_ml_price(
            product.get("price", 0),
            product.get("cost_price"),
        )
        body: dict[str, Any] = {
            "title": product.get("title", ""),
            "category_id": product.get("category_id", settings.ml_default_category),
            "price": pricing.price,
            "currency_id": "BRL",
            "available_quantity": product.get("stock_quantity", 1),
            "condition": product.get("condition", "new"),
            "listing_type_id": pricing.listing_type_id,
            "seller_custom_field": product.get("sku", ""),
            "sale_terms": [
                {"id": "WARRANTY_TYPE", "value_name": "Sem garantia"},
            ],
        }

        if pricing.shipping_mode == "free":
            body["shipping"] = {"mode": "me2", "free_shipping": True}

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

    # ------------------------------------------------------------------
    # Catalog products (produto de catálogo do ML por EAN)
    # ------------------------------------------------------------------

    async def search_catalog_by_ean(self, ean: str) -> dict[str, Any] | None:
        """Search the ML catalog for a product by EAN (barcode).

        Returns the first active catalog product match (``catalog_product_id``
        plus name/pictures) or ``None`` when nothing is found.
        """
        params = {"status": "active", "site_id": "MLB", "q": ean}
        try:
            resp = await self._request("GET", "/products/search", params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                logger.info("ML: no catalog product for EAN %s", ean)
                return None
            p = results[0]
            logger.info(
                "ML: catalog product for EAN %s → %s (%s)",
                ean, p.get("id"), (p.get("name") or "")[:50],
            )
            return {
                "catalog_product_id": p.get("id"),
                "name": p.get("name", p.get("title", "")),
                "pictures": p.get("pictures", []),
                "status": p.get("status"),
                "domain_id": p.get("domain_id"),
            }
        except httpx.HTTPStatusError as exc:
            logger.error("ML catalog search failed (EAN %s): %s", ean, exc.response.text[:300])
            return None

    async def publish_catalog_listing(self, product: dict[str, Any]) -> str:
        """Publish a catalog listing on Mercado Livre.

        Links the listing to a catalog product (``catalog_product_id``), so ML
        supplies the official title, photos and attributes. The seller only
        defines price, stock and SKU.

        ``product`` expects keys:
          - ``catalog_product_id`` → ML catalog product ID (e.g. "MLB67014274")
          - ``sku``                → stored as seller_custom_field
          - ``price``              → decimal (PDV / store price — base do markup)
          - ``stock_quantity``     → int
          - ``condition``          → "new" (default) or "used"
          - ``listing_type_id``    → default "gold_special"
        """
        pricing = compute_ml_price(
            product.get("price", 0),
            product.get("cost_price"),
        )
        # ML derives the title from the catalog product, so we must NOT send it.
        body: dict[str, Any] = {
            "site_id": "MLB",
            "catalog_product_id": product.get("catalog_product_id"),
            "catalog_listing": True,
            "price": pricing.price,
            "currency_id": "BRL",
            "available_quantity": product.get("stock_quantity", 1),
            "condition": product.get("condition", "new"),
            "listing_type_id": pricing.listing_type_id,
            "seller_custom_field": product.get("sku", ""),
            "sale_terms": [
                {"id": "WARRANTY_TYPE", "value_name": "Sem garantia"},
            ],
        }

        if pricing.shipping_mode == "free":
            body["shipping"] = {"mode": "me2", "free_shipping": True}

        category_id = product.get("category_id") or settings.ml_default_category
        if not category_id:
            # Derive the category from the catalog product name via domain discovery.
            name = product.get("name") or product.get("title") or ""
            cat = await self._discover_category(name)
            if cat:
                category_id = cat
        body["category_id"] = category_id

        try:
            resp = await self._request("POST", "/items", json=body)
            resp.raise_for_status()
            created = resp.json()
            external_id = created["id"]
            logger.info(
                "ML catalog listing published: SKU %s → ID %s (catalog %s)",
                product.get("sku"),
                external_id,
                product.get("catalog_product_id"),
            )
            await self._save_mapping(product.get("sku", ""), external_id)
            return external_id
        except httpx.HTTPStatusError as exc:
            logger.error("ML catalog publish failed: %s", exc.response.text[:600])
            raise

    async def _save_mapping(self, sku: str, external_id: str) -> None:
        """Persist the SKU → ML item ID mapping for fast stock/price sync."""
        if not sku:
            return
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(ChannelProductMapping).where(
                        ChannelProductMapping.sku == sku,
                        ChannelProductMapping.channel == "mercadolivre",
                    )
                )
                mapping = result.scalar_one_or_none()
                if mapping:
                    mapping.external_id = external_id
                else:
                    session.add(
                        ChannelProductMapping(
                            sku=sku,
                            channel="mercadolivre",
                            external_id=external_id,
                            external_url=f"https://www.mercadolivre.com.br/items/{external_id}",
                            status="active",
                        )
                    )
                await session.commit()
        except Exception:
            logger.exception("ML: failed to save channel mapping for SKU %s", sku)

    async def _discover_category(self, query: str) -> str | None:
        """Resolve the ML category_id for a product name via domain discovery."""
        if not query:
            return None
        params = {"limit": 1, "q": query}
        try:
            resp = await self._request(
                "GET", "/sites/MLB/domain_discovery/search", params=params
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data and data[0].get("category_id"):
                return data[0]["category_id"]
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("ML category discovery failed: %s", exc.response.text[:200])
            return None

    # ------------------------------------------------------------------
    # Stock / price convenience (updates existing listings)
    # ------------------------------------------------------------------
