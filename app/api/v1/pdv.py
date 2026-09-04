"""POS (Vender) endpoints — mobile sales written straight into OSPOS MySQL.

Powers the "Vender" tab of the Android dashboard app with an offline-first
flow: the app caches the catalog locally (``GET /v1/pdv/catalog``) and
queues sales while the PC is off-line. When connectivity returns, the app
POSTs the queued sales to ``POST /v1/pdv/sale``, which writes them
transactionally into OSPOS — replicating ``Sale::save_value()`` semantics
(sales + payments + sales_items + inventory decrement + stock_status).

Idempotency: every sale carries a client-generated UUID (``client_sale_id``)
stored in ``ospos_sales_payments.reference_code``. Re-sending the same UUID
returns the already-created sale instead of duplicating it — so a lost
response during sync never double-sells.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import ospos_client
from app.utils.security import verify_api_key, rate_limit_write

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdv", tags=["pdv"])

# OSPOS constants (app/Config/Constants.php + app/Models/Item_quantity.php)
COMPLETED = 0
SALE_TYPE_POS = 0
HAS_STOCK = 0
PERCENT = 0
FIXED = 1
STOCK_OK = 0
STOCK_ZERADO = 1
STOCK_IRREGULAR = 2

_LOCATION_ID = 1  # estoque físico do balcão (location_id padrão usado pelo estoque app)

# pt-BR labels as found in ospos_sales_payments.payment_type (fix #25)
PAYMENT_LABELS = {
    "cash": "Dinheiro",
    "debit": "Cartão Débito",
    "credit": "Cartão Crédito",
    "pix": "PIX",
    "fiado": "Fiado",
}


class PdvItem(BaseModel):
    item_id: int
    line: int = 0
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    cost_price: float = 0.0
    discount: float = 0.0
    discount_type: int = PERCENT
    item_location: int = _LOCATION_ID
    description: str = ""
    serialnumber: Optional[str] = None
    print_option: int = 0


class PdvPayment(BaseModel):
    payment_type: str
    payment_amount: float = Field(ge=0)
    cash_refund: float = 0.0
    cash_adjustment: int = 0


class PdvSaleRequest(BaseModel):
    items: list[PdvItem] = Field(min_length=1)
    payments: list[PdvPayment] = Field(min_length=1)
    customer_id: Optional[int] = None
    employee_id: int = 1
    comment: str = ""
    client_sale_id: str = ""


# ── Catalog (offline cache) ──────────────────────────────────────────────


@router.get("/catalog")
async def get_catalog(
    offset: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=2000),
) -> dict[str, Any]:
    """Active products for the phone's offline cache.

    Paged by ``offset``/``limit`` (+ ``X-Total-Count`` header shows how many
    pages remain). Returns name, prices, stock and pic so the app can sell
    fully offline once the cache is warm.
    """
    pool = await ospos_client._pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            base = """
                SELECT i.item_id, i.name, COALESCE(i.item_number, '') AS item_number,
                       i.category, i.description, i.cost_price, i.unit_price,
                       COALESCE(i.pic_filename, '') AS pic_filename,
                       COALESCE(q.quantity, 0) AS quantity,
                       COALESCE(q.stock_status, 0) AS stock_status
                FROM ospos_items i
                LEFT JOIN ospos_item_quantities q
                       ON q.item_id = i.item_id AND q.location_id = %s
                WHERE i.deleted = 0
                ORDER BY i.item_id ASC
                LIMIT %s OFFSET %s
            """
            await cur.execute(base, (_LOCATION_ID, limit, offset))
            cols = [d[0] for d in cur.description]
            items = [dict(zip(cols, row)) async for row in cur]

            await cur.execute("SELECT COUNT(*) FROM ospos_items WHERE deleted = 0")
            total = (await cur.fetchone())[0]

    return {"items": items, "total": total, "offset": offset, "limit": limit}


# ── Settings ─────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Store config the selling UI needs: payment methods, currency, cashiers."""
    pool = await ospos_client._pool()
    settings: dict[str, str] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT `key`, value FROM ospos_app_config "
                "WHERE `key` IN ('currency_symbol','cash_decimals','payment_options_order')"
            )
            async for row in cur:
                settings[row[0]] = row[1]

            await cur.execute(
                "SELECT p.person_id, CONCAT(p.first_name, ' ', p.last_name) AS full_name "
                "FROM ospos_employees e JOIN ospos_people p ON p.person_id = e.person_id "
                "WHERE p.first_name <> '' ORDER BY p.person_id"
            )
            employees = [{"id": row[0], "name": row[1]} async for row in cur]

    # payment_options_order is stored as "cash\ndebit\ncredit\npix\nfiado"
    order_raw = settings.get("payment_options_order", "cash\ndebit\ncredit\npix")
    keys = [k.strip() for k in order_raw.replace("\r", "").split("\n") if k.strip()]

    return {
        "currency_symbol": settings.get("currency_symbol", "R$"),
        "cash_decimals": settings.get("cash_decimals", "2"),
        "payment_methods": [
            {"key": k, "label": PAYMENT_LABELS.get(k, k)} for k in keys
        ],
        "employees": employees,
        "default_location_id": _LOCATION_ID,
    }


# ── Sale write (idempotent, transactional) ───────────────────────────────


async def _find_existing_sale(pool, client_sale_id: str) -> Optional[int]:
    """If a sale with this UUID was already written, return its sale_id."""
    if not client_sale_id:
        return None
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT sale_id FROM ospos_sales_payments "
                "WHERE reference_code = %s LIMIT 1",
                (client_sale_id[:40],),
            )
            row = await cur.fetchone()
    return row[0] if row else None


@router.post("/sale", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def create_sale(payload: PdvSaleRequest) -> dict[str, Any]:
    """Write a completed POS sale into OSPOS (mirrors Sale::save_value).

    HTTP entry point — delegates to :func:`write_ospos_sale`, which is also
    reused by the Mercado Livre webhook so ML sales hit the real OSPOS
    tables (not a local simulation).
    """
    return await write_ospos_sale(payload)


async def write_ospos_sale(payload: PdvSaleRequest) -> dict[str, Any]:
    """Write a completed sale into OSPOS (mirrors Sale::save_value).

    Shared core for both ``POST /pdv/sale`` and the ML webhook processor.

    - Single transaction: sales → payments → sales_items → stock/inventory.
    - Stock is clamped at 0 and flagged (STOCK_ZERADO / STOCK_IRREGULAR),
      exactly like the OSPOS register does.
    - Idempotent by ``client_sale_id`` (stored in payments.reference_code);
      a retry returns ``{"duplicate": true, "sale_id": N}``.
    """
    pool = await ospos_client._pool()

    # 0. Idempotency: already written?
    existing = await _find_existing_sale(pool, payload.client_sale_id)
    if existing:
        return {"success": True, "duplicate": True, "sale_id": existing}

    if payload.employee_id <= 0:
        raise HTTPException(status_code=400, detail="employee_id inválido")

    # 1. Load item rows for stock validation + names/costs (single query)
    item_ids = list(dict.fromkeys(it.item_id for it in payload.items))
    placeholders = ",".join(["%s"] * len(item_ids))
    pool_inner = await ospos_client._pool()
    async with pool_inner.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT i.item_id, i.name, i.cost_price, i.unit_price, i.stock_type,
                       i.deleted, COALESCE(q.quantity, 0) AS qty
                FROM ospos_items i
                LEFT JOIN ospos_item_quantities q
                       ON q.item_id = i.item_id AND q.location_id = %s
                WHERE i.item_id IN ({placeholders})
                """,
                (_LOCATION_ID, *item_ids),
            )
            db_rows = {row[0]: row async for row in cur}

    missing = [iid for iid in item_ids if iid not in db_rows]
    if missing:
        raise HTTPException(status_code=400, detail=f"Item(ns) não encontrado(s): {missing}")

    # 2. Compute totals + grab authoritative cost/price/stock for each line
    total = 0.0
    refundable_extra = 0.0
    for it in payload.items:
        row = db_rows[it.item_id]
        item_id, name, db_cost, db_price, stock_type, deleted, qty = (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        )
        if deleted:
            raise HTTPException(status_code=400, detail=f"Item {item_id} ({name}) está inativo")
        # Trust the cashier-entered sell price, but never the cost from the app
        if it.price == 0:
            it.price = float(db_price or 0)
        if it.cost_price == 0:
            it.cost_price = float(db_cost or 0)
        if it.discount > 0 and it.price == 0:
            it.discount = 0.0

        line_total = it.quantity * it.price
        if it.discount_type == FIXED:
            line_total -= it.quantity * it.discount
        else:  # PERCENT
            line_total -= it.quantity * it.price * it.discount / 100.0
        total += line_total

    # 3. Cash refund: if payments exceed the total, refund goes to "Dinheiro"
    payments_total = sum(p.payment_amount for p in payload.payments)
    if payments_total > total:
        refundable_extra = round(payments_total - total, 2)

    async with pool.acquire() as conn:
        cur = await conn.cursor()
        try:
            await conn.begin()

            # 3a. Sales header
            customer_id: Optional[int] = payload.customer_id
            if customer_id is not None:
                await cur.execute(
                    "SELECT person_id FROM ospos_customers WHERE person_id = %s LIMIT 1",
                    (customer_id,),
                )
                if not await cur.fetchone():
                    customer_id = None

            # NOTE: to_datetime('now') goes through MySQL tz; sale_time as server NOW()
            await cur.execute(
                """
                INSERT INTO ospos_sales
                    (sale_time, customer_id, employee_id, comment, sale_status,
                     invoice_number, quote_number, work_order_number,
                     dinner_table_id, sale_type)
                VALUES (NOW(), %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (customer_id, payload.employee_id, payload.comment, COMPLETED, SALE_TYPE_POS),
            )
            sale_id = cur.lastrowid

            # 3b. Payments (reference_code carries the client UUID for dedupe)
            for i, pay in enumerate(payload.payments):
                refund = 0.0
                if refundable_extra > 0 and "Dinheiro" in pay.payment_type:
                    refund = min(refundable_extra, pay.payment_amount)
                    refundable_extra = round(refundable_extra - refund, 2)
                await cur.execute(
                    """
                    INSERT INTO ospos_sales_payments
                        (sale_id, payment_type, payment_amount, cash_refund,
                         cash_adjustment, employee_id, reference_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sale_id, pay.payment_type, round(pay.payment_amount, 2), refund,
                        pay.cash_adjustment, payload.employee_id,
                        payload.client_sale_id[:40] if i == 0 else None,
                    ),
                )

            # 3c. Items + stock + inventory
            for it in payload.items:
                row = db_rows[it.item_id]
                item_id, name, db_cost, _db_price, stock_type, _deleted, qty = (
                    row[0], row[1], row[2], row[3], row[4], row[5], row[6]
                )
                await cur.execute(
                    """
                    INSERT INTO ospos_sales_items
                        (sale_id, item_id, description, serialnumber, line,
                         quantity_purchased, item_cost_price, item_unit_price,
                         discount, discount_type, item_location, print_option)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sale_id, item_id,
                        (it.description or name)[:255],
                        (it.serialnumber or "")[:30] or None,
                        it.line,
                        round(it.quantity, 3),
                        round(it.cost_price, 2),
                        round(it.price, 2),
                        round(it.discount, 2),
                        it.discount_type,
                        it.item_location or _LOCATION_ID,
                        it.print_option,
                    ),
                )

                if stock_type == HAS_STOCK:
                    cur_qty = float(qty or 0)
                    new_stock = cur_qty - it.quantity
                    if new_stock < 0:
                        new_stock = 0.0
                        new_status = STOCK_IRREGULAR
                    elif new_stock == 0 and cur_qty > 0:
                        new_status = STOCK_ZERADO
                    elif new_stock == 0 and cur_qty <= 0:
                        new_status = STOCK_IRREGULAR
                    else:
                        new_status = STOCK_OK

                    await cur.execute(
                        """
                        INSERT INTO ospos_item_quantities (item_id, location_id, quantity, stock_status)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE quantity = VALUES(quantity), stock_status = VALUES(stock_status)
                        """,
                        (item_id, it.item_location or _LOCATION_ID, round(new_stock, 3), new_status),
                    )

                    await cur.execute(
                        """
                        INSERT INTO ospos_inventory
                            (trans_items, trans_user, trans_date, trans_comment, trans_location, trans_inventory)
                        VALUES (%s, %s, NOW(), %s, %s, %s)
                        """,
                        (
                            item_id, payload.employee_id,
                            f"POS {sale_id}",
                            it.item_location or _LOCATION_ID,
                            round(-it.quantity, 3),
                        ),
                    )

            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            logger.exception("PDV sale failed")
            raise HTTPException(status_code=500, detail=f"Falha ao gravar venda: {exc}")
        finally:
            await cur.close()

    logger.info("PDV: sale %d written by employee %d (%d items)", sale_id, payload.employee_id, len(payload.items))
    return {"success": True, "sale_id": sale_id, "total": round(total, 2)}