"""Store-frontend endpoints — reads enriched product data from local DB.

Previously queried OSPOS MySQL directly. Now reads from the local
``store_products`` table which is populated by the sync service
(``StoreSync``).  Products are only visible when ``store_visible=True``
(i.e.  stock > 0 AND an image has been uploaded).
"""

from __future__ import annotations

import logging
import math
import grp
import os
import shutil
from pathlib import Path
from typing import Any, Optional

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import get_session
from app.models.store_product import StoreProduct
from app.services.store_sync import StoreSync
from app.utils.security import verify_api_key, rate_limit_store, rate_limit_write

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/store", tags=["store"])

# ── Image storage ─────────────────────────────────────────────────────────
IMAGE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB

# ── Photo upload history (real-time feedback) ─────────────────────────────
PHOTO_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "photo_uploads.jsonl"


def _log_photo_event(event: dict[str, Any]) -> None:
    """Append one JSON line per photo upload (bounded tail read later)."""
    import json
    from datetime import datetime

    event["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with open(PHOTO_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover
        logger.warning("photo event log append failed: %s", exc)


def _recent_photos(limit: int = 8) -> list[dict[str, Any]]:
    """Return the last ``limit`` photo upload events, newest first."""
    import json

    events: list[dict[str, Any]] = []
    try:
        with open(PHOTO_LOG_PATH, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            if f.tell() > 0:
                f.readline()
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        return []
    except OSError as exc:  # pragma: no cover
        logger.warning("photo event log read failed: %s", exc)
        return []
    return list(reversed(events[-limit:]))

# ── Global sync service reference ────────────────────────────────────────
_store_sync: StoreSync | None = None


def _set_store_sync(sync: StoreSync) -> None:
    global _store_sync
    _store_sync = sync


# ── Response Schemas ─────────────────────────────────────────────────────


class StoreProductOut(BaseModel):
    id: int
    name: str
    description: str
    price: float
    category: str
    sku: str
    stock: int
    image_url: Optional[str] = None

    model_config = {"from_attributes": True}


class StoreProductsResponse(BaseModel):
    products: list[StoreProductOut]
    total: int
    page: int
    per_page: int
    total_pages: int


class StoreCategory(BaseModel):
    name: str
    count: int


# ── Helpers ──────────────────────────────────────────────────────────────


def _normalize_cat(name: str) -> str:
    """Remove acentos, normaliza maiúsculas e plurais para agrupar categorias."""
    import unicodedata
    sem_acento = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", errors="ignore")
        .decode("ascii")
    )
    base = sem_acento.lower().strip()
    if len(base) > 3 and base.endswith("s"):
        base = base[:-1]
    return base


def _r2_image_url(image_url: str) -> str:
    """Rewrite a local /v1/store/images/{key} URL to R2 public URL if configured.

    Falls back to the original URL when R2 is not available or any error occurs.
    """
    if image_url and image_url.startswith("/v1/store/images/"):
        try:
            from app.services import r2_storage
            key = image_url.removeprefix("/v1/store/images/")
            return r2_storage.get_public_url(f"images/{key}")
        except Exception:
            pass
    return image_url


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/products", response_model=StoreProductsResponse, dependencies=[Depends(rate_limit_store)])
async def list_store_products(
    search: Optional[str] = Query(None, min_length=1, max_length=100),
    category: Optional[str] = Query(None, min_length=1, max_length=100),
    page: int = Query(1, ge=1, le=1000),
    per_page: int = Query(20, ge=1, le=100),
    sort: str = Query("name", pattern=r"^(name|price_asc|price_desc)$"),
    has_image: Optional[bool] = Query(None, description="Filter: only products with images"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """List products visible in the store.

    Only returns products where ``store_visible=True``.
    Use ``has_image=true`` to show only products that have an uploaded image.
    """
    query = select(StoreProduct).where(StoreProduct.store_visible == True)  # noqa: E712

    # ── Search ───────────────────────────────────────────────────────
    if search:
        query = query.where(
            StoreProduct.name.ilike(f"%{search}%")
        )

    # ── Image filter ─────────────────────────────────────────────────
    if has_image is True:
        query = query.where(StoreProduct.image_url.isnot(None))

    # ── Category filter ──────────────────────────────────────────────
    if category:
        cat_normalized = _normalize_cat(category)
        # Fetch all distinct categories that normalise to the same key
        cat_subq = (
            select(StoreProduct.category)
            .where(StoreProduct.store_visible == True)  # noqa: E712
            .distinct()
        )
        cat_result = await session.execute(cat_subq)
        all_cats = [row[0] for row in cat_result.fetchall()]
        cats_match = [
            c for c in all_cats
            if _normalize_cat(c) == cat_normalized
        ]
        if cats_match:
            query = query.where(StoreProduct.category.in_(cats_match))
        else:
            # No match — return empty set
            query = query.where(StoreProduct.id < 0)

    # ── Count total ──────────────────────────────────────────────────
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / per_page))

    # ── Sort ─────────────────────────────────────────────────────────
    order_map = {
        "name": StoreProduct.name.asc(),
        "price_asc": StoreProduct.price.asc(),
        "price_desc": StoreProduct.price.desc(),
    }
    order_col = order_map.get(sort, StoreProduct.name.asc())

    # ── Paginate ─────────────────────────────────────────────────────
    offset = (page - 1) * per_page
    query = query.order_by(order_col).offset(offset).limit(per_page)

    result = await session.execute(query)
    products = result.scalars().all()

    items = [StoreProductOut.model_validate(p) for p in products]
    for item in items:
        if item.image_url:
            item.image_url = _r2_image_url(item.image_url)

    return StoreProductsResponse(
        products=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/products/{product_id}", response_model=StoreProductOut, dependencies=[Depends(rate_limit_store)])
async def get_store_product(
    product_id: int,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Get a single product detail by local store product ID."""
    result = await session.execute(
        select(StoreProduct).where(
            StoreProduct.id == product_id,
            StoreProduct.store_visible == True,  # noqa: E712
        )
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    out = StoreProductOut.model_validate(product)
    if out.image_url:
        out.image_url = _r2_image_url(out.image_url)
    return out


@router.get("/categories", response_model=list[StoreCategory], dependencies=[Depends(rate_limit_store)])
async def list_categories(
    normalized: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """List all product categories with product count.

    Only counts products that are store_visible.
    """
    from sqlalchemy import func as sa_func

    query = (
        select(StoreProduct.category, sa_func.count(StoreProduct.id))
        .where(StoreProduct.store_visible == True)  # noqa: E712
        .group_by(StoreProduct.category)
        .order_by(sa_func.count(StoreProduct.id).desc())
    )
    result = await session.execute(query)
    rows = result.fetchall()

    if normalized:
        grupos: dict[str, dict[str, int]] = {}
        for cat_name, cat_count in rows:
            chave = _normalize_cat(cat_name)
            if chave not in grupos:
                grupos[chave] = {"nome": cat_name, "count": 0}
            grupos[chave]["count"] += cat_count
            if cat_count > grupos[chave]["count"]:
                grupos[chave]["nome"] = cat_name
        return [
            StoreCategory(name=g["nome"], count=g["count"])
            for g in grupos.values()
        ]

    return [
        StoreCategory(name=row[0], count=row[1])
        for row in rows
    ]


# ── Image endpoints ──────────────────────────────────────────────────────


@router.get("/images/{filename:path}")
async def serve_product_image(filename: str) -> Any:
    """Serve a product image — R2 first, local fallback."""
    from app.services import r2_storage

    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    r2_key = f"images/{filename}"
    img_bytes = r2_storage.download(r2_key)

    if img_bytes is not None:
        ext = Path(filename).suffix.lower()
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        media_type = media_types.get(ext, "application/octet-stream")
        return Response(
            content=img_bytes,
            media_type=media_type,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    filepath = IMAGE_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    ext = filepath.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        str(filepath),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/ospos-item-images/{filename:path}")
async def serve_ospos_item_image(filename: str) -> Any:
    """Serve an item photo directly from the OSPOS uploads dir.

    Lets another PC on the LAN pull photos written back to OSPOS
    (``public/uploads/item_pics/{item_id}{ext}``) over HTTP.
    """
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = Path(settings.ospos_uploads_dir) / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    ext = filepath.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return FileResponse(str(filepath), media_type=media_types.get(ext, "application/octet-stream"))


@router.get("/sync-total", response_model_exclude_none=True)
async def sync_total(
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    include_deleted: bool = Query(False),
    since: Optional[str] = Query(
        None, description="YYYY-MM-DD HH:MM:SS — get only items with last_modified >= since"
    ),
    response: Response = None,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Full product sync for another PC to consume.

    Returns the entire OSPOS product catalog (paginated) with name, code,
    prices, stock, photo filename, last_modified, etc. Use ``limit``/``offset``
    to page through the whole catalog; read the ``X-Total-Count`` response
    header to know the total.

    The returned ``image_url`` points at ``/v1/store/ospos-item-images/{pic}``
    so the consumer can download the photo over HTTP.

    Light & stateless — no sync, no dedupe, no side effects.
    """
    from app.services import ospos_client

    rows, total = await ospos_client.fetch_items_total(
        limit=limit, offset=offset, include_deleted=include_deleted, since=since
    )

    items = []
    for r in rows:
        pic = r.get("pic_filename")
        last_mod = r.get("last_modified")
        if hasattr(last_mod, "isoformat"):
            last_mod = last_mod.isoformat(timespec="seconds")
        last_mod = str(last_mod) if last_mod else None
        items.append({
            "item_id": r["item_id"],
            "sku": r["item_number"],
            "name": r["name"],
            "category": r["category"],
            "description": r["description"],
            "cost_price": float(r["cost_price"]) if r["cost_price"] is not None else None,
            "unit_price": float(r["unit_price"]) if r["unit_price"] is not None else None,
            "stock": int(r["stock"]) if float(r["stock"] or 0) == int(float(r["stock"] or 0)) else float(r["stock"]),
            "image_url": f"/v1/store/ospos-item-images/{pic}" if pic else None,
            "pic_filename": pic,
            "last_modified": last_mod,
            "deleted": bool(r["deleted"]),
        })

    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Offset"] = str(offset)
        response.headers["Content-Type"] = "application/json"
    return items


@router.post("/products/{product_id}/image", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = Query(True, description="Apply background removal via rembg"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Upload an image for a product.

    Accepts JPG, PNG, WebP, GIF up to 5 MB.
    If ``remove_bg=true`` (default), background is removed automatically
    using rembg before saving.

    The file is saved as ``product_{id}.{ext}`` and the product's
    ``image_url`` is updated in the local DB.  If stock > 0 the product
    becomes ``store_visible=True`` automatically.
    """
    # ── Validate file ───────────────────────────────────────────────
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # ── Verify product exists in local DB ────────────────────────────
    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # ── Read content ────────────────────────────────────────────────
    contents = await file.read()
    if len(contents) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {MAX_IMAGE_SIZE // (1024*1024)} MB.",
        )

    # ── Background removal (if requested) ───────────────────────────
    if remove_bg:
        try:
            from rembg import remove as rembg_remove
            import io
            from PIL import Image

            input_image = Image.open(io.BytesIO(contents))
            output_image = rembg_remove(input_image)

            buf = io.BytesIO()
            output_image.save(buf, format="PNG")
            contents = buf.getvalue()
            ext = ".png"  # PNG preserves transparency
            logger.info("Background removed for product %d", product_id)
        except ImportError:
            logger.warning("rembg not installed — skipping background removal")
        except Exception as exc:
            logger.warning("rembg failed for product %d: %s — saving original", product_id, exc)

    # ── Save file ───────────────────────────────────────────────────
    filename = f"product_{product_id}{ext}"
    filepath = IMAGE_DIR / filename

    from app.services import r2_storage

    # Remove old image with different extension
    for old_ext in ALLOWED_EXTENSIONS:
        old_path = IMAGE_DIR / f"product_{product_id}{old_ext}"
        if old_path.exists() and old_path != filepath:
            old_path.unlink()
        old_r2_key = f"images/product_{product_id}{old_ext}"
        if old_r2_key != f"images/{filename}":
            r2_storage.delete(old_r2_key)

    with open(filepath, "wb") as f:
        f.write(contents)

    r2_storage.upload(f"images/{filename}", contents, r2_storage.get_content_type(filename))

    # ── Update local DB ─────────────────────────────────────────────
    image_url = f"/v1/store/images/{filename}"
    product.image_url = image_url
    product.updated_at = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("UTC"))

    # Auto-mark as store_visible if stock > 0
    if product.stock > 0:
        product.store_visible = True

    await session.commit()

    logger.info("Image uploaded for product %d: %s (%d bytes)", product_id, filename, len(contents))

    # ── Write-back to OSPOS (thumbnail + pic_filename) ──────────────
    # Mirror the image into the OSPOS uploads dir under its item_id
    # naming (e.g. 3913.png) and update ospos_items.pic_filename so the
    # OSPOS item grid / sale screens show the photo too.  If the mapped
    # OSPOS item is deleted, the photo is redirected to the active item
    # carrying the same barcode instead.
    from app.services import ospos_client

    ospos_written = None
    writeback_error = None
    try:
        target_id = await ospos_client.resolve_photo_target(product.ospos_id, product.sku)
        if not target_id:
            writeback_error = "no active OSPOS item (mapped item deleted)"
            logger.error(
                "OSPOS write-back skipped for product %d: no active item "
                "(mapped item_id=%s barcode=%s)",
                product_id, product.ospos_id, product.sku,
            )
        else:
            ospos_fname = f"{target_id}{ext}"
            ospos_uploads = Path(settings.ospos_uploads_dir)
            ospos_uploads.mkdir(parents=True, exist_ok=True)
            dest = ospos_uploads / ospos_fname

            # The dest may already exist owned by another user (e.g. www-data
            # from an OSPOS UI upload).  Unlink first so the copy only needs
            # write permission on the directory.
            if dest.exists():
                dest.unlink()
            shutil.copy2(filepath, dest)
            # Group-writable so both OSPOS (www-data) and the service (ismaiel)
            # can overwrite it later.  The group must be www-data: chmod 664
            # alone is not enough when the file is created as ismael:ismael.
            # NOTE: there is no os.chgrp() in Python — use os.chown() with
            # uid=-1 to keep the owner unchanged.
            try:
                os.chown(dest, -1, grp.getgrnam("www-data").gr_gid)
                os.chmod(dest, 0o664)
            except OSError:
                pass

            await ospos_client.set_pic_filename(target_id, ospos_fname)

            ospos_written = ospos_fname
            logger.info(
                "OSPOS write-back: product %d → OSPOS item %d pic_filename=%s",
                product_id, target_id, ospos_fname,
            )
    except Exception as exc:
        writeback_error = str(exc)
        logger.error("OSPOS write-back failed for product %d: %s", product_id, exc)

    # ── Record + broadcast the photo event (real-time feedback) ──────────
    event: dict[str, Any] = {
        "product_id": product_id,
        "product_name": product.name,
        "ospos_item_id": target_id if ospos_written else None,
        "pic_filename": ospos_written,
        "status": "ok" if ospos_written else "failed",
        "error": writeback_error,
    }
    _log_photo_event(event)
    await _photo_notifier.broadcast({"type": "photo", **event})

    return {
        "success": True,
        "filename": filename,
        "image_url": image_url,
        "background_removed": remove_bg,
        "ospos_pic_filename": ospos_written,
    }


@router.post("/photos/clean", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def clean_product_photo(
    file: UploadFile = File(...),
    item_id: Optional[int] = Query(None, description="OSPOS item id — alvo direto"),
    product_id: Optional[int] = Query(None, description="Store product id — resolve o item via mapeamento"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Substitui a foto de um item OSPOS pela versão com a etiqueta removida.

    A imagem chega já processada (inpaint feito no próprio celular via
    OpenCV.js). Aqui só fazemos o write-back: copia para
    ``uploads/item_pics/{item_id}{ext}`` (chmod/chown www-data), remove o
    thumb antigo e atualiza ``ospos_items.pic_filename``. Se um produto da
    loja estiver vinculado ao item, a imagem local da loja
    (``data/images/product_{id}{ext}``) também é atualizada.

    Um de ``item_id`` ou ``product_id`` é obrigatório (item_id tem prioridade).
    """
    from app.services import ospos_client

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(contents) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {MAX_IMAGE_SIZE // (1024 * 1024)} MB.",
        )

    # ── Resolve o item OSPOS alvo ─────────────────────────────────────
    product = None
    if item_id is None and product_id is None:
        raise HTTPException(status_code=400, detail="Provide item_id or product_id")

    if product_id is not None:
        result = await session.execute(
            select(StoreProduct).where(StoreProduct.id == product_id)
        )
        product = result.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        target_id = await ospos_client.resolve_photo_target(product.ospos_id, product.sku)
        if not target_id:
            raise HTTPException(
                status_code=409,
                detail="No active OSPOS item for this product (mapped item is deleted)",
            )
    else:
        target_id = item_id
        result = await session.execute(
            select(StoreProduct).where(StoreProduct.ospos_id == item_id).limit(1)
        )
        product = result.scalars().first()

    # ── Grava a foto limpa no OSPOS ───────────────────────────────────
    ospos_uploads = Path(settings.ospos_uploads_dir)
    ospos_uploads.mkdir(parents=True, exist_ok=True)
    fname = f"{target_id}{ext}"

    # Backup da foto atual (qualquer extensão) antes de sobrescrever.
    try:
        from datetime import datetime
        bak_dir = Path(__file__).resolve().parent.parent.parent.parent / "data" / "photo_backups"
        bak_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for cur in ospos_uploads.glob(f"{target_id}.*"):
            if cur.suffix.lower() in ALLOWED_EXTENSIONS:
                shutil.copy2(cur, bak_dir / f"{target_id}_{stamp}{cur.suffix.lower()}")
                break
    except OSError as exc:  # pragma: no cover
        logger.warning("photo backup failed for item %s: %s", target_id, exc)

    # Remove versões antigas com outra extensão e o thumb gerado pelo OSPOS.
    for old in ospos_uploads.glob(f"{target_id}.*"):
        if old.suffix.lower() in ALLOWED_EXTENSIONS and old.name != fname:
            try:
                old.unlink()
            except OSError:
                pass
    for old in ospos_uploads.glob(f"{target_id}_thumb.*"):
        try:
            old.unlink()
        except OSError:
            pass

    dest = ospos_uploads / fname
    if dest.exists():
        dest.unlink()
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(contents)
    os.chown(tmp, -1, grp.getgrnam("www-data").gr_gid)
    os.chmod(tmp, 0o664)
    tmp.rename(dest)

    await ospos_client.set_pic_filename(target_id, fname)

    # ── Sincroniza a imagem da loja (se houver produto vinculado) ─────
    local_image = None
    if product is not None:
        try:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            new_path = IMAGE_DIR / f"product_{product.id}{ext}"
            if new_path.exists():
                new_path.unlink()
            new_path.write_bytes(contents)
            os.chmod(new_path, 0o664)
            for old in ALLOWED_EXTENSIONS:
                old_path = IMAGE_DIR / f"product_{product.id}{old}"
                if old_path.exists() and old_path != new_path:
                    old_path.unlink()
            product.image_url = f"/v1/store/images/{new_path.name}"
            product.updated_at = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("UTC"))
            await session.commit()
            local_image = new_path.name
        except Exception as exc:  # pragma: no cover
            logger.warning("loja image update failed for product %s: %s", product.id, exc)

    logger.info("Cleaned photo written back: OSPOS item %s → %s (local %s)", target_id, fname, local_image)

    return {
        "success": True,
        "item_id": target_id,
        "pic_filename": fname,
        "image_url": f"/v1/store/ospos-item-images/{fname}",
        "store_image": local_image,
    }


# ── LaMa (inpainting local no PC via ONNX) ────────────────────────────────

# Serializa a inferência (1 foto por vez) para não estourar a RAM do PC.
_lama_lock = asyncio.Lock()


@router.post("/photos/lama", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def lama_product_photo(
    file: UploadFile = File(...),
    mask: UploadFile = File(...),
    item_id: Optional[int] = Query(None, description="OSPOS item id"),
    product_id: Optional[int] = Query(None, description="Store product id"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Remove a etiqueta via LaMa (ONNX) rodando local no PC.

    Recebe a foto + máscara (branco = área a remover), roda LaMa com
    resolução limitada a ``lama_max_side`` (leve, ~1GB) e devolve o
    resultado em alta resolução como data URI base64 — só a região da
    máscara é substituída (com borda suavizada), o resto fica idêntico.

    NÃO faz write-back no OSPOS: o app envia o resultado para
    ``POST /photos/clean`` quando o usuário salvar.
    """
    import base64
    import io

    import numpy as np
    from PIL import Image, ImageFilter

    if not file.filename or not mask.filename:
        raise HTTPException(status_code=400, detail="file and mask required")
    if (
        Path(file.filename).suffix.lower() not in ALLOWED_EXTENSIONS
        or Path(mask.filename).suffix.lower() not in ALLOWED_EXTENSIONS
    ):
        raise HTTPException(status_code=400, detail="Invalid file type. Allowed: jpg, jpeg, png, webp, gif")

    contents = await file.read()
    mask_bytes = await mask.read()
    if not contents or not mask_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(contents) > MAX_IMAGE_SIZE or len(mask_bytes) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Max {MAX_IMAGE_SIZE // (1024 * 1024)} MB.")

    try:
        original = Image.open(io.BytesIO(contents))
        original.load()
        original = original.convert("RGB")
        msk = Image.open(io.BytesIO(mask_bytes)).convert("L")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")

    # ── Inferência (serializada, em thread) ───────────────────────────
    from app.services import lama_inpainter

    loop = asyncio.get_running_loop()
    async with _lama_lock:
        try:
            result = await loop.run_in_executor(
                None, lambda: lama_inpainter.inpaint_pil(original, msk)
            )
        except Exception as exc:  # pragma: no cover
            logger.error("LaMa inference failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"LaMa inference failed: {exc}")

    # ── Composição em alta resolução (só a região da máscara muda) ────
    full = result.resize(original.size, Image.LANCZOS)
    feather = msk.filter(ImageFilter.GaussianBlur(radius=10))
    m_arr = np.asarray(feather, dtype=np.float32)[..., None] / 255.0
    o_arr = np.asarray(original, dtype=np.float32)
    f_arr = np.asarray(full, dtype=np.float32)
    blended = np.clip(o_arr * (1.0 - m_arr) + f_arr * m_arr, 0, 255).astype(np.uint8)
    final = Image.fromarray(blended, "RGB")

    buf = io.BytesIO()
    final.save(buf, "JPEG", quality=92, optimize=True)
    data_b64 = base64.b64encode(buf.getvalue()).decode()
    logger.info(
        "LaMa inpainting done: %sx%s → %sx%s",
        original.width, original.height, final.width, final.height,
    )
    return {
        "success": True,
        "width": final.width,
        "height": final.height,
        "mime": "image/jpeg",
        "data": "data:image/jpeg;base64," + data_b64,
    }


# ── Photo upload status (real-time feedback) ──────────────────────────────


@router.get("/photos/recent")
async def recent_photo_uploads(
    limit: int = Query(8, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Last ``limit`` photo upload events, newest first.

    Used by the OSPOS items screen (PC) and the mobile status page to
    show, in near real time, that a photo captured by the Loja Capture
    app was saved into the system (write-back to OSPOS done or failed).
    """
    return _recent_photos(limit=limit)


@router.websocket("/photo/ws")
async def photo_websocket(websocket: WebSocket) -> None:
    """Real-time photo-upload notifications for the OSPOS items screen.

    The PC browser connects here; whenever a photo is uploaded via the
    Loja Capture app, the event is broadcast so the items grid can
    refresh and show a toast immediately (no polling).
    """
    await _photo_notifier.connect(websocket)
    try:
        # Send the most recent event immediately on connect (feedback
        # for uploads that already happened).
        last = _recent_photos(limit=1)
        if last:
            await websocket.send_json({"type": "photo", "last": True, **last[0]})

        # Keep connection alive — listen for client pings
        while True:
            try:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await _photo_notifier.disconnect(websocket)


# ── Link existing image ───────────────────────────────────────────────────


class LinkImageRequest(BaseModel):
    filename: str


@router.post("/products/{product_id}/image/link", status_code=200)
async def link_existing_image(
    product_id: int,
    body: LinkImageRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Link an already-uploaded image file to a product.

    The file must already exist in the images directory.  Multiple
    images can be linked to the same product — the last one wins.
    """
    filename = body.filename.strip()
    src = IMAGE_DIR / filename

    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Image file not found: {filename}")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid extension: {ext}")

    result = await session.execute(
        select(StoreProduct).where(StoreProduct.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Copy to convention: product_{id}{ext} (keep original for re-linking)
    dest_name = f"product_{product_id}{ext}"
    dest = IMAGE_DIR / dest_name

    import shutil
    shutil.copy2(src, dest)

    from app.services import r2_storage
    r2_storage.upload(f"images/{dest_name}", src.read_bytes(), r2_storage.get_content_type(dest_name))

    image_url = f"/v1/store/images/{dest_name}"
    product.image_url = image_url
    product.updated_at = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("UTC"))

    if product.stock > 0:
        product.store_visible = True

    await session.commit()

    logger.info("Image linked for product %d: %s", product_id, dest_name)

    return {
        "success": True,
        "filename": dest_name,
        "image_url": image_url,
        "product_id": product_id,
    }


# ── Sync endpoint ────────────────────────────────────────────────────────


@router.post("/sync", status_code=202)
async def trigger_store_sync(
    mode: str = Query("delta", pattern=r"^(full|delta)$"),
    only_with_images: bool = Query(False),
    min_stock: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Trigger a sync from OSPOS into the local store_products table.

    - ``mode=full``: re-sync all products from OSPOS.
    - ``mode=delta``: only sync products changed since last sync (default).
    - ``only_with_images=true``: only import products that already have an
      image file on disk.
    - ``min_stock=N``: minimum stock threshold (default 0).

    Returns 202 (accepted) — the sync runs synchronously but is fast
    enough for most store databases.
    """
    if _store_sync is None:
        raise HTTPException(status_code=503, detail="StoreSync not initialised")

    try:
        if mode == "full":
            stats = await _store_sync.run(
                only_with_images=only_with_images,
                min_stock=min_stock,
            )
        else:
            stats = await _store_sync.run_delta(
                only_with_images=only_with_images,
                min_stock=min_stock,
            )

        return {
            "status": "completed",
            "mode": mode,
            **stats,
        }
    except Exception as exc:
        logger.exception("Store sync failed")
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc


# ── Scan endpoint (barcode → phone) ──────────────────────────────────────


# In-memory store for the last scanned barcode.
# In production this would be Redis or a DB table.
_last_scan: dict[str, Any] = {}

# ── WebSocket connection manager ─────────────────────────────────────────


class ScanNotifier:
    """Manages WebSocket connections for real-time scan notifications.

    Phones connect to the WebSocket and receive scan events as they
    happen — no polling needed.
    """

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)
        logger.info("ScanNotifier: client connected (%d total)", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.remove(ws)
        logger.info("ScanNotifier: client disconnected (%d remaining)", len(self._connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send scan data to all connected clients."""
        dead: list[WebSocket] = []
        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections.remove(ws)
        if dead:
            logger.info("ScanNotifier: cleaned %d dead connection(s)", len(dead))


_scan_notifier = ScanNotifier()

# Same connection manager reused for photo-upload events (OSPOS PC screen).
_photo_notifier = ScanNotifier()


@router.websocket("/scan/ws")
async def scan_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time scan notifications.

    Phones connect here. Whenever the PC scanner registers a scan
    (``POST /store/scan/{barcode}``), the scan data is broadcast
    to all connected clients.
    """
    await _scan_notifier.connect(websocket)
    try:
        # Send the last known scan immediately on connect
        if _last_scan:
            await websocket.send_json({"type": "last", ** _last_scan})

        # Keep connection alive — listen for client pings
        while True:
            try:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await _scan_notifier.disconnect(websocket)


@router.post("/scan/{barcode}")
async def register_scan(
    barcode: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Register a barcode scan from the PC scanner.

    Looks up the product in the local DB by SKU and stores the scan
    so the phone can pick it up (via WebSocket or polling).

    The scan is broadcast to all connected WebSocket clients in real time.
    """
    result = await session.execute(
        select(StoreProduct).where(StoreProduct.sku == barcode)
    )
    matches = result.scalars().all()

    # When the same barcode exists on multiple products (duplicate SKU),
    # resolve to the best record.  Candidates whose OSPOS item is deleted
    # are deprioritized so the photo/scan follows the live product.
    if matches:
        if len(matches) == 1:
            product = matches[0]
        else:
            from app.services import ospos_client
            deleted = await ospos_client.item_deleted_map([m.ospos_id for m in matches])
            active = [m for m in matches if m.ospos_id in deleted and not deleted[m.ospos_id]]

            from app.services.duplicate_rule import pick_best_duplicate
            product = pick_best_duplicate(active or list(matches))
    else:
        product = None

    scan_data = {
        "barcode": barcode,
        "product_id": product.id if product else None,
        "product_name": product.name if product else None,
        "found": product is not None,
    }

    if not product:
        logger.info("Scan: barcode %s not found in store_products", barcode)
    else:
        logger.info("Scan: barcode %s → product %d (%s)", barcode, product.id, product.name)
        scan_data["product_id"] = product.id
        scan_data["product_name"] = product.name
        scan_data["sku"] = product.sku
        scan_data["price"] = product.price
        scan_data["stock"] = product.stock

    _last_scan.clear()
    _last_scan.update(scan_data)

    # Broadcast to all connected phones
    await _scan_notifier.broadcast({"type": "scan", **scan_data})

    return scan_data


@router.get("/scan/last")
async def get_last_scan() -> dict[str, Any]:
    """Get the last registered barcode scan (phone polls this)."""
    if not _last_scan:
        return {
            "type": "last",
            "barcode": "",
            "product_id": None,
            "product_name": None,
            "found": False,
        }
    return _last_scan


class ClientLogEntry(BaseModel):
    level: str = "error"
    message: str
    screen: str = ""
    device: str = ""
    timestamp: str = ""


@router.post("/log")
async def client_log(entry: ClientLogEntry) -> dict[str, str]:
    """Receive error logs from the Android app."""
    logger.warning(
        "[APP:%s] [%s] %s | device=%s screen=%s",
        entry.level.upper(),
        entry.timestamp or "?",
        entry.message,
        entry.device,
        entry.screen,
    )
    return {"status": "logged"}
