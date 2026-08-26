"""Mobile stock-app endpoints (Estoque) — direct read/write on OSPOS MySQL.

Powers ``static/estoque.html`` (phone): product search, item detail,
full editing, creation of products WITHOUT a barcode and photo capture
with write-back into OSPOS (``item_pics/{id}.{ext}`` + ``pic_filename``).

All writes go straight to MySQL (ospos_items / ospos_item_quantities)
and bump ``last_modified`` so the regular delta sync picks the changes
up for the online store.
"""

from __future__ import annotations

import grp
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.services import ospos_client
from app.utils.security import verify_api_key, rate_limit_write

# Reuse the storage layout + real-time photo feedback from the store API.
from app.api.v1.store import (
    ALLOWED_EXTENSIONS,
    IMAGE_DIR,
    MAX_IMAGE_SIZE,
    _log_photo_event,
    _photo_notifier,
    _item_update_notifier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/estoque", tags=["estoque"])

# Columns the app may edit (whitelist — never expose arbitrary SQL updates).
_EDITABLE_TEXT = {"name", "item_number", "category", "description"}
_EDITABLE_NUM = {"cost_price", "unit_price", "reorder_level"}

_ITEM_COLS = (
    "i.item_id, i.name, i.item_number, i.category, i.description, "
    "i.cost_price, i.unit_price, i.reorder_level, i.pic_filename, "
    "COALESCE(q.quantity, 0) AS quantity, "
    "COALESCE(q.stock_status, 0) AS stock_status"
)

_ITEM_FROM = (
    "ospos_items i "
    "LEFT JOIN ospos_item_quantities q ON q.item_id = i.item_id AND q.location_id = 1 "
)


class ItemCreate(BaseModel):
    name: str
    item_number: Optional[str] = None
    category: str = ""
    description: str = ""
    unit_price: float = 0.0
    cost_price: float = 0.0
    quantity: float = 0.0
    reorder_level: float = 0.0


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    item_number: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[float] = None
    cost_price: Optional[float] = None
    reorder_level: Optional[float] = None
    quantity: Optional[float] = None


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(40, ge=1, le=100),
) -> dict[str, Any]:
    """Search active OSPOS items by name or barcode fragment."""
    term = q.strip()
    rows = await _search_sql(term, limit)
    return {"items": [_row_dict(r) for r in rows]}


@router.get("/item/{item_id}")
async def get_item(item_id: int) -> dict[str, Any]:
    """Full detail of one active OSPOS item."""
    row = await _fetch_one(
        f"SELECT {_ITEM_COLS} FROM {_ITEM_FROM} WHERE i.item_id = %s AND i.deleted = 0",
        (item_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    return _row_dict(row)


@router.post("/item", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def create_item(payload: ItemCreate) -> dict[str, Any]:
    """Create an OSPOS product — designed for products WITHOUT a barcode.

    ``item_number`` is optional; when given it must not collide with an
    existing active item.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nome é obrigatório")

    barcode = (payload.item_number or "").strip() or None
    if barcode:
        dup = await _fetch_one(
            "SELECT item_id FROM ospos_items WHERE item_number = %s AND deleted = 0 LIMIT 1",
            (barcode,),
        )
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"Código {barcode} já existe (produto #{dup[0]})",
            )

    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ospos_items
                    (name, item_number, category, description,
                     cost_price, unit_price, reorder_level, receiving_quantity,
                     allow_alt_description, is_serialized, stock_type, item_type,
                     deleted, last_modified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 1, 0, 0, 0, 0, NOW())
                """,
                (
                    name,
                    barcode,
                    payload.category.strip(),
                    payload.description.strip()[:255],
                    round(payload.cost_price, 2),
                    round(payload.unit_price, 2),
                    payload.reorder_level,
                ),
            )
            item_id = cur.lastrowid

            await cur.execute(
                """
                INSERT INTO ospos_item_quantities (item_id, location_id, quantity, stock_status)
                VALUES (%s, 1, %s, %s)
                ON DUPLICATE KEY UPDATE quantity = VALUES(quantity), stock_status = VALUES(stock_status)
                """,
                (item_id, payload.quantity, 0 if payload.quantity > 0 else 1),
            )

            # OSPOS grid does INNER JOIN on inventory — without this row the item is invisible
            qty = round(payload.quantity, 3)
            await cur.execute(
                """
                INSERT INTO ospos_inventory
                    (trans_items, trans_user, trans_date, trans_comment, trans_location, trans_inventory)
                VALUES (%s, 1, NOW(), %s, 1, %s)
                """,
                (item_id, f"Criado pelo Estoque (quantidade inicial {qty})", qty),
            )

    logger.info("Estoque: created OSPOS item %d (%s)", item_id, name)

    await _item_update_notifier.broadcast({
        "type": "item_update",
        "action": "created",
        "item_id": item_id,
        "item_name": name,
    })

    return {"success": True, "item_id": item_id}


@router.patch("/item/{item_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def update_item(item_id: int, payload: ItemUpdate) -> dict[str, Any]:
    """Edit product fields (whitelisted) + stock at the default location."""
    current = await _fetch_one(
        f"SELECT {_ITEM_COLS} FROM {_ITEM_FROM} WHERE i.item_id = %s AND i.deleted = 0",
        (item_id,),
    )
    if not current:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    fields: dict[str, Any] = {}
    for key in _EDITABLE_TEXT | _EDITABLE_NUM:
        value = getattr(payload, key)
        if value is not None:
            fields[key] = value.strip() if key in _EDITABLE_TEXT else value
    if "item_number" in fields:
        barcode = fields["item_number"] or None
        if barcode:
            dup = await _fetch_one(
                "SELECT item_id FROM ospos_items WHERE item_number = %s AND deleted = 0 "
                "AND item_id != %s LIMIT 1",
                (barcode, item_id),
            )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Código {barcode} já existe (produto #{dup[0]})",
                )
        fields["item_number"] = barcode

    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if fields:
                sets = ", ".join(f"{col} = %s" for col in fields)
                await cur.execute(
                    f"UPDATE ospos_items SET {sets}, last_modified = NOW() WHERE item_id = %s",
                    (*fields.values(), item_id),
                )
            if payload.quantity is not None:
                new_qty = max(0.0, payload.quantity)
                # Get old quantity BEFORE updating
                old_qty_row = await _fetch_one(
                    "SELECT quantity FROM ospos_item_quantities WHERE item_id = %s AND location_id = 1",
                    (item_id,),
                )
                old_qty = float(old_qty_row[0]) if old_qty_row else 0.0
                # ZERADO when empty; never auto-clears IRREGULAR (that is a
                # deliberate state from the receiving flow).
                await cur.execute(
                    """
                    INSERT INTO ospos_item_quantities (item_id, location_id, quantity, stock_status)
                    VALUES (%s, 1, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        quantity = VALUES(quantity),
                        stock_status = IF(stock_status = 2, 2, IF(VALUES(quantity) <= 0, 1, 0))
                    """,
                    (item_id, new_qty, 0 if new_qty > 0 else 1),
                )
                diff = round(new_qty - old_qty, 3)
                if abs(diff) > 0.001:
                    comment = f"Estoque ajustado pelo Estoque: {old_qty:.3g} → {new_qty:.3g}"
                    await cur.execute(
                        """
                        INSERT INTO ospos_inventory
                            (trans_items, trans_user, trans_date, trans_comment, trans_location, trans_inventory)
                        VALUES (%s, 1, NOW(), %s, 1, %s)
                        """,
                        (item_id, comment, diff),
                    )

    logger.info("Estoque: updated OSPOS item %d (fields=%s)", item_id, list(fields))

    item_name = (await _fetch_one("SELECT name FROM ospos_items WHERE item_id = %s", (item_id,),)) or [""]
    await _item_update_notifier.broadcast({
        "type": "item_update",
        "action": "updated",
        "item_id": item_id,
        "item_name": item_name[0] if isinstance(item_name, (list, tuple)) else item_name,
        "fields": list(fields.keys()),
    })

    return {"success": True}


@router.post("/item/{item_id}/image", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def upload_item_image(
    item_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = Query(False, description="Background removal (rembg) — heavy"),
) -> dict[str, Any]:
    """Capture/replace the product photo straight into OSPOS.

    Saves locally under ``data/images/product_{id}.{ext}``, mirrors into
    the OSPOS uploads dir as ``{item_id}{ext}`` and updates
    ``pic_filename``.  Broadcasts the same photo event used by
    ``photos.html`` / the items grid toast.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Sem arquivo")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".jpg"

    row = await _fetch_one(
        f"SELECT {_ITEM_COLS} FROM {_ITEM_FROM} WHERE i.item_id = %s AND i.deleted = 0",
        (item_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    contents = await file.read()
    if len(contents) > MAX_IMAGE_SIZE * 4:  # hard cap before any processing
        raise HTTPException(status_code=400, detail="Foto grande demais (>20 MB)")

    if remove_bg:
        try:
            import io

            from PIL import Image
            from rembg import remove as rembg_remove

            output_image = rembg_remove(Image.open(io.BytesIO(contents)))
            buf = io.BytesIO()
            output_image.save(buf, format="PNG")
            contents = buf.getvalue()
            ext = ".png"
        except ImportError:
            logger.warning("rembg not installed — skipping background removal")
        except Exception as exc:
            logger.warning("rembg failed for item %d: %s — saving original", item_id, exc)

    filename = f"product_{item_id}{ext}"
    filepath = IMAGE_DIR / filename
    for old_ext in ALLOWED_EXTENSIONS:
        old_path = IMAGE_DIR / f"product_{item_id}{old_ext}"
        if old_path.exists() and old_path != filepath:
            old_path.unlink()

    filepath.write_bytes(contents)

    # ── Write-back into OSPOS uploads ────────────────────────────────
    ospos_fname = f"{item_id}{ext}"
    ospos_uploads = Path(settings.ospos_uploads_dir)
    ospos_uploads.mkdir(parents=True, exist_ok=True)
    dest = ospos_uploads / ospos_fname
    if dest.exists():
        dest.unlink()  # dir is group-writable; avoids owner issues
    shutil.copy2(filepath, dest)
    try:
        os.chown(dest, -1, grp.getgrnam("www-data").gr_gid)
        os.chmod(dest, 0o664)
    except OSError:
        pass

    await ospos_client.set_pic_filename(item_id, ospos_fname)

    event: dict[str, Any] = {
        "product_id": item_id,
        "product_name": row[1],
        "ospos_item_id": item_id,
        "pic_filename": ospos_fname,
        "status": "ok",
        "error": None,
    }
    _log_photo_event(event)
    await _photo_notifier.broadcast({"type": "photo", **event})

    logger.info(
        "Estoque: photo for item %d saved (%d bytes, remove_bg=%s)",
        item_id, len(contents), remove_bg,
    )
    return {
        "success": True,
        "filename": ospos_fname,
        "image_url": f"/v1/store/ospos-item-images/{ospos_fname}",
    }


@router.delete("/item/{item_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def delete_item(item_id: int) -> dict[str, Any]:
    """Soft-delete (``deleted=1``) — the item vanishes from sales/search
    but stays recoverable directly in OSPOS."""
    row = await _fetch_one(
        "SELECT item_id FROM ospos_items WHERE item_id = %s AND deleted = 0",
        (item_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ospos_items SET deleted = 1, last_modified = NOW() WHERE item_id = %s",
                (item_id,),
            )
    logger.info("Estoque: soft-deleted OSPOS item %d", item_id)

    await _item_update_notifier.broadcast({
        "type": "item_update",
        "action": "deleted",
        "item_id": item_id,
        "item_name": "",
    })

    return {"success": True}


# ── helpers ───────────────────────────────────────────────────────────────

async def _fetch_one(sql: str, params: tuple) -> Optional[tuple]:
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def _search_sql(term: str, limit: int) -> list[tuple]:
    like = f"%{term}%"
    return await _fetch_all(
        f"""
        SELECT {_ITEM_COLS}
        FROM {_ITEM_FROM}
        WHERE i.deleted = 0 AND (i.name LIKE %s OR i.item_number LIKE %s)
        ORDER BY i.name
        LIMIT %s
        """,
        (like, like, limit),
    )


async def _fetch_all(sql: str, params: tuple) -> list[tuple]:
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


def _row_dict(row: tuple) -> dict[str, Any]:
    return {
        "item_id": row[0],
        "name": row[1],
        "item_number": row[2],
        "category": row[3],
        "description": row[4],
        "cost_price": float(row[5] or 0),
        "unit_price": float(row[6] or 0),
        "reorder_level": float(row[7] or 0),
        "pic_filename": row[8],
        "quantity": float(row[9] or 0),
        "stock_status": int(row[10] or 0),
    }
