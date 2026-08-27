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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
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
async def create_item(payload: ItemCreate, request: Request) -> dict[str, Any]:
    """Create an OSPOS product — designed for products WITHOUT a barcode.

    ``item_number`` is optional; when given it must not collide with an
    existing active item.
    """
    role = _assert_write_allowed(request)
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

    _audit("created", role, item_id, name=_requester_name(request), detail={
        "name": name,
        "item_number": barcode,
        "unit_price": round(payload.unit_price, 2),
        "quantity": payload.quantity,
    })

    return {"success": True, "item_id": item_id}


@router.patch("/item/{item_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def update_item(item_id: int, payload: ItemUpdate, request: Request) -> dict[str, Any]:
    """Edit product fields (whitelisted) + stock at the default location."""
    role = _assert_write_allowed(request)
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

    _audit("updated", role, item_id, name=_requester_name(request), detail={
        "fields": list(fields.keys()),
        "quantity": payload.quantity,
        "old_quantity": old_qty if "old_qty" in locals() else None,
    })

    return {"success": True}


@router.post("/item/{item_id}/image", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def upload_item_image(
    item_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = Query(False, description="Background removal (rembg) — heavy"),
    request: Request = None,
) -> dict[str, Any]:
    """Capture/replace the product photo straight into OSPOS.

    Saves locally under ``data/images/product_{id}.{ext}``, mirrors into
    the OSPOS uploads dir as ``{item_id}{ext}`` and updates
    ``pic_filename``.  Broadcasts the same photo event used by
    ``photos.html`` / the items grid toast.
    """
    role = _assert_write_allowed(request)
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
    _audit("photo", role, item_id, name=_requester_name(request), detail={
        "filename": ospos_fname,
        "remove_bg": remove_bg,
        "bytes": len(contents),
    })
    return {
        "success": True,
        "filename": ospos_fname,
        "image_url": f"/v1/store/ospos-item-images/{ospos_fname}",
    }


@router.delete("/item/{item_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def delete_item(item_id: int, request: Request) -> dict[str, Any]:
    """Soft-delete (``deleted=1``) — the item vanishes from sales/search
    but stays recoverable directly in OSPOS."""
    role = _assert_write_allowed(request)
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

    _audit("deleted", role, item_id, name=_requester_name(request), detail={"soft_delete": True})

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


# ── App settings (PIN, role, schedule, remote lock) ─────────────────────

import hashlib
import json as _json
from datetime import datetime as _dt

_SETTINGS_PATH = settings._data_dir if hasattr(settings, '_data_dir') else Path(__file__).resolve().parent.parent.parent / "data"
_SETTINGS_FILE = _SETTINGS_PATH / "estoque_settings.json"
_AUDIT_FILE = _SETTINGS_PATH / "estoque_audit.jsonl"

_DEFAULT_SETTINGS: dict[str, Any] = {
    "pin_hash": "",           # SHA-256 of 4-digit PIN (empty = no PIN set)
    "role": "owner",          # "owner" | "employee"
    "lock_enabled": False,    # remote lock toggle
    "schedule_enabled": False,
    "schedule_start": "08:00",
    "schedule_end": "18:00",
    "hide_cost": True,        # employee can't see cost_price
    "hide_totals": True,      # employee can't see total product count
}


def _load_settings() -> dict[str, Any]:
    if _SETTINGS_FILE.exists():
        try:
            data = _json.loads(_SETTINGS_FILE.read_text())
            merged = {**_DEFAULT_SETTINGS, **data}
            return merged
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)


def _save_settings(data: dict[str, Any]) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(_json.dumps(data, indent=2, ensure_ascii=False))


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


class SettingsUpdate(BaseModel):
    current_pin: str = ""          # required if PIN is set
    new_pin: Optional[str] = None  # set/change PIN
    role: Optional[str] = None     # "owner" | "employee"
    schedule_enabled: Optional[bool] = None
    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    hide_cost: Optional[bool] = None
    hide_totals: Optional[bool] = None


class LoginRequest(BaseModel):
    pin: str


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Return app settings (never expose pin_hash)."""
    s = _load_settings()
    has_pin = bool(s.get("pin_hash"))
    return {
        "has_pin": has_pin,
        "role": s.get("role", "owner"),
        "lock_enabled": s.get("lock_enabled", False),
        "schedule_enabled": s.get("schedule_enabled", False),
        "schedule_start": s.get("schedule_start", "08:00"),
        "schedule_end": s.get("schedule_end", "18:00"),
        "hide_cost": s.get("hide_cost", True),
        "hide_totals": s.get("hide_totals", True),
    }


@router.post("/settings/login")
async def settings_login(body: LoginRequest) -> dict[str, Any]:
    """Verify PIN and return role. If no PIN is set, always returns owner."""
    s = _load_settings()
    pin_hash = s.get("pin_hash", "")
    if not pin_hash:
        # No PIN configured — first user becomes owner
        return {"success": True, "role": "owner", "message": "PIN não configurado. Defina um PIN nas configurações."}
    if _hash_pin(body.pin) != pin_hash:
        raise HTTPException(status_code=401, detail="PIN incorreto")
    return {"success": True, "role": s.get("role", "owner")}


@router.post("/settings/lock")
async def toggle_lock(body: LoginRequest) -> dict[str, Any]:
    """Owner can lock/unlock the app remotely."""
    s = _load_settings()
    pin_hash = s.get("pin_hash", "")
    if pin_hash and _hash_pin(body.pin) != pin_hash:
        raise HTTPException(status_code=401, detail="PIN incorreto")
    s["lock_enabled"] = not s.get("lock_enabled", False)
    _save_settings(s)
    logger.info("Estoque: remote lock toggled to %s", s["lock_enabled"])
    return {"success": True, "lock_enabled": s["lock_enabled"]}


@router.patch("/settings")
async def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    """Update app settings. Requires current PIN if one is set."""
    s = _load_settings()
    pin_hash = s.get("pin_hash", "")

    # Verify current PIN if one exists
    if pin_hash and body.current_pin and _hash_pin(body.current_pin) != pin_hash:
        raise HTTPException(status_code=401, detail="PIN atual incorreto")
    if pin_hash and not body.current_pin:
        raise HTTPException(status_code=400, detail="PIN atual é obrigatório")

    # Change/set PIN
    if body.new_pin is not None:
        if len(body.new_pin) < 4:
            raise HTTPException(status_code=400, detail="PIN deve ter pelo menos 4 dígitos")
        s["pin_hash"] = _hash_pin(body.new_pin)

    if body.role is not None:
        if body.role not in ("owner", "employee"):
            raise HTTPException(status_code=400, detail="Role deve ser 'owner' ou 'employee'")
        s["role"] = body.role

    if body.schedule_enabled is not None:
        s["schedule_enabled"] = body.schedule_enabled
    if body.schedule_start is not None:
        s["schedule_start"] = body.schedule_start
    if body.schedule_end is not None:
        s["schedule_end"] = body.schedule_end
    if body.hide_cost is not None:
        s["hide_cost"] = body.hide_cost
    if body.hide_totals is not None:
        s["hide_totals"] = body.hide_totals

    _save_settings(s)
    logger.info("Estoque: settings updated (role=%s)", s.get("role"))
    return {"success": True, **get_settings_sync(s)}


def get_settings_sync(s: dict) -> dict[str, Any]:
    return {
        "has_pin": bool(s.get("pin_hash")),
        "role": s.get("role", "owner"),
        "lock_enabled": s.get("lock_enabled", False),
        "schedule_enabled": s.get("schedule_enabled", False),
        "schedule_start": s.get("schedule_start", "08:00"),
        "schedule_end": s.get("schedule_end", "18:00"),
        "hide_cost": s.get("hide_cost", True),
        "hide_totals": s.get("hide_totals", True),
    }


# ── Requester role + audit (telemetry) ─────────────────────────────────

def _requester_role(request: Request, *, default: str = "owner") -> str:
    """Identify who is calling from the ``X-App-Role`` header.

    The employee APK always sends ``X-App-Role: employee``; the owner APK
    sends ``owner``. Falls back to the global configured role for legacy
    / web clients.
    """
    hdr = (request.headers.get("X-App-Role") or "").strip().lower()
    if hdr in ("owner", "employee"):
        return hdr
    return _load_settings().get("role", default)


def _requester_name(request: Request) -> str:
    """Who is calling, from the ``X-App-Name`` header (set by the app from
    the profile screen). Capped at 80 chars; used inside audit entries."""
    return (request.headers.get("X-App-Name") or "").strip()[:80]


def _now_in_schedule() -> bool:
    """True when the current local time falls within the configured window
    (supports ranges that cross midnight)."""
    s = _load_settings()
    if not s.get("schedule_enabled"):
        return True
    start = s.get("schedule_start", "08:00")
    end = s.get("schedule_end", "18:00")
    try:
        now = _dt.now().strftime("%H:%M")
        if start < end:
            return start <= now < end
        return now >= start or now < end  # crosses midnight
    except Exception:
        return True


def _assert_write_allowed(request: Request) -> str:
    """Enforce the work schedule on employee writes.

    The owner APK is never blocked; only employee-role callers are gated.
    Returns the requester role (so callers can keep it for auditing).
    """
    role = _requester_role(request)
    s = _load_settings()
    if role == "employee" and s.get("lock_enabled"):
        raise HTTPException(status_code=403, detail="App travado pelo proprietário")
    if role == "employee" and not _now_in_schedule():
        sched = s.get("schedule_start", "08:00"), s.get("schedule_end", "18:00")
        raise HTTPException(
            status_code=403,
            detail=f"Fora do horário de trabalho ({sched[0]} – {sched[1]}). "
                   "Você não pode alterar o estoque agora.",
        )
    return role


def _audit(action: str, role: str, item_id: Optional[int], name: str = "", detail: dict[str, Any] | None = None) -> None:
    """Append one telemetry line per employee action (append-only journal).

    Every write performed on the employee app is recorded here, so the
    owner can audit what was changed, when and by whom. Owner actions are
    also recorded for completeness. ``name`` comes from the ``X-App-Name``
    header (employee profile).
    """
    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "role": role,
            "action": action,
            "item_id": item_id,
            "ip": "lan",
        }
        if name:
            entry["name"] = name
        if detail:
            entry["detail"] = detail
        with _AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Estoque audit: role=%s action=%s item=%s", role, action, item_id)
    except Exception as exc:  # never break a write because auditing failed
        logger.warning("Estoque audit write failed: %s", exc)


@router.get("/audit")
async def get_audit(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Return the most recent audit entries (newest first) — used by the
    owner to inspect employee activity."""
    if not _AUDIT_FILE.exists():
        return {"events": []}
    lines = _AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-limit:]:
        try:
            events.append(_json.loads(line))
        except Exception:
            continue
    events.reverse()
    return {"events": events}
