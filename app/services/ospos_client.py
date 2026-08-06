"""Async client for the OSPOS MySQL database used by the store API.

Provides the write-back of product photos into OSPOS
(``public/uploads/item_pics/{item_id}.png`` + ``pic_filename``) and the
``deleted`` status needed to resolve duplicate SKUs to the active item.
"""

from __future__ import annotations

import logging
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
                "SELECT i.item_id, i.item_number, i.name, i.category, i.description, "
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
