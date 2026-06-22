"""Store Sync — pulls products from OSPOS MySQL into local store_products table.

Usage:
    sync = StoreSync()
    result = await sync.run()  # sync all products
    result = await sync.run_delta()  # sync only changed products
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models.store_product import StoreProduct

logger = logging.getLogger(__name__)

# ── Image storage ─────────────────────────────────────────────────────────
IMAGE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "images"


@dataclass
class OSPOSRow:
    """Raw row from ospos_items query."""
    item_id: int
    name: str
    description: str
    unit_price: float
    category: str
    sku: str
    stock: int
    pic_filename: Optional[str]


# ── Normalisation helpers (copied from store.py) ─────────────────────────

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


def _clean_product_name(name: str) -> str:
    """Remove marca/volume do nome para exibição limpa na loja.

    Exemplo: "CAMISETA BASICA PRETA (MarcaX) - 300ml" → "Camiseta Basica Preta"
    """
    import re
    # Remove conteúdo entre parênteses que pareça marca/volume
    name = re.sub(r'\([^)]*(Marca|marca|Vol|vol|ml|ML|LT|lt|Lt|gr|GR|kg|KG)[^)]*\)', '', name).strip()
    # Remove traços no final
    name = re.sub(r'\s*-\s*$', '', name).strip()
    return name


# ── Sync Service ──────────────────────────────────────────────────────────

class StoreSync:
    """Syncs products from OSPOS MySQL into the local store_products table."""

    PRODUCT_FIELDS = (
        "item_id, name, description, unit_price, category, "
        "COALESCE(NULLIF(item_number, ''), NULLIF(item_number, 'NULL'), CAST(item_id AS CHAR)) AS sku, "
        "CAST(receiving_quantity AS SIGNED) AS stock, "
        "COALESCE(NULLIF(pic_filename, ''), NULLIF(pic_filename, 'NULL'), NULL) AS pic_filename"
    )

    def __init__(self) -> None:
        self._stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    async def run(
        self,
        *,
        only_with_images: bool = False,
        min_stock: int = 0,
    ) -> dict[str, int]:
        """Full sync: fetch ALL active products from OSPOS and upsert into local DB.

        Args:
            only_with_images: Only sync products that have a pic_filename.
            min_stock: Minimum stock quantity (default 0 = all).

        Returns:
            Dict with created/updated/skipped/errors counts.
        """
        self._stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

        rows = await self._fetch_ospos_items(min_stock=min_stock)
        if not rows:
            logger.info("StoreSync: no products returned from OSPOS")
            return self._stats

        logger.info("StoreSync: fetched %d products from OSPOS", len(rows))

        async with async_session_factory() as session:
            for row in rows:
                try:
                    await self._upsert_product(session, row, only_with_images)
                except Exception as exc:
                    logger.error("StoreSync: error upserting OSPOS item %d: %s", row.item_id, exc)
                    self._stats["errors"] += 1

            await session.commit()

        logger.info(
            "StoreSync: done — %d created, %d updated, %d skipped, %d errors",
            self._stats["created"],
            self._stats["updated"],
            self._stats["skipped"],
            self._stats["errors"],
        )
        return self._stats

    async def run_delta(
        self,
        *,
        only_with_images: bool = False,
        min_stock: int = 0,
    ) -> dict[str, int]:
        """Delta sync: only process products changed since last sync.

        Uses incremental item_id polling (CDC-style).
        """
        self._stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

        # Get the max ospos_id we've synced
        async with async_session_factory() as session:
            result = await session.execute(
                select(StoreProduct.ospos_id)
                .order_by(StoreProduct.ospos_id.desc())
                .limit(1)
            )
            last_row = result.scalar_one_or_none()
            last_id = last_row or 0

        rows = await self._fetch_ospos_items(
            min_stock=min_stock,
            since_id=last_id,
        )
        if not rows:
            logger.info("StoreSync (delta): no new/changed products since OSPOS id %d", last_id)
            return self._stats

        logger.info(
            "StoreSync (delta): fetched %d products since OSPOS id %d",
            len(rows), last_id,
        )

        async with async_session_factory() as session:
            for row in rows:
                try:
                    await self._upsert_product(session, row, only_with_images)
                except Exception as exc:
                    logger.error("StoreSync: error upserting OSPOS item %d: %s", row.item_id, exc)
                    self._stats["errors"] += 1

            await session.commit()

        logger.info(
            "StoreSync (delta): done — %d created, %d updated, %d skipped, %d errors",
            self._stats["created"],
            self._stats["updated"],
            self._stats["skipped"],
            self._stats["errors"],
        )
        return self._stats

    # ── OSPOS MySQL access ───────────────────────────────────────────

    async def _fetch_ospos_items(
        self,
        min_stock: int = 0,
        since_id: int = 0,
    ) -> list[OSPOSRow]:
        """Fetch products from OSPOS MySQL.

        Only returns active (deleted=0) products with stock >= min_stock.
        """
        import aiomysql  # type: ignore[import-untyped]

        pool = await aiomysql.create_pool(
            host=settings.ospos_db_host,
            port=settings.ospos_db_port,
            user=settings.ospos_db_user,
            password=settings.ospos_db_pass,
            db=settings.ospos_db_name,
            charset="utf8",
            autocommit=True,
        )

        rows: list[OSPOSRow] = []
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    where_parts = ["deleted = 0", "receiving_quantity >= %s"]
                    params: list[Any] = [min_stock]

                    if since_id > 0:
                        where_parts.append("item_id > %s")
                        params.append(since_id)

                    where_sql = " AND ".join(where_parts)

                    sql = (
                        f"SELECT {self.PRODUCT_FIELDS} "
                        f"FROM ospos_items "
                        f"WHERE {where_sql} "
                        f"ORDER BY item_id ASC"
                    )
                    await cur.execute(sql, tuple(params))
                    fetched = await cur.fetchall()

                    for row in fetched:
                        rows.append(OSPOSRow(
                            item_id=row[0],
                            name=row[1],
                            description=row[2] or "",
                            unit_price=float(row[3]),
                            category=row[4],
                            sku=row[5],
                            stock=row[6],
                            pic_filename=row[7],
                        ))
        finally:
            pool.close()
            await pool.wait_closed()

        return rows

    # ── Upsert logic ─────────────────────────────────────────────────

    def _image_exists(self, pic_filename: str | None) -> bool:
        """Check if the image file exists on local disk."""
        if not pic_filename or pic_filename.strip() in ("", "NULL", "null"):
            return False
        return (IMAGE_DIR / pic_filename.strip()).exists()

    def _build_image_url(self, pic_filename: str | None) -> str | None:
        """Build public image URL if file exists locally."""
        if not pic_filename or pic_filename.strip() in ("", "NULL", "null"):
            return None
        filename = pic_filename.strip()
        if (IMAGE_DIR / filename).exists():
            return f"/v1/store/images/{filename}"
        return None

    async def _upsert_product(
        self,
        session: AsyncSession,
        row: OSPOSRow,
        only_with_images: bool,
    ) -> None:
        """Insert or update a single product in store_products."""
        image_url = self._build_image_url(row.pic_filename)
        has_image = image_url is not None

        # If only_with_images and no image → skip
        if only_with_images and not has_image:
            self._stats["skipped"] += 1
            return

        # Determine store_visibility
        store_visible = has_image and row.stock > 0

        clean_name = _clean_product_name(row.name)

        # Check if product already exists (by ospos_id)
        result = await session.execute(
            select(StoreProduct).where(StoreProduct.ospos_id == row.item_id)
        )
        existing = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if existing:
            # Update
            existing.name = clean_name
            existing.description = row.description
            existing.price = row.unit_price
            existing.category = row.category
            existing.stock = row.stock
            existing.sku = row.sku
            existing.store_visible = store_visible
            existing.last_sync_at = now
            existing.updated_at = now

            # Only update image_url if we have one (don't overwrite with None)
            if has_image:
                existing.image_url = image_url
            elif existing.image_url is None and only_with_images:
                # Product lost its image and we're in strict mode
                existing.store_visible = False

            self._stats["updated"] += 1
        else:
            # Create
            product = StoreProduct(
                ospos_id=row.item_id,
                sku=row.sku,
                name=clean_name,
                description=row.description,
                price=row.unit_price,
                category=row.category,
                stock=row.stock,
                image_url=image_url,
                store_visible=store_visible,
                last_sync_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(product)
            self._stats["created"] += 1
