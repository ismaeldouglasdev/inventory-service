"""Admin dashboard endpoints — métricas, stats, full product list."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.models.store_product import StoreProduct
from app.services.circuit_breaker import CircuitBreaker
from app.utils.metrics import generate_metrics, db_query_duration

# ── Injected references (set by main.py during lifespan) ────────────────
_registry: AdapterRegistry | None = None
_circuit_breaker: CircuitBreaker | None = None


def _set_registry(r: AdapterRegistry) -> None:
    global _registry
    _registry = r


def _set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _circuit_breaker
    _circuit_breaker = cb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Schemas ──────────────────────────────────────────────────────────────


class AdminStats(BaseModel):
    total_products: int
    visible_products: int
    with_images: int
    in_stock: int
    categories: int
    db_size_mb: float
    images_count: int
    images_size_mb: float


class ProductAdminOut(BaseModel):
    id: int
    ospos_id: int
    name: str
    sku: str
    category: str
    price: float
    stock: int
    image_url: Optional[str] = None
    store_visible: bool

    model_config = {"from_attributes": True}


class AdminProductsResponse(BaseModel):
    products: list[ProductAdminOut]
    total: int
    page: int
    per_page: int
    total_pages: int


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/metrics")
async def get_metrics():
    """Prometheus-formatted metrics for scraping."""
    return PlainTextResponse(generate_metrics().decode(), media_type="text/plain")


@router.get("/stats", response_model=AdminStats)
async def get_admin_stats(session: AsyncSession = Depends(get_session)):
    """Store statistics for the admin dashboard."""
    # Product counts
    import time as _time
    _t0 = _time.time()
    total = await session.scalar(select(func.count(StoreProduct.id))) or 0
    db_query_duration.labels(query="count_products").observe(_time.time() - _t0)

    visible = await session.scalar(
        select(func.count(StoreProduct.id)).where(StoreProduct.store_visible == True)
    ) or 0
    with_img = await session.scalar(
        select(func.count(StoreProduct.id)).where(StoreProduct.image_url.isnot(None))
    ) or 0
    in_stock = await session.scalar(
        select(func.count(StoreProduct.id)).where(StoreProduct.stock > 0)
    ) or 0

    # Categories
    cat_count = await session.scalar(
        select(func.count()).select_from(
            select(StoreProduct.category).distinct().subquery()
        )
    ) or 0

    # DB size
    db_path = Path(__file__).resolve().parent.parent.parent / "data" / "inventory.db"
    db_size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0

    # Images
    img_dir = Path(__file__).resolve().parent.parent.parent / "data" / "images"
    if img_dir.exists():
        imgs = list(img_dir.glob("*"))
        images_count = sum(1 for f in imgs if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
        images_size_mb = sum(f.stat().st_size for f in imgs if f.is_file()) / (1024 * 1024)
    else:
        images_count = 0
        images_size_mb = 0

    return AdminStats(
        total_products=total or 0,
        visible_products=visible or 0,
        with_images=with_img or 0,
        in_stock=in_stock or 0,
        categories=cat_count or 0,
        db_size_mb=round(db_size_mb, 2),
        images_count=images_count,
        images_size_mb=round(images_size_mb, 2),
    )


@router.get("/products", response_model=AdminProductsResponse)
async def list_admin_products(
    page: int = Query(1, ge=1, le=10000),
    per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, min_length=1, max_length=100),
    has_image: Optional[bool] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """Full product list (all products, admin view)."""
    query = select(StoreProduct)

    if search:
        query = query.where(StoreProduct.name.ilike(f"%{search}%"))
    if has_image is True:
        query = query.where(StoreProduct.image_url.isnot(None))
    elif has_image is False:
        query = query.where(StoreProduct.image_url.is_(None))

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    total_pages = max(1, -(-total // per_page))  # ceil division

    # Sort by ospos_id for consistent ordering
    query = query.order_by(StoreProduct.ospos_id).offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(query)
    products = result.scalars().all()

    return AdminProductsResponse(
        products=[ProductAdminOut.model_validate(p) for p in products],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/health/detailed")
async def detailed_health(session: AsyncSession = Depends(get_session)):
    """Detailed system health overview."""
    # DB check
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    # Images dir
    img_dir = Path(__file__).resolve().parent.parent.parent / "data" / "images"
    img_dir_ok = img_dir.exists()

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "error",
        "images_directory": "ok" if img_dir_ok else "missing",
        "version": "0.1.0",
        "uptime_seconds": time.time() - _start_time if _start_time else 0,
    }


_start_time: float = time.time()


class ImageMapRequest(BaseModel):
    photo_filename: str
    item_id: int


@router.post("/images/map")
async def map_product_image(
    req: ImageMapRequest,
    session: AsyncSession = Depends(get_session),
):
    """Map a high-res photo from desktop_photos/ to a product by item_id.
    
    Copies the photo to data/images/product_{item_id}.jpg and updates
    the store_products.image_url.
    """
    import shutil

    # Validate product exists
    result = await session.execute(
        select(StoreProduct).where(StoreProduct.ospos_id == req.item_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product with ospos_id={req.item_id} not found")

    # Find the source photo
    src = Path(__file__).resolve().parent.parent.parent / "data" / "images" / "desktop_photos" / req.photo_filename
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Photo {req.photo_filename} not found in desktop_photos/")

    ext = src.suffix.lower()
    dest_name = f"product_{req.item_id}{ext}"
    dest = src.parent.parent / dest_name

    shutil.copy2(src, dest)
    logger.info("Mapped %s → product_%d%s", req.photo_filename, req.item_id, ext)

    # Update DB
    image_url = f"/v1/store/images/{dest_name}"
    product.image_url = image_url
    product.store_visible = True
    product.updated_at = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("UTC"))
    await session.commit()

    return {"success": True, "image_url": image_url, "product_id": product.id, "ospos_id": req.item_id}
