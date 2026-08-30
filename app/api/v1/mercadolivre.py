"""Mercado Livre OAuth and product-publish endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.implementations.mercadolivre import (
    MercadoLivreAdapter,
    _token_store,
)
from app.adapters.registry import AdapterRegistry
from app.config import settings
from app.database import get_session
from app.models.product_mapping import ProductMapping
from app.models.channel_product_mapping import ChannelProductMapping
from app.schemas.product import ChannelPublishRequest
from app.services.event_processor import create_event
from app.utils.security import verify_admin_auth, verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mercadolivre", tags=["mercadolivre"])

# Registry reference (injected from main.py)
_registry: AdapterRegistry | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


def _get_adapter() -> MercadoLivreAdapter:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    try:
        adapter = _registry.get("mercadolivre")
        if not isinstance(adapter, MercadoLivreAdapter):
            raise HTTPException(status_code=500, detail="Wrong adapter type")
        return adapter
    except Exception:
        raise HTTPException(status_code=503, detail="ML adapter not registered")


# ── OAuth ────────────────────────────────────────────────────────────────


@router.get("/auth-url")
async def auth_url() -> dict[str, str]:
    """Return the URL a seller must visit to authorise the app."""
    url = MercadoLivreAdapter.auth_url()
    return {"auth_url": url}


@router.get("/callback")
async def oauth_callback(
    code: str = Query(..., description="Authorization code from ML"),
) -> dict[str, Any]:
    """Exchange an OAuth code for access/refresh tokens."""
    try:
        data = await MercadoLivreAdapter.exchange_code(code)
        return {
            "status": "ok",
            "access_token": data.get("access_token", "")[:20] + "...[hidden]",
            "refresh_token": data.get("refresh_token", "")[:20] + "...[hidden]",
            "user_id": data.get("user_id"),
            "expires_in": data.get("expires_in"),
        }
    except Exception as exc:
        logger.error("ML OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def ml_status() -> dict[str, Any]:
    """Check whether the ML adapter is authenticated."""
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    return {
        "authenticated": authed,
        "user_id": _token_store.user_id,
        "has_refresh_token": bool(_token_store.refresh_token),
    }


@router.get("/token-debug", dependencies=[Depends(verify_admin_auth)])
async def token_debug() -> dict[str, Any]:
    """Return whether the ML adapter holds tokens (no raw values).

    Security fix (29/ago/2026): this endpoint previously returned the raw
    access_token/refresh_token in plaintext with NO authentication. It is now
    admin-only and returns only booleans — never the token values.
    """
    return {
        "has_access_token": bool(_token_store.access_token),
        "has_refresh_token": bool(_token_store.refresh_token),
        "ml_user_id": _token_store.user_id,
    }


# ── Product publishing ───────────────────────────────────────────────────


@router.post("/publish", status_code=201, dependencies=[Depends(verify_api_key)])
async def publish_product(
    body: ChannelPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Publish a product to Mercado Livre.

    Security fix (29/ago/2026): this endpoint previously had NO authentication
    — anyone could publish items to ML. Now requires a valid X-API-Key.

    Accepts full product data in the request body, publishes directly
    to ML, saves the channel mapping, and creates a tracking event
    in the EventStore.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone

    sku = body.sku

    # 1. Ensure ProductMapping exists (create if not)
    result = await session.execute(
        select(ProductMapping).where(ProductMapping.sku == sku)
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        mapping = ProductMapping(
            sku=sku,
            ospos_id=0,
            has_variants=False,
            store_id="principal",
        )
        session.add(mapping)

    # 2. Check if already published
    result = await session.execute(
        select(ChannelProductMapping).where(
            ChannelProductMapping.sku == sku,
            ChannelProductMapping.channel == "mercadolivre",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return {
            "status": "already_published",
            "channel": "mercadolivre",
            "external_id": existing.external_id,
            "external_url": existing.external_url,
        }

    # 3. Publish via adapter
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    if not authed:
        raise HTTPException(
            status_code=401,
            detail="ML not authenticated. Visit /v1/mercadolivre/auth-url first.",
        )

    product_data = {
        "title": body.title,
        "sku": sku,
        "description": body.description,
        "price": body.price,
        "cost_price": body.cost_price,
        "stock_quantity": body.stock_quantity,
        "condition": body.condition,
        "listing_type_id": body.listing_type_id,
        "category_id": body.category_id or settings.ml_default_category,
        "pictures": body.pictures,
        "attributes": body.attributes,
    }

    try:
        external_id = await adapter.publish_product(product_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ML publish failed: {exc}")

    # 4. Save channel mapping
    channel_mapping = ChannelProductMapping(
        sku=sku,
        channel="mercadolivre",
        external_id=external_id,
        external_url=f"https://www.mercadolivre.com.br/items/{external_id}",
        status="active",
        synced_at=datetime.now(timezone.utc),
    )
    session.add(channel_mapping)

    # 5. Create tracking event in EventStore
    event = create_event(
        event_type="product.created",
        payload=product_data,
        sku=sku,
        channel="mercadolivre",
    )
    session.add(event)

    await session.commit()

    return {
        "status": "published",
        "channel": "mercadolivre",
        "external_id": external_id,
        "external_url": channel_mapping.external_url,
        "event_id": event.id,
    }
