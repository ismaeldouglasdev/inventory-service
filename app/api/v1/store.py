"""Store-frontend endpoints — reads enriched product data from local DB.

Previously queried OSPOS MySQL directly. Now reads from the local
``store_products`` table which is populated by the sync service
(``StoreSync``).  Products are only visible when ``store_visible=True``
(i.e.  stock > 0 AND an image has been uploaded).
"""

from __future__ import annotations

import logging
import math
import os
import shutil
from pathlib import Path
from typing import Any, Optional

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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

    return StoreProductsResponse(
        products=[StoreProductOut.model_validate(p) for p in products],
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
    return StoreProductOut.model_validate(product)


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
    """Serve a product image from local storage."""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

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

    return FileResponse(str(filepath), media_type=media_type)


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

    # Remove old image with different extension
    for old_ext in ALLOWED_EXTENSIONS:
        old_path = IMAGE_DIR / f"product_{product_id}{old_ext}"
        if old_path.exists() and old_path != filepath:
            old_path.unlink()

    with open(filepath, "wb") as f:
        f.write(contents)

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
    try:
        target_id = await ospos_client.resolve_photo_target(product.ospos_id, product.sku)
        if not target_id:
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
            # can overwrite it later.
            try:
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
        logger.error("OSPOS write-back failed for product %d: %s", product_id, exc)

    return {
        "success": True,
        "filename": filename,
        "image_url": image_url,
        "background_removed": remove_bg,
        "ospos_pic_filename": ospos_written,
    }


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
