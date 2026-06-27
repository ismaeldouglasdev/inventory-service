"""Shopee OAuth endpoints — auth URL + callback.

Flow:
  1. GET /v1/shopee/auth-url → returns the Shopee partner auth URL
  2. Seller visits URL, authorises app
  3. Shopee redirects to /v1/shopee/callback?code=...&shop_id=...
  4. Token exchanged and stored
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.adapters.implementations.shopee import ShopeeAdapter, ShopeeTokenStore, _token_store
from app.adapters.registry import AdapterRegistry
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shopee", tags=["shopee"])

_registry: AdapterRegistry | None = None


def _set_registry(r: AdapterRegistry) -> None:
    global _registry
    _registry = r


@router.get("/auth-url")
async def auth_url() -> dict[str, str]:
    """Return the Shopee partner auth URL for OAuth flow."""
    if not settings.shopee_partner_id or not settings.shopee_api_key:
        raise HTTPException(
            status_code=503,
            detail="Shopee not configured (SHOPEE_PARTNER_ID / SHOPEE_API_KEY)",
        )
    return {"auth_url": ShopeeAdapter.auth_url()}


@router.get("/callback")
async def callback(
    code: str = Query(...),
    shop_id: int = Query(..., alias="shop_id"),
) -> dict[str, Any]:
    """Handle Shopee OAuth callback — exchange code for tokens."""
    try:
        data = await ShopeeAdapter.exchange_code(code, shop_id)
        return {
            "status": "ok",
            "access_token": data.get("access_token", "")[:20] + "...",
            "shop_id": data.get("shop_id", shop_id),
            "expires_in": data.get("expires_in", 0),
        }
    except Exception as exc:
        logger.error("Shopee OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def status() -> dict[str, Any]:
    """Check if Shopee adapter is authenticated."""
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    return {
        "authenticated": authed,
        "shop_id": _token_store.shop_id,
        "has_access_token": bool(_token_store.access_token),
        "has_refresh_token": bool(_token_store.refresh_token),
    }


@router.post("/refresh")
async def refresh() -> dict[str, bool]:
    """Manually trigger a token refresh."""
    ok = await ShopeeAdapter.refresh_access_token()
    return {"ok": ok}


def _get_adapter() -> ShopeeAdapter:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    try:
        return _registry.get("shopee")  # type: ignore[return-value]
    except Exception:
        raise HTTPException(status_code=503, detail="Shopee adapter not registered")
