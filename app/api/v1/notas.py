"""Supplier-note endpoints (/v1/notas) — photo → AI parse → cross-match
→ confirmed receiving entry in OSPOS.

Flow:
1. ``POST /parse``          — upload note page photos; Gemini Vision extracts
                              structured data; lines are cross-matched against
                              the catalog (learned supplier-ref map, EAN,
                              fuzzy name+price).
2. ``POST /confirm``        — after user review in the app: creates/updates the
                              supplier, writes a full OSPOS receiving (header +
                              items + inventory + quantities + cost price) and
                              grows the learned map.
3. ``GET  /pending-barcodes`` — items still waiting for a barcode.
4. ``POST /link-barcode``   — bind a scanned EAN to an existing item
                              (anti-duplicate guard included).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.config import settings
from app.services import nota_matcher, ospos_client
from app.services.nota_parser import NotaParseError, parse_note_images
from app.utils.security import rate_limit_write, verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notas", tags=["notas"])

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_NOTAS_LOG = _DATA_DIR / "notas_log.jsonl"

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

_tables_ready = False


async def _ensure_tables() -> None:
    """Create the learned-map table on first use."""
    global _tables_ready
    if _tables_ready:
        return
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ospos_supplier_item_map (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    supplier_id INT NOT NULL,
                    supplier_ref VARCHAR(64) NOT NULL,
                    item_id INT NOT NULL,
                    note_ref VARCHAR(32) DEFAULT NULL,
                    ean_pending TINYINT NOT NULL DEFAULT 0,
                    last_seen DATETIME DEFAULT NULL,
                    UNIQUE KEY uq_sup_ref (supplier_id, supplier_ref),
                    KEY idx_item (item_id),
                    KEY idx_pending (ean_pending)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8
                """
            )
    _tables_ready = True


async def _fetch_all(sql: str, params: tuple) -> list[tuple]:
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


async def _fetch_one(sql: str, params: tuple) -> Optional[tuple]:
    rows = await _fetch_all(sql, params)
    return rows[0] if rows else None


def _digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


# ── 1. Parse ──────────────────────────────────────────────────────────────


@router.post("/parse", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def parse(
    files: list[UploadFile] = File(..., description="Note page photos"),
) -> dict[str, Any]:
    """Read note photos via AI and cross-match every line with the catalog."""
    if not files:
        raise HTTPException(status_code=400, detail="Envie ao menos uma foto da nota")
    if len(files) > settings.gemini_max_images:
        raise HTTPException(
            status_code=400,
            detail=f"Máximo {settings.gemini_max_images} páginas por vez",
        )

    images: list[tuple[bytes, str]] = []
    for f in files:
        content = await f.read()
        mime = (f.content_type or "image/jpeg").split(";")[0].lower()
        if mime not in _ALLOWED_MIME:
            mime = "image/jpeg"
        if len(content) > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"Foto {f.filename} grande demais (>15 MB)")
        images.append((content, mime))

    try:
        parsed = await parse_note_images(images)
    except NotaParseError as exc:
        logger.error("Nota parse failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    started = time.time()
    await _ensure_tables()

    # Learned map for the recognized supplier (by id when known).
    supplier_id = await resolve_supplier_id(
        cnpj=_digits(parsed["supplier"]["cnpj"]),
        name=parsed["supplier"]["name"],
    )
    learned_map: dict[str, int] = {}
    if supplier_id and any(line.get("ref") for line in parsed["items"]):
        rows = await _fetch_all(
            "SELECT supplier_ref, item_id FROM ospos_supplier_item_map WHERE supplier_id = %s",
            (supplier_id,),
        )
        learned_map = {str(ref): int(item_id) for ref, item_id in rows}

    catalog_rows = await _fetch_all(
        "SELECT item_id, name, item_number, unit_price, cost_price "
        "FROM ospos_items WHERE deleted = 0"
    )
    matches = nota_matcher.match_items(parsed["items"], catalog_rows, learned_map)

    items_out = []
    for idx, (line, match) in enumerate(zip(parsed["items"], matches)):
        items_out.append({**line, "index": idx, "match": match.to_dict()})

    result = {
        **parsed,
        "supplier_id": supplier_id,
        "items": items_out,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    await log_note_event({"event": "parse", "result": _json_safe(result)})
    return result


async def resolve_supplier_id(cnpj: Optional[str], name: Optional[str]) -> Optional[int]:
    """Find an existing supplier by CNPJ (account_number digits) or name."""
    if cnpj:
        rows = await _fetch_all(
            "SELECT person_id, account_number FROM ospos_suppliers WHERE deleted = 0",
            (),
        )
        for pid, acct in rows:
            if acct and _digits(str(acct)) == cnpj:
                return int(pid)
    if name and len(name) >= 3:
        row = await _fetch_one(
            "SELECT s.person_id FROM ospos_suppliers s "
            "WHERE s.deleted = 0 AND s.company_name LIKE %s LIMIT 1",
            (f"%{name[:60]}%",),
        )
        if row:
            return int(row[0])
    return None


# ── 2. Confirm ────────────────────────────────────────────────────────────


class ConfirmLine(BaseModel):
    index: int
    action: Literal["use", "create", "skip"]
    item_id: Optional[int] = None
    name_note: str = ""
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    discount_percent: Optional[float] = None
    discount_value: Optional[float] = None
    line_total: Optional[float] = None
    ref: Optional[str] = None
    ean: Optional[str] = None
    # create-only fields
    name: Optional[str] = None
    category: Optional[str] = None
    unit_sale_price: Optional[float] = None


class NoteConfirm(BaseModel):
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    supplier_cnpj: Optional[str] = None
    supplier_phone: Optional[str] = None
    supplier_email: Optional[str] = None
    note_number: Optional[str] = None
    payment_terms: Optional[str] = None
    comment: Optional[str] = None
    total: Optional[float] = None
    lines: list[ConfirmLine]


def net_unit_cost(line: ConfirmLine) -> float:
    gross = line.qty * line.unit_price
    if line.discount_value is not None:
        disc = line.discount_value
    elif line.discount_percent is not None:
        disc = gross * line.discount_percent / 100.0
    else:
        disc = 0.0
    net_total = max(gross - disc, 0.0)
    return round(net_total / line.qty, 2)


@router.post("/confirm", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def confirm(payload: NoteConfirm) -> dict[str, Any]:
    """Write the confirmed note into OSPOS as a full receiving."""
    await _ensure_tables()

    active_lines = [l for l in payload.lines if l.action != "skip"]
    if not active_lines:
        raise HTTPException(status_code=400, detail="Nenhuma linha ativa na nota")

    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        cur = await conn.cursor()
        try:
            await conn.begin()

            supplier_id = payload.supplier_id
            if supplier_id is not None:
                await cur.execute(
                    "SELECT person_id FROM ospos_suppliers WHERE person_id = %s AND deleted = 0",
                    (supplier_id,),
                )
                if not await cur.fetchone():
                    supplier_id = None

            created_supplier = False
            if supplier_id is None and (payload.supplier_name or payload.supplier_cnpj):
                supplier_id = await resolve_supplier_id(
                    _digits(payload.supplier_cnpj), payload.supplier_name
                )
                if supplier_id is None:
                    name = (payload.supplier_name or payload.supplier_cnpj or "Fornecedor").strip()[:255]
                    cnpj = _digits(payload.supplier_cnpj)
                    phone = (payload.supplier_phone or "").strip()
                    email = (payload.supplier_email or "").strip()
                    await cur.execute(
                        """
                        INSERT INTO ospos_people
                            (first_name, last_name, gender, phone_number, email,
                             address_1, address_2, city, state, zip, country, comments)
                        VALUES (%s, '', NULL, %s, %s, '', '', '', '', '', '',
                                'Criado automaticamente pelo app de estoque')
                        """,
                        (name, phone, email),
                    )
                    person_id = cur.lastrowid
                    await cur.execute(
                        "INSERT INTO ospos_suppliers (person_id, company_name, agency_name, account_number, deleted) "
                        "VALUES (%s, %s, '', %s, 0)",
                        (person_id, name[:255], cnpj),
                    )
                    supplier_id = person_id
                    created_supplier = True

            comment_parts = []
            if payload.note_number:
                comment_parts.append(f"Nota {payload.note_number}")
            if payload.payment_terms:
                comment_parts.append(f"Prazo: {payload.payment_terms}")
            if payload.comment:
                comment_parts.append(payload.comment)
            # employee_id has a FK to ospos_employees — use the admin account
            # (person_id 1); 0 violates the constraint.
            await cur.execute(
                """
                INSERT INTO ospos_receivings
                    (receiving_time, supplier_id, employee_id, comment, payment_type, reference)
                VALUES (NOW(), %s, 1, %s, NULL, %s)
                """,
                (
                    supplier_id,
                    (" · ".join(comment_parts))[:65535],
                    (payload.note_number or "")[:32] or None,
                ),
            )
            receiving_id = cur.lastrowid

            used: list[int] = []
            created: list[int] = []
            total_net = 0.0
            line_no = 1

            for line in sorted(active_lines, key=lambda l: l.index):
                net_unit = net_unit_cost(line)

                if line.action == "use":
                    if not line.item_id:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Linha {line.index}: action=use sem item_id",
                        )
                    await cur.execute(
                        "SELECT item_id, COALESCE(unit_price,0), deleted FROM ospos_items WHERE item_id = %s",
                        (line.item_id,),
                    )
                    row = await cur.fetchone()
                    if not row or row[2]:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Produto #{line.item_id} não encontrado (linha {line.index})",
                        )
                    item_id = int(row[0])
                    sale_price = float(row[1] or 0)
                    used.append(item_id)
                else:  # create
                    name = (line.name or line.name_note or "").strip()[:255]
                    if not name:
                        raise HTTPException(
                            status_code=400, detail=f"Linha {line.index}: nome vazio"
                        )
                    ean_digits = _digits(line.ean) or (line.ean or "").strip() or None
                    if ean_digits:
                        await cur.execute(
                            "SELECT item_id FROM ospos_items WHERE item_number = %s AND deleted = 0 LIMIT 1",
                            (ean_digits,),
                        )
                        dup = await cur.fetchone()
                        if dup:
                            raise HTTPException(
                                status_code=409,
                                detail=f"Código {ean_digits} já existe no produto #{dup[0]} (linha {line.index})",
                            )
                    sale_price = round(line.unit_sale_price if line.unit_sale_price else net_unit, 2)
                    await cur.execute(
                        """
                        INSERT INTO ospos_items
                            (name, item_number, category, description,
                             cost_price, unit_price, reorder_level, receiving_quantity,
                             allow_alt_description, is_serialized, stock_type, item_type,
                             deleted, last_modified)
                        VALUES (%s, %s, %s, '', %s, %s, 0, 1, 1, 0, 0, 0, 0, NOW())
                        """,
                        (name, ean_digits, (line.category or "").strip()[:255],
                         net_unit, sale_price),
                    )
                    item_id = cur.lastrowid
                    await cur.execute(
                        "INSERT INTO ospos_item_quantities (item_id, location_id, quantity, stock_status) "
                        "VALUES (%s, 1, 0, 1)",
                        (item_id,),
                    )
                    created.append(item_id)

                # Cost price follows the latest purchase (OSPOS behavior).
                await cur.execute(
                    "UPDATE ospos_items SET cost_price = %s, last_modified = NOW() WHERE item_id = %s",
                    (net_unit, item_id),
                )

                await cur.execute(
                    """
                    INSERT INTO ospos_receivings_items
                        (receiving_id, item_id, description, serialnumber, line,
                         quantity_purchased, item_cost_price, item_unit_price,
                         discount_percent, item_location, receiving_quantity)
                    VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, 1, 1)
                    """,
                    (
                        receiving_id,
                        item_id,
                        (line.name_note or "")[:30],
                        line_no,
                        line.qty,
                        net_unit,
                        sale_price,
                        line.discount_percent or 0,
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO ospos_inventory
                        (trans_items, trans_user, trans_date, trans_comment, trans_location, trans_inventory)
                    VALUES (%s, 0, NOW(), %s, 1, %s)
                    """,
                    (item_id, ("Entrada nota " + (payload.note_number or ""))[:255], line.qty),
                )
                await cur.execute(
                    """
                    INSERT INTO ospos_item_quantities (item_id, location_id, quantity, stock_status)
                    VALUES (%s, 1, %s, 0)
                    ON DUPLICATE KEY UPDATE
                        quantity = quantity + VALUES(quantity),
                        stock_status = IF(stock_status = 2, 2, IF(quantity + VALUES(quantity) <= 0, 1, 0))
                    """,
                    (item_id, line.qty),
                )

                # Grow the learned map (supplier ref → item).
                ref_clean = (line.ref or "").strip()
                if supplier_id is not None and ref_clean:
                    has_barcode_row = await _fetch_one(
                        "SELECT item_number FROM ospos_items WHERE item_id = %s",
                        (item_id,),
                    )
                    pending = 0 if (has_barcode_row and has_barcode_row[0]) else 1
                    await cur.execute(
                        """
                        INSERT INTO ospos_supplier_item_map
                            (supplier_id, supplier_ref, item_id, note_ref, ean_pending, last_seen)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON DUPLICATE KEY UPDATE
                            item_id = VALUES(item_id),
                            note_ref = VALUES(note_ref),
                            last_seen = NOW(),
                            ean_pending = IF(VALUES(ean_pending) = 0, 0, ean_pending)
                        """,
                        (supplier_id, ref_clean[:64], item_id,
                         (payload.note_number or "")[:32], pending),
                    )

                total_net += net_unit * line.qty
                line_no += 1

            await conn.commit()
        except HTTPException:
            await conn.rollback()
            raise
        except Exception as exc:
            await conn.rollback()
            logger.exception("Nota confirm failed")
            raise HTTPException(status_code=500, detail=f"Falha ao gravar nota: {exc}")
        finally:
            await cur.close()

    summary = {
        "success": True,
        "receiving_id": receiving_id,
        "supplier_id": supplier_id,
        "created_supplier": created_supplier,
        "items_used": used,
        "items_created": created,
        "total_net": round(total_net, 2),
        "lines": len(active_lines),
    }
    await log_note_event({"event": "confirm", **_json_safe(summary)})
    logger.info(
        "Nota gravada: receiving=%s supplier=%s used=%s created=%s",
        receiving_id, supplier_id, len(used), len(created),
    )
    return summary


# ── 3. Pending barcodes ───────────────────────────────────────────────────


@router.get("/pending-barcodes")
async def pending_barcodes(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Items that entered through notes but still have no barcode."""
    await _ensure_tables()
    rows = await _fetch_all(
        """
        SELECT m.item_id, i.name, i.pic_filename, i.cost_price, m.supplier_ref,
               m.note_ref, m.last_seen, s.company_name
        FROM ospos_supplier_item_map m
        JOIN ospos_items i ON i.item_id = m.item_id AND i.deleted = 0
        LEFT JOIN ospos_suppliers s ON s.person_id = m.supplier_id
        WHERE m.ean_pending = 1 AND i.item_number IS NULL
        ORDER BY m.last_seen DESC
        LIMIT %s
        """,
        (limit,),
    )
    items = [
        {
            "item_id": r[0],
            "name": r[1],
            "pic_filename": r[2],
            "cost_price": float(r[3] or 0),
            "supplier_ref": r[4],
            "note_ref": r[5],
            "last_seen": str(r[6]) if r[6] else None,
            "supplier_name": r[7],
        }
        for r in rows
    ]
    return {"count": len(items), "items": items}


class LinkBarcode(BaseModel):
    item_id: int
    barcode: str


@router.post("/link-barcode", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def link_barcode(payload: LinkBarcode) -> dict[str, Any]:
    """Bind a scanned EAN to an existing item (never creates a product)."""
    barcode = payload.barcode.strip()
    if len(barcode) < 4:
        raise HTTPException(status_code=400, detail="Código inválido")

    dup = await _fetch_one(
        "SELECT item_id FROM ospos_items WHERE item_number = %s AND deleted = 0 AND item_id != %s LIMIT 1",
        (barcode, payload.item_id),
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"Esse código já pertence ao produto #{dup[0]}",
        )
    row = await _fetch_one(
        "SELECT item_id FROM ospos_items WHERE item_id = %s AND deleted = 0",
        (payload.item_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ospos_items SET item_number = %s, last_modified = NOW() WHERE item_id = %s",
                (barcode, payload.item_id),
            )
            await cur.execute(
                "UPDATE ospos_supplier_item_map SET ean_pending = 0 WHERE item_id = %s",
                (payload.item_id,),
            )

    await log_note_event(
        {"event": "link-barcode", "item_id": payload.item_id, "barcode": barcode}
    )
    logger.info("Barcode %s vinculado ao item %s", barcode, payload.item_id)
    return {"success": True, "item_id": payload.item_id, "barcode": barcode}


# ── helpers ───────────────────────────────────────────────────────────────


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (int, float, str)) or obj is None:
        return obj
    return str(obj)


async def log_note_event(event: dict[str, Any]) -> None:
    """Append-only audit trail of note parsing/confirmation."""
    try:
        event = {**event, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        line = json.dumps(_json_safe(event), ensure_ascii=False)
        def _write() -> None:
            _NOTAS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _NOTAS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        import asyncio
        await asyncio.to_thread(_write)
    except Exception:
        logger.warning("Falha ao registrar evento de nota", exc_info=True)
