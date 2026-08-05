"""Async client for the OSPOS MySQL database used by the store API.

Provides the write-back of product photos into OSPOS
(``public/uploads/item_pics/{item_id}.png`` + ``pic_filename``) and the
``deleted`` status needed to resolve duplicate SKUs to the active item.
"""

from __future__ import annotations

import logging
from typing import Optional

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
