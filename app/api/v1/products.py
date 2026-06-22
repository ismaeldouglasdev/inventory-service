"""Product-management and sync endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.product_mapping import ProductMapping
from app.models.channel_product_mapping import ChannelProductMapping
from app.schemas.product import (
    ProductMappingCreate,
    ProductMappingRead,
    ChannelProductMappingRead,
)
from app.services.cdc_agent import CDCAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["products"])

# ── CDC agent reference (injected from main.py) ──────────────────────────

_cdc_agent: CDCAgent | None = None


def _set_cdc_agent(agent: CDCAgent) -> None:
    global _cdc_agent
    _cdc_agent = agent


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProductMappingRead])
async def list_products(
    session: AsyncSession = Depends(get_session),
) -> Any:
    """List all product mappings."""
    result = await session.execute(
        select(ProductMapping).order_by(ProductMapping.sku)
    )
    products = result.scalars().all()
    return products


@router.get("/{sku}", response_model=ProductMappingRead)
async def get_product(
    sku: str,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Get a single product mapping by SKU."""
    result = await session.execute(
        select(ProductMapping).where(ProductMapping.sku == sku)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("", response_model=ProductMappingRead, status_code=201)
async def create_product_mapping(
    payload: ProductMappingCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Create a manual product mapping (for testing / pre-existing products)."""
    existing = await session.execute(
        select(ProductMapping).where(ProductMapping.sku == payload.sku)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="SKU already exists")

    mapping = ProductMapping(
        sku=payload.sku,
        ospos_id=payload.ospos_id,
        has_variants=payload.has_variants,
        store_id=payload.store_id,
    )
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)
    return mapping


@router.get("/{sku}/channels", response_model=list[ChannelProductMappingRead])
async def list_product_channels(
    sku: str,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """List all channel mappings for a given SKU."""
    result = await session.execute(
        select(ChannelProductMapping).where(
            ChannelProductMapping.sku == sku
        )
    )
    mappings = result.scalars().all()
    return mappings


@router.post("/sync", status_code=202)
async def trigger_sync() -> dict[str, Any]:
    """Trigger a CDC poll cycle immediately.

    Returns 202 regardless of whether new events were created.
    """
    if _cdc_agent is None:
        raise HTTPException(status_code=503, detail="CDC Agent not initialised")

    count = await _cdc_agent.run_once()
    return {
        "status": "accepted",
        "events_created": count,
        "message": f"CDC poll completed, {count} event(s) created",
    }
