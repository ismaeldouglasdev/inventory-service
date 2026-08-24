"""Admin dashboard endpoints — métricas, stats, full product list."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.models.store_product import StoreProduct
from app.services.circuit_breaker import CircuitBreaker
from app.utils.metrics import generate_metrics, db_query_duration
from app.utils.security import (
    verify_admin_auth,
    verify_api_key,
    rate_limit_admin,
    create_admin_token,
)

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


class LoginRequest(BaseModel):
    password: str


class RenameCategoryRequest(BaseModel):
    frm: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/auth/login", dependencies=[Depends(rate_limit_admin)])
async def admin_login(body: LoginRequest) -> dict[str, Any]:
    """Troca a senha do painel por um JWT de 24h."""
    from app.config import settings

    if body.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Senha incorreta")
    return {
        "access_token": create_admin_token(),
        "token_type": "bearer",
        "expires_in": 86400,
    }


@router.get("/metrics", dependencies=[Depends(verify_api_key), Depends(rate_limit_admin)])
async def get_metrics():
    """Prometheus-formatted metrics for scraping."""
    return PlainTextResponse(generate_metrics().decode(), media_type="text/plain")


@router.get("/stats", response_model=AdminStats, dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)])
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


@router.get("/products", response_model=AdminProductsResponse, dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)])
async def list_admin_products(
    page: int = Query(1, ge=1, le=10000),
    per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, min_length=1, max_length=100),
    has_image: Optional[str] = Query(None),
    store_visible: Optional[str] = Query(None),
    category: Optional[str] = Query(None, max_length=100),
    sort: Optional[str] = Query(None, pattern="^(name|price|stock|ospos_id)$"),
    order: Optional[str] = Query("asc", pattern="^(asc|desc)$"),
    session: AsyncSession = Depends(get_session),
):
    query = select(StoreProduct)

    if search:
        query = query.where(StoreProduct.name.ilike(f"%{search}%"))

    if has_image is not None:
        val = has_image.lower() in ("true", "1", "yes")
        if val:
            query = query.where(StoreProduct.image_url.isnot(None))
        else:
            query = query.where(StoreProduct.image_url.is_(None))

    if store_visible is not None:
        val = store_visible.lower() in ("true", "1", "yes")
        query = query.where(StoreProduct.store_visible == val)

    if category:
        query = query.where(StoreProduct.category.ilike(f"%{category}%"))

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    total_pages = max(1, -(-total // per_page))  # ceil division

    # Sort
    sort_col = {
        "name": StoreProduct.name,
        "price": StoreProduct.price,
        "stock": StoreProduct.stock,
        "ospos_id": StoreProduct.ospos_id,
    }.get(sort or "ospos_id", StoreProduct.ospos_id)
    if order == "desc":
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc()).offset((page - 1) * per_page).limit(per_page)
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


@router.post("/images/map", dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)])
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


# ── Product detail + CRUD ────────────────────────────────────────────────


class ProductDetailOut(BaseModel):
    id: int
    ospos_id: int
    name: str
    sku: str
    description: str
    category: str
    price: float
    stock: int
    image_url: Optional[str] = None
    store_visible: bool

    model_config = {"from_attributes": True}


class ProductUpdateRequest(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    description: Optional[str] = None
    category: Optional[str] = None
    store_visible: Optional[bool] = None
    image_url: Optional[str] = None


class RotateRequest(BaseModel):
    degrees: int = 90


class CropRequest(BaseModel):
    x: int
    y: int
    width: int
    height: int


class InpaintRequest(BaseModel):
    mask_base64: str
    prompt: str = "remove price tag, fill with product background"


def _get_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent / "data"


def _get_image_filename(product: StoreProduct) -> str:
    if not product.image_url:
        raise HTTPException(400, "Product has no image")
    return product.image_url.split("/")[-1]


def _get_image_path(product: StoreProduct) -> Path:
    filename = _get_image_filename(product)
    return _get_data_dir() / "images" / filename


def _backup_image(img_path: Path) -> None:
    originals = img_path.parent / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    backup = originals / img_path.name
    if not backup.exists():
        import shutil
        shutil.copy2(img_path, backup)
        logger.info("Backed up original: %s", backup)


async def _r2_backup(product: StoreProduct) -> None:
    from app.services import r2_storage
    filename = _get_image_filename(product)
    r2_key = filename
    r2_orig_key = f"originals/{filename}"
    if r2_storage.exists(r2_key) and not r2_storage.exists(r2_orig_key):
        data = r2_storage.download(r2_key)
        if data:
            r2_storage.upload(r2_orig_key, data, r2_storage.get_content_type(filename))
            logger.info("R2 backed up original: %s", r2_orig_key)


async def _r2_upload(product: StoreProduct, data: bytes) -> None:
    from app.services import r2_storage
    filename = _get_image_filename(product)
    r2_key = filename
    r2_storage.upload(r2_key, data, r2_storage.get_content_type(filename))
    logger.info("R2 uploaded: %s (%d bytes)", r2_key, len(data))


def _local_save(img_path: Path, data: bytes) -> None:
    img_path.write_bytes(data)


@router.get("/products/{product_id}", dependencies=[Depends(verify_admin_auth)])
async def get_admin_product(product_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")
    return ProductDetailOut.model_validate(product)


@router.put("/products/{product_id}", dependencies=[Depends(verify_admin_auth)])
async def update_admin_product(
    product_id: int,
    body: ProductUpdateRequest,
    session: AsyncSession = Depends(get_session),
):
    from datetime import datetime, timezone
    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(422, "No fields to update")

    for field, value in updates.items():
        setattr(product, field, value)
    product.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(product)

    return ProductDetailOut.model_validate(product)


@router.post("/products/{product_id}/image/rotate", dependencies=[Depends(verify_admin_auth)])
async def rotate_product_image(
    product_id: int,
    body: RotateRequest,
    session: AsyncSession = Depends(get_session),
):
    from PIL import Image
    from app.services import r2_storage
    import io

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")

    filename = _get_image_filename(product)
    r2_key = filename

    img_bytes = r2_storage.download(r2_key)
    if img_bytes is None:
        img_path = _get_image_path(product)
        if not img_path.exists():
            raise HTTPException(404, "Image not found")
        img_bytes = img_path.read_bytes()

    await _r2_backup(product)
    _backup_image(_get_image_path(product))

    with Image.open(io.BytesIO(img_bytes)) as img:
        rotated = img.rotate(-body.degrees, expand=True)
        buf = io.BytesIO()
        rotated.save(buf, format=Path(filename).suffix.upper().lstrip(".") or "PNG")
        result_bytes = buf.getvalue()

    r2_storage.upload(r2_key, result_bytes, r2_storage.get_content_type(filename))
    _local_save(_get_image_path(product), result_bytes)

    return {"success": True, "degrees": body.degrees, "filename": filename}


@router.post("/products/{product_id}/image/crop", dependencies=[Depends(verify_admin_auth)])
async def crop_product_image(
    product_id: int,
    body: CropRequest,
    session: AsyncSession = Depends(get_session),
):
    from PIL import Image
    from app.services import r2_storage
    import io

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")

    filename = _get_image_filename(product)
    r2_key = filename

    img_bytes = r2_storage.download(r2_key)
    if img_bytes is None:
        img_path = _get_image_path(product)
        if not img_path.exists():
            raise HTTPException(404, "Image not found")
        img_bytes = img_path.read_bytes()

    await _r2_backup(product)
    _backup_image(_get_image_path(product))

    with Image.open(io.BytesIO(img_bytes)) as img:
        box = (body.x, body.y, body.x + body.width, body.y + body.height)
        cropped = img.crop(box)
        buf = io.BytesIO()
        cropped.save(buf, format=Path(filename).suffix.upper().lstrip(".") or "PNG")
        result_bytes = buf.getvalue()

    r2_storage.upload(r2_key, result_bytes, r2_storage.get_content_type(filename))
    _local_save(_get_image_path(product), result_bytes)

    return {"success": True, "crop": {"x": body.x, "y": body.y, "w": body.width, "h": body.height}}


@router.post("/products/{product_id}/image/inpaint", dependencies=[Depends(verify_admin_auth)])
async def inpaint_product_image(
    product_id: int,
    body: InpaintRequest,
    session: AsyncSession = Depends(get_session),
):
    import httpx
    from app.services import r2_storage

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")

    filename = _get_image_filename(product)
    r2_key = filename

    img_bytes = r2_storage.download(r2_key)
    if img_bytes is None:
        img_path = _get_image_path(product)
        if not img_path.exists():
            raise HTTPException(404, "Image not found")
        img_bytes = img_path.read_bytes()

    await _r2_backup(product)
    _backup_image(_get_image_path(product))

    ext = Path(filename).suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    img_b64 = base64.b64encode(img_bytes).decode()

    router_url = os.environ.get("INPAINT_URL", "http://localhost:20131/v1/images/generations")
    router_key = os.environ.get("INPAINT_KEY", "")

    payload = {
        "model": "cf/@cf/runwayml/stable-diffusion-v1-5-inpainting",
        "prompt": body.prompt,
        "image": f"data:{mime};base64,{img_b64}",
        "mask": f"data:image/png;base64,{body.mask_base64}",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            router_url,
            json=payload,
            headers={"Authorization": f"Bearer {router_key}"},
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Inpainting API error: {resp.text[:200]}")

    data = resp.json()
    if "data" not in data or not data["data"]:
        raise HTTPException(502, "Inpainting returned no result")

    item = data["data"][0]
    if "b64_json" in item:
        result_bytes = base64.b64decode(item["b64_json"])
    elif "url" in item:
        async with httpx.AsyncClient(timeout=30.0) as client:
            img_resp = await client.get(item["url"])
            result_bytes = img_resp.content
    else:
        raise HTTPException(502, "Inpainting returned unexpected format")

    r2_storage.upload(r2_key, result_bytes, r2_storage.get_content_type(filename))
    _local_save(_get_image_path(product), result_bytes)

    return {"success": True, "filename": filename}


@router.post("/products/{product_id}/image/restore", dependencies=[Depends(verify_admin_auth)])
async def restore_product_image(
    product_id: int,
    session: AsyncSession = Depends(get_session),
):
    import shutil
    from app.services import r2_storage

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")

    filename = _get_image_filename(product)
    r2_key = filename
    r2_orig_key = f"originals/{filename}"

    r2_orig_data = r2_storage.download(r2_orig_key)
    if r2_orig_data:
        r2_storage.upload(r2_key, r2_orig_data, r2_storage.get_content_type(filename))
        _local_save(_get_image_path(product), r2_orig_data)
        return {"success": True, "restored": filename, "source": "r2"}

    img_path = _get_image_path(product)
    originals = img_path.parent / "originals"
    backup = originals / img_path.name

    if backup.exists():
        shutil.copy2(backup, img_path)
        return {"success": True, "restored": filename, "source": "local"}

    raise HTTPException(404, "No original backup found for this image")


@router.post("/categories/rename", dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)])
async def rename_category(body: RenameCategoryRequest, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Renomeia uma categoria em todos os produtos (bulk update)."""
    if not body.frm.strip() or not body.to.strip():
        raise HTTPException(422, "Categoria não pode ser vazia")
    if body.frm == body.to:
        raise HTTPException(422, "'from' e 'to' devem ser diferentes")

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.category == body.frm)
    )
    products = result.scalars().all()
    now = datetime.now(timezone.utc)
    for product in products:
        product.category = body.to
        product.updated_at = now
    await session.commit()
    logger.info("Category renamed %r → %r (%d products)", body.frm, body.to, len(products))
    return {"updated": len(products), "from": body.frm, "to": body.to}


# ── Analytics (product views — JSONL, sem migration) ─────────────────────

VIEWS_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "product_views.jsonl"


class ProductViewOut(BaseModel):
    product_id: int
    name: str
    views: int


class AdminAnalyticsOut(BaseModel):
    total_views: int
    unique_products: int
    views_today: int
    top: list[ProductViewOut]


@router.get("/analytics", response_model=AdminAnalyticsOut, dependencies=[Depends(verify_admin_auth)])
async def admin_analytics(days: int = Query(30, ge=1, le=365), session: AsyncSession = Depends(get_session)):
    """Agrega visualizações de produto registradas em data/product_views.jsonl."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    per_product: dict[int, int] = {}
    total = 0
    today = 0
    try:
        with open(VIEWS_LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            blob = f.read().decode("utf-8", errors="ignore")
            if size > 256 * 1024:
                blob = blob.split("\n", 1)[-1]
        for line in blob.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                ts_raw = str(event.get("ts", ""))
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                pid = int(event.get("product_id", 0))
            except Exception:
                continue
            if pid <= 0:
                continue
            per_product[pid] = per_product.get(pid, 0) + 1
            total += 1
            if ts.strftime("%Y-%m-%d") == today_str:
                today += 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("analytics read failed: %s", exc)

    top_ids = sorted(per_product.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top: list[ProductViewOut] = []
    for pid, count in top_ids:
        row = await session.execute(
            select(StoreProduct.name).where(StoreProduct.id == pid)
        )
        name = row.scalar_one_or_none()
        top.append(ProductViewOut(product_id=pid, name=name or f"#{pid}", views=count))

    return AdminAnalyticsOut(
        total_views=total,
        unique_products=len(per_product),
        views_today=today,
        top=top,
    )
