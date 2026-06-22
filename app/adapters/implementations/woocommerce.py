"""WooCommerce REST API adapter.

Communicates with the WooCommerce API v3 via Basic Auth
(Consumer Key / Consumer Secret).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.adapters.base import MarketplaceAdapter
from app.config import settings

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────

_BASE_PATH = "/wp-json/wc/v3"


def _auth_params() -> dict[str, str]:
    """Return query parameters for WooCommerce Basic Auth."""
    return {
        "consumer_key": settings.wood_commerce_consumer_key,
        "consumer_secret": settings.wood_commerce_consumer_secret,
    }


def _build_url(path: str) -> str:
    base = settings.wood_commerce_url.rstrip("/")
    return f"{base}{_BASE_PATH}{path}"


def _webhook_signature(payload: bytes, secret: str) -> str:
    """Compute WooCommerce webhook signature for verification."""
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


# ── Adapter ────────────────────────────────────────────────────────────


class WooCommerceAdapter(MarketplaceAdapter):
    """Adapter for WooCommerce stores via the REST API v3."""

    @property
    def channel_name(self) -> str:
        return "woocommerce"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def authenticate(self) -> bool:
        """Verify credentials by hitting the system-status endpoint."""
        url = _build_url("/system_status")
        params = _auth_params()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                logger.info(
                    "WooCommerce auth OK — store %s",
                    settings.wood_commerce_url,
                )
                return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "WooCommerce auth failed (%s): %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return False
        except httpx.RequestError as exc:
            logger.error("WooCommerce unreachable: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    async def update_stock(self, sku: str, quantity: int) -> bool:
        """Update stock quantity on WooCommerce.

        Resolves the SKU to a product ID first, then issues a partial
        update with ``stock_quantity``.
        """
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update stock — SKU %s not found on WooCommerce", sku)
            return False

        url = _build_url(f"/products/{external_id}")
        params = _auth_params()
        body = {"stock_quantity": quantity}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.put(url, params=params, json=body)
                resp.raise_for_status()
                logger.info("Stock updated for SKU %s (ID %s) → %d", sku, external_id, quantity)
                return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Stock update failed for SKU %s: %s",
                sku,
                exc.response.text[:300],
            )
            return False
        except httpx.RequestError as exc:
            logger.error("Stock update request error for SKU %s: %s", sku, exc)
            return False

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------
    async def update_price(self, sku: str, price: float) -> bool:
        """Update regular price on WooCommerce.

        Resolves the SKU to a product ID, then issues a partial update
        with ``regular_price``.
        """
        external_id = await self.get_external_id(sku)
        if external_id is None:
            logger.warning("Cannot update price — SKU %s not found on WooCommerce", sku)
            return False

        url = _build_url(f"/products/{external_id}")
        params = _auth_params()
        body = {"regular_price": str(price)}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.put(url, params=params, json=body)
                resp.raise_for_status()
                logger.info("Price updated for SKU %s (ID %s) → %.2f", sku, external_id, price)
                return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Price update failed for SKU %s: %s",
                sku,
                exc.response.text[:300],
            )
            return False
        except httpx.RequestError as exc:
            logger.error("Price update request error for SKU %s: %s", sku, exc)
            return False

    # ------------------------------------------------------------------
    # Webhook parsing
    # ------------------------------------------------------------------
    async def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise a WooCommerce webhook payload to internal format.

        Supported webhook topics:
          - ``product.updated`` / ``product.created``
          - ``order.created`` / ``order.updated``
        """
        event_type = payload.get("action", "unknown")
        raw_data = payload.get("data", payload)

        internal: dict[str, Any] = {
            "event_type": f"woocommerce.{event_type}",
            "channel": "woocommerce",
            "raw": payload,
        }

        # Extract SKU when a product payload is present
        if isinstance(raw_data, dict):
            internal["sku"] = raw_data.get("sku")
            # Order payloads may contain line items with SKUs
            if "line_items" in raw_data:
                skus = [
                    item.get("sku")
                    for item in raw_data["line_items"]
                    if item.get("sku")
                ]
                if skus:
                    internal["skus"] = skus

        return internal

    # ------------------------------------------------------------------
    # SKU → external ID resolution
    # ------------------------------------------------------------------
    async def get_external_id(self, sku: str) -> str | None:
        """Search for a product by SKU and return its WooCommerce ID.

        Uses the ``sku`` query parameter which WooCommerce v3 supports
        natively.
        """
        url = _build_url("/products")
        params = {**_auth_params(), "sku": sku}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    product_id = str(data[0]["id"])
                    logger.debug("Resolved SKU %s → WooCommerce ID %s", sku, product_id)
                    return product_id
                logger.info("SKU %s not found on WooCommerce", sku)
                return None
        except httpx.HTTPStatusError as exc:
            logger.error(
                "SKU lookup failed for %s: %s",
                sku,
                exc.response.text[:200],
            )
            return None
        except httpx.RequestError as exc:
            logger.error("SKU lookup request error for %s: %s", sku, exc)
            return None

    # ------------------------------------------------------------------
    # Product publishing
    # ------------------------------------------------------------------
    async def publish_product(self, product: dict[str, Any]) -> str:
        """Create a new product on WooCommerce.

        ``product`` should contain keys understood by the WooCommerce API:
        ``name``, ``type``, ``regular_price``, ``sku``, ``description``,
        ``stock_quantity``, ``categories``, ``images``.
        """
        url = _build_url("/products")
        params = _auth_params()
        body = {
            "name": product.get("name", ""),
            "type": product.get("type", "simple"),
            "regular_price": str(product.get("price", 0)),
            "sku": product.get("sku", ""),
            "description": product.get("description", ""),
            "stock_quantity": product.get("stock_quantity", 0),
            "manage_stock": True,
        }

        # Optional: categories as ID list
        categories = product.get("categories", [])
        if categories:
            body["categories"] = [{"id": cid} for cid in categories]

        # Optional: images
        images = product.get("images", [])
        if images:
            body["images"] = [{"src": url} for url in images]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, params=params, json=body)
                resp.raise_for_status()
                created = resp.json()
                external_id = str(created["id"])
                logger.info(
                    "Product published on WooCommerce: SKU %s → ID %s",
                    product.get("sku"),
                    external_id,
                )
                return external_id
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Product publish failed: %s",
                exc.response.text[:500],
            )
            raise
        except httpx.RequestError as exc:
            logger.error("Product publish request error: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Utility: verify webhook signature
    # ------------------------------------------------------------------
    @staticmethod
    def verify_webhook_signature(
        payload_body: bytes, signature_header: str, secret: str | None = None
    ) -> bool:
        """Validate an incoming WooCommerce webhook HMAC-SHA256 signature.

        Usage in a FastAPI endpoint::

            is_valid = WooCommerceAdapter.verify_webhook_signature(
                await request.body(),
                request.headers.get("x-wc-webhook-signature", ""),
            )
        """
        key = secret or settings.wood_commerce_consumer_secret
        expected = _webhook_signature(payload_body, key)
        return hmac.compare_digest(expected, signature_header)
