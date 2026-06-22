"""Schemas for product-mapping CRUD and adapter interchange."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# ── ProductMapping ─────────────────────────────────────────────────────
class ProductMappingBase(BaseModel):
    sku: str
    ospos_id: int
    has_variants: bool = False
    store_id: str = "principal"


class ProductMappingCreate(ProductMappingBase):
    pass


class ProductMappingRead(ProductMappingBase):
    last_sync_at: Optional[datetime] = None
    last_hash: Optional[str] = None

    model_config = {"from_attributes": True}


# ── ChannelProductMapping ──────────────────────────────────────────────
class ChannelProductMappingBase(BaseModel):
    sku: str
    channel: str
    external_id: str
    external_url: Optional[str] = None
    status: str = "active"


class ChannelProductMappingCreate(ChannelProductMappingBase):
    pass


class ChannelProductMappingRead(ChannelProductMappingBase):
    synced_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── WooCommerce-specific ───────────────────────────────────────────────
class WooCommercePublishRequest(BaseModel):
    """Fields required to publish a product to WooCommerce."""
    name: str
    sku: str
    description: str = ""
    price: float
    stock_quantity: int = 0
    categories: list[int] = []
    images: list[str] = []  # URLs


# ── Generic Publish Request (used by ML, future channels) ────────────
class ChannelPublishRequest(BaseModel):
    """Product data for publishing to any marketplace channel."""
    sku: str
    title: str
    description: str = ""
    price: float
    stock_quantity: int = 1
    condition: str = "new"
    listing_type_id: str = "gold_special"
    category_id: str = ""  # channel-specific category ID
    pictures: list[str] = []
    attributes: list[dict] = []
