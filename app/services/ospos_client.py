"""Async client for the OSPOS MySQL database used by the store API.

Provides the write-back of product photos into OSPOS
(``public/uploads/item_pics/{item_id}.png`` + ``pic_filename``) and the
``deleted`` status needed to resolve duplicate SKUs to the active item.
"""

from __future__ import annotations

import logging
from html import unescape as html_unescape
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_OSPOS_POOL = None


async def _pool():
    """Lazily build a shared aiomysql pool from the OSPOS settings."""
    global _OSPOS_POOL
    if _OSPOS_POOL is None:
        import aiomysql

        _OSPOS_POOL = await aiomysql.create_pool(
            host=settings.ospos_db_host,
            port=settings.ospos_db_port,
            user=settings.ospos_db_user,
            password=settings.ospos_db_pass,
            db=settings.ospos_db_name,
            charset="utf8",
            autocommit=True,
            maxsize=5,
        )
    return _OSPOS_POOL


async def item_deleted_map(item_ids: list[int]) -> dict[int, bool]:
    """Return ``{item_id: deleted}`` for the given OSPOS item ids.

    Item ids that do not exist in OSPOS are simply absent from the dict.
    """
    result: dict[int, bool] = {}
    if not item_ids:
        return result

    pool = await _pool()
    placeholders = ",".join(["%s"] * len(item_ids))
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT item_id, deleted FROM ospos_items WHERE item_id IN ({placeholders})",
                tuple(item_ids),
            )
            async for row in cur:
                result[row[0]] = bool(row[1])
    return result


async def find_active_item_by_barcode(sku: str) -> Optional[int]:
    """Return the first non-deleted OSPOS item carrying this barcode."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT item_id FROM ospos_items WHERE item_number=%s AND deleted=0 "
                "ORDER BY item_id LIMIT 1",
                (sku,),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def resolve_photo_target(ospos_id: int, sku: str) -> Optional[int]:
    """Resolve which OSPOS item should receive a product photo.

    Priority:
    1. The mapped item, when it exists and is not deleted.
    2. Otherwise the first active item carrying the same barcode.
    3. Otherwise ``None`` (no target — the photo cannot be written back).
    """
    deleted = await item_deleted_map([ospos_id])
    if ospos_id in deleted and not deleted[ospos_id]:
        return ospos_id
    return await find_active_item_by_barcode(sku)


async def set_pic_filename(item_id: int, filename: str) -> None:
    """Update ``ospos_items.pic_filename`` for an item."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ospos_items SET pic_filename=%s WHERE item_id=%s",
                (filename, item_id),
            )


async def fetch_items_total(
    limit: int = 1000,
    offset: int = 0,
    include_deleted: bool = False,
    since: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Read a page of product data directly from the OSPOS MySQL DB.

    Used by ``GET /v1/store/sync-total`` so another PC can pull the full
    catalog (names, prices, stock, photo filename, last_modified, ...) on
    demand. Returns ``(rows, total_count_rows)``.

    ``since`` (optional ``YYYY-MM-DD HH:MM:SS``) restricts to items whose
    ``last_modified`` is newer; only items touched via the items form carry
    ``last_modified``, so it is a best-effort delta, not a full change log.
    """
    pool = await _pool()
    if include_deleted:
        where = "WHERE (i.deleted = 0 OR i.deleted = 1)"
    else:
        where = "WHERE i.deleted = 0"

    params: list[Any] = []
    if since:
        where += " AND (i.last_modified >= %s OR i.last_modified IS NULL)"
        params.append(since)

    rows: list[dict] = []
    count = 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            query = (
                "SELECT i.item_id, COALESCE(i.item_number,'') AS item_number, i.name, i.category, i.description, "
                "i.cost_price, i.unit_price, i.pic_filename, i.last_modified, i.deleted, "
                "COALESCE(q.total_qty, 0) AS stock "
                "FROM ospos_items i "
                "LEFT JOIN (SELECT item_id, SUM(quantity) AS total_qty "
                "           FROM ospos_item_quantities GROUP BY item_id) q "
                "ON q.item_id = i.item_id "
                + where +
                " ORDER BY i.item_id ASC "
                "LIMIT %s, %s"
            )
            cur_params = list(params) + [offset, limit]  # MySQL LIMIT offset, count
            await cur.execute(query, cur_params)
            cols = [d[0] for d in cur.description]
            async for row in cur:
                rows.append(dict(zip(cols, row)))

            # total count for the same (non-paged) filter
            count_sql = (
                "SELECT COUNT(*) FROM ospos_items i " + where
            )
            await cur.execute(count_sql, params)
            count_row = await cur.fetchone()
            count = count_row[0] if count_row else 0
    return rows, count


# ── Dashboard queries ──────────────────────────────────────────────────────


async def fetch_dashboard_summary(
    period: str = "today",
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch sales KPIs for the given period.

    period: "today" | "yesterday" | "week" | "month" | "custom"
    custom_start / custom_end: "YYYY-MM-DD" when period="custom"
    """
    pool = await _pool()

    # Build date filter
    date_where, params = _build_date_filter(period, custom_start, custom_end)

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 1. Sales total for period (mirrors OSPOS sales/manage: sums received payments)
            query = f"""
                SELECT ROUND(SUM(sp.payment_amount - sp.cash_refund), 2) AS total
                FROM ospos_sales_payments sp
                JOIN ospos_sales s ON s.sale_id = sp.sale_id
                WHERE s.sale_status = 0 AND sp.payment_amount > 0 {date_where}
            """
            await cur.execute(query, params)
            row = await cur.fetchone()
            sales_total = float(row[0]) if row and row[0] is not None else 0.0

            # 1b. Same total for the previous equivalent period (comparison)
            prev_where, prev_params = _build_prev_date_filter(period, custom_start, custom_end)
            await cur.execute(
                f"""
                SELECT ROUND(SUM(sp.payment_amount - sp.cash_refund), 2) AS total
                FROM ospos_sales_payments sp
                JOIN ospos_sales s ON s.sale_id = sp.sale_id
                WHERE s.sale_status = 0 AND sp.payment_amount > 0 {prev_where}
                """,
                prev_params,
            )
            row = await cur.fetchone()
            prev_sales_total = float(row[0]) if row and row[0] is not None else 0.0
            if prev_sales_total > 0:
                change_pct = round((sales_total - prev_sales_total) / prev_sales_total * 100)
            elif sales_total > 0:
                change_pct = 100
            else:
                change_pct = 0

            # 2. Transactions count
            query = f"""
                SELECT COUNT(DISTINCT s.sale_id)
                FROM ospos_sales s
                WHERE s.sale_status = 0 {date_where}
            """
            await cur.execute(query, params)
            row = await cur.fetchone()
            transactions = int(row[0]) if row and row[0] is not None else 0

            # 3. Items sold (distinct)
            query = f"""
                SELECT COUNT(DISTINCT si.item_id)
                FROM ospos_sales_items si
                JOIN ospos_sales s ON s.sale_id = si.sale_id
                WHERE s.sale_status = 0 {date_where}
            """
            await cur.execute(query, params)
            row = await cur.fetchone()
            items_sold = int(row[0]) if row and row[0] is not None else 0

            # 4. Avg ticket
            avg_ticket = sales_total / transactions if transactions > 0 else 0.0

            # 5. Top 5 items by quantity
            query = f"""
                SELECT i.item_id, i.name, SUM(si.quantity_purchased) AS qty,
                       ROUND(SUM(
                           CASE WHEN si.discount_type = 1 THEN si.quantity_purchased * (si.item_unit_price - si.discount)
                           ELSE si.quantity_purchased * si.item_unit_price - ROUND(si.quantity_purchased * si.item_unit_price * si.discount / 100, 2) END
                       ), 2) AS revenue
                FROM ospos_sales_items si
                JOIN ospos_sales s ON s.sale_id = si.sale_id
                JOIN ospos_items i ON i.item_id = si.item_id
                WHERE s.sale_status = 0 {date_where}
                GROUP BY si.item_id
                ORDER BY qty DESC
                LIMIT 5
            """
            await cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            top_items = [dict(zip(cols, row)) async for row in cur]

            # 6. Hourly sales (only for today/yesterday single day periods)
            hourly = [0.0] * 24
            hourly_max = 0.0
            if period in ("today", "yesterday") or (period == "custom" and custom_start == custom_end):
                query = f"""
                    SELECT HOUR(s.sale_time) AS hour,
                           ROUND(SUM(sp.payment_amount - sp.cash_refund), 2) AS total
                    FROM ospos_sales_payments sp
                    JOIN ospos_sales s ON s.sale_id = sp.sale_id
                    WHERE s.sale_status = 0 AND sp.payment_amount > 0 {date_where}
                    GROUP BY HOUR(s.sale_time)
                    ORDER BY hour ASC
                """
                await cur.execute(query, params)
                async for row in cur:
                    hour = int(row[0])
                    total = float(row[1])
                    hourly[hour] = total
                    if total > hourly_max:
                        hourly_max = total

            # 7. Daily sales target from app_config (adjusted for period length)
            await cur.execute("SELECT value FROM ospos_app_config WHERE `key` = 'daily_sales_target'")
            row = await cur.fetchone()
            daily_target_raw = float(row[0]) if row and row[0] else 0.0
            # Compute period length in days to scale the target
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            if period == "today":
                period_days = 1
            elif period == "yesterday":
                period_days = 1
            elif period == "week":
                period_days = 7
            elif period == "month":
                period_days = _today.day  # days elapsed this month
            elif period == "custom" and custom_start:
                try:
                    _s = _date.fromisoformat(custom_start)
                    _e = _date.fromisoformat(custom_end or custom_start)
                    period_days = max(1, (_e - _s).days + 1)
                except ValueError:
                    period_days = 1
            else:
                period_days = 1
            daily_target = daily_target_raw * period_days
            target_pct = min(100, round(sales_total / daily_target * 100)) if daily_target > 0 else 0

            # 8. Pending receivables (fiado)
            query = f"""
                SELECT IFNULL(SUM(sp.payment_amount), 0)
                FROM ospos_sales_payments sp
                JOIN ospos_sales s ON s.sale_id = sp.sale_id
                WHERE sp.payment_type = 'Fiado' {date_where}
            """
            await cur.execute(query, params)
            row = await cur.fetchone()
            pending_receivables = float(row[0]) if row and row[0] is not None else 0.0

            # 9. Totals grouped by payment type (mirrors OSPOS sales/manage summary)
            query = f"""
                SELECT sp.payment_type, COUNT(sp.payment_amount) AS cnt,
                       ROUND(SUM(sp.payment_amount - sp.cash_refund), 2) AS total
                FROM ospos_sales_payments sp
                JOIN ospos_sales s ON s.sale_id = sp.sale_id
                WHERE s.sale_status = 0 AND sp.payment_amount > 0 {date_where}
                GROUP BY sp.payment_type
                ORDER BY total DESC
            """
            await cur.execute(query, params)
            payment_summary = [
                {
                    "payment_type": html_unescape(row[0] or "Sem tipo"),
                    "count": int(row[1] or 0),
                    "total": float(row[2] or 0.0),
                }
                async for row in cur
            ]

    return {
        "period": period,
        "sales_total": sales_total,
        "transactions": transactions,
        "items_sold": items_sold,
        "avg_ticket": round(avg_ticket, 2),
        "top_items": top_items,
        "hourly_sales": hourly,
        "hourly_max": hourly_max,
        "daily_target": daily_target,
        "target_pct": target_pct,
        "pending_receivables": pending_receivables,
        "prev_sales_total": prev_sales_total,
        "change_pct": change_pct,
        "payment_summary": payment_summary,
    }


async def fetch_stock_alerts(limit: int = 20) -> list[dict]:
    """Fetch items with ZERADO or IRREGULAR stock status."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            query = """
                SELECT i.item_id, i.name, COALESCE(i.item_number, '') AS item_number, i.reorder_level,
                       iq.quantity, iq.stock_status, iq.location_id
                FROM ospos_items i
                JOIN ospos_item_quantities iq ON iq.item_id = i.item_id
                WHERE i.stock_type = 0  -- 0 = item with stock (1 = non-stockable)
                  AND i.deleted = 0
                  AND iq.stock_status IN (1, 2)  -- 1=ZERADO, 2=IRREGULAR
                ORDER BY iq.stock_status DESC, iq.quantity ASC
                LIMIT %s
            """
            await cur.execute(query, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) async for row in cur]


async def fetch_new_sales(after_sale_id: int, limit: int = 50) -> list[dict]:
    """Fetch completed sales with ``sale_id > after_sale_id`` (oldest first).

    Used by the dashboard poller to detect individual new sales and push
    them one by one (for real-time notifications).
    """
    pool = await _pool()
    rows: list[dict] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.sale_id, s.sale_time,
                       TRIM(CONCAT(COALESCE(p.first_name, ''), ' ', COALESCE(p.last_name, ''))) AS customer,
                       COUNT(DISTINCT si.item_id) AS items_count,
                       ROUND(SUM(
                           CASE WHEN si.discount_type = 1 THEN si.quantity_purchased * (si.item_unit_price - si.discount)
                           ELSE si.quantity_purchased * si.item_unit_price - ROUND(si.quantity_purchased * si.item_unit_price * si.discount / 100, 2) END
                       ), 2) AS total
                FROM ospos_sales s
                JOIN ospos_sales_items si ON si.sale_id = s.sale_id
                LEFT JOIN ospos_customers c ON c.person_id = s.customer_id
                LEFT JOIN ospos_people p ON p.person_id = c.person_id
                WHERE s.sale_status = 0 AND s.sale_id > %s
                GROUP BY s.sale_id
                ORDER BY s.sale_id ASC
                LIMIT %s
                """,
                (after_sale_id, limit),
            )
            cols = [d[0] for d in cur.description]
            async for row in cur:
                item = dict(zip(cols, row))
                if item.get("sale_time") is not None:
                    item["sale_time"] = item["sale_time"].strftime("%Y-%m-%d %H:%M:%S")
                rows.append(item)
    return rows


async def fetch_recent_sales(
    limit: int = 10,
    period: str = "today",
    custom_start: str | None = None,
    custom_end: str | None = None,
) -> list[dict]:
    """Fetch the last ``limit`` completed sales (newest first), filtered by period."""
    date_where, params = _build_date_filter(period, custom_start, custom_end)
    pool = await _pool()
    rows: list[dict] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT s.sale_id, s.sale_time,
                       TRIM(CONCAT(COALESCE(p.first_name, ''), ' ', COALESCE(p.last_name, ''))) AS customer,
                       COUNT(DISTINCT si.item_id) AS items_count,
                       ROUND(SUM(
                           CASE WHEN si.discount_type = 1 THEN si.quantity_purchased * (si.item_unit_price - si.discount)
                           ELSE si.quantity_purchased * si.item_unit_price - ROUND(si.quantity_purchased * si.item_unit_price * si.discount / 100, 2) END
                       ), 2) AS total
                FROM ospos_sales s
                JOIN ospos_sales_items si ON si.sale_id = s.sale_id
                LEFT JOIN ospos_customers c ON c.person_id = s.customer_id
                LEFT JOIN ospos_people p ON p.person_id = c.person_id
                WHERE s.sale_status = 0 {date_where}
                GROUP BY s.sale_id
                ORDER BY s.sale_id DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            cols = [d[0] for d in cur.description]
            async for row in cur:
                item = dict(zip(cols, row))
                if item.get("sale_time") is not None:
                    item["sale_time"] = item["sale_time"].strftime("%Y-%m-%d %H:%M:%S")
                rows.append(item)
    return rows


async def fetch_max_sale_id() -> int:
    """Return the highest ``sale_id`` in ``ospos_sales`` (0 when empty)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(sale_id), 0) FROM ospos_sales")
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def fetch_stock_alert_count() -> int:
    """Count of items with ZERADO or IRREGULAR stock status."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT COUNT(*)
                FROM ospos_items i
                JOIN ospos_item_quantities iq ON iq.item_id = i.item_id
                WHERE i.stock_type = 0  -- 0 = item with stock (1 = non-stockable)
                  AND i.deleted = 0
                  AND iq.stock_status IN (1, 2)
            """)
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] else 0


def _build_date_filter(period: str, custom_start: Optional[str], custom_end: Optional[str]) -> tuple[str, list]:
    """Build WHERE clause and params for date filtering on sales.sale_time."""
    from datetime import date, timedelta

    today = date.today()
    params: list = []

    if period == "today":
        return "AND DATE(s.sale_time) = %s", [today]
    elif period == "yesterday":
        return "AND DATE(s.sale_time) = %s", [today - timedelta(days=1)]
    elif period == "week":
        # Last 7 days (rolling), more useful than calendar Mon-Sun for shop owners
        return "AND DATE(s.sale_time) >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)", []
    elif period == "month":
        return "AND MONTH(s.sale_time) = %s AND YEAR(s.sale_time) = %s", [today.month, today.year]
    elif period == "custom" and custom_start:
        if not custom_end:
            custom_end = custom_start
        return "AND DATE(s.sale_time) BETWEEN %s AND %s", [custom_start, custom_end]
    else:
        return "AND DATE(s.sale_time) = %s", [today]


def _build_prev_date_filter(period: str, custom_start: Optional[str], custom_end: Optional[str]) -> tuple[str, list]:
    """Build WHERE clause/params for the previous equivalent period.

    Mirrors ``_build_date_filter`` so KPIs can be compared (e.g. today vs
    yesterday, this month vs last month).
    """
    from datetime import date, timedelta

    today = date.today()
    params: list = []

    if period == "today":
        return "AND DATE(s.sale_time) = %s", [today - timedelta(days=1)]
    elif period == "yesterday":
        return "AND DATE(s.sale_time) = %s", [today - timedelta(days=2)]
    elif period == "week":
        # Previous 7-day window (days 7–13 ago)
        return "AND DATE(s.sale_time) BETWEEN DATE_SUB(CURDATE(), INTERVAL 13 DAY) AND DATE_SUB(CURDATE(), INTERVAL 7 DAY)", []
    elif period == "month":
        if today.month == 1:
            return "AND MONTH(s.sale_time) = 12 AND YEAR(s.sale_time) = %s", [today.year - 1]
        return "AND MONTH(s.sale_time) = %s AND YEAR(s.sale_time) = %s", [today.month - 1, today.year]
    elif period == "custom" and custom_start:
        from datetime import datetime as dt

        try:
            start = dt.strptime(custom_start, "%Y-%m-%d").date()
            end = dt.strptime(custom_end or custom_start, "%Y-%m-%d").date()
        except ValueError:
            return "AND DATE(s.sale_time) = %s", [today - timedelta(days=1)]
        span = (end - start).days
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span)
        return "AND DATE(s.sale_time) BETWEEN %s AND %s", [prev_start, prev_end]
    else:
        return "AND DATE(s.sale_time) = %s", [today - timedelta(days=1)]
