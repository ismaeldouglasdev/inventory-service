"""WooCommerce status and product-publish endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.implementations.woocommerce import WooCommerceAdapter
from app.adapters.registry import AdapterRegistry
from app.config import settings
from app.database import get_session
from app.models.product_mapping import ProductMapping
from app.models.channel_product_mapping import ChannelProductMapping
from app.schemas.product import ChannelPublishRequest
from app.services.event_processor import create_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/woocommerce", tags=["woocommerce"])

_registry: AdapterRegistry | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


def _get_adapter() -> WooCommerceAdapter:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    try:
        adapter = _registry.get("woocommerce")
        if not isinstance(adapter, WooCommerceAdapter):
            raise HTTPException(status_code=500, detail="Wrong adapter type")
        return adapter
    except Exception:
        raise HTTPException(status_code=503, detail="WooCommerce adapter not registered")


@router.get("/status")
async def wc_status() -> dict[str, Any]:
    """Check whether the WooCommerce adapter is authenticated."""
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    return {
        "authenticated": authed,
        "store_url": settings.wood_commerce_url or "(not configured)",
    }


@router.post("/publish", status_code=201)
async def publish_product(
    body: ChannelPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Publish a product to WooCommerce.

    Accepts full product data in the request body, publishes directly
    to WooCommerce, saves the channel mapping, and creates a tracking
    event in the EventStore.
    """
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
            ChannelProductMapping.channel == "woocommerce",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return {
            "status": "already_published",
            "channel": "woocommerce",
            "external_id": existing.external_id,
            "external_url": existing.external_url,
        }

    # 3. Publish via adapter
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    if not authed:
        raise HTTPException(
            status_code=401,
            detail="WooCommerce not authenticated. Check WOOD_COMMERCE_URL and credentials.",
        )

    product_data = {
        "name": body.title,
        "sku": sku,
        "description": body.description,
        "price": body.price,
        "stock_quantity": body.stock_quantity,
        "type": "simple",
        "images": body.pictures,
    }

    try:
        external_id = await adapter.publish_product(product_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WooCommerce publish failed: {exc}")

    # 4. Save channel mapping
    store_domain = settings.wood_commerce_url.rstrip("/")
    channel_mapping = ChannelProductMapping(
        sku=sku,
        channel="woocommerce",
        external_id=external_id,
        external_url=f"{store_domain}/wp-admin/post.php?post={external_id}&action=edit",
        status="active",
        synced_at=datetime.now(timezone.utc),
    )
    session.add(channel_mapping)

    # 5. Create tracking event in EventStore
    event = create_event(
        event_type="product.created",
        payload=product_data,
        sku=sku,
        channel="woocommerce",
    )
    session.add(event)

    await session.commit()

    return {
        "status": "published",
        "channel": "woocommerce",
        "external_id": external_id,
        "external_url": channel_mapping.external_url,
        "event_id": event.id,
    }
