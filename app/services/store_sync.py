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
    last_modified: Optional[datetime]
    has_image: bool = False


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


# Remote (Render) rejects uploads above 5 MB — stay under with margin.
PUSH_IMAGE_MAX_BYTES = 4 * 1024 * 1024


def _downscale_image(path: Path, max_dim: int = 1600) -> bytes:
    """Re-encode an image below PUSH_IMAGE_MAX_BYTES for remote push."""
    from io import BytesIO

    from PIL import Image

    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    data = b""
    while max_dim >= 400:
        img.thumbnail((max_dim, max_dim))
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) < PUSH_IMAGE_MAX_BYTES:
            return data
        max_dim //= 2
    return data


# ── Sync Service ──────────────────────────────────────────────────────────

class StoreSync:
    """Syncs products from OSPOS MySQL into the local store_products table."""

    PRODUCT_FIELDS = (
        "items.item_id, items.name, items.description, items.unit_price, items.category, "
        "COALESCE(NULLIF(items.item_number, ''), NULLIF(items.item_number, 'NULL'), CAST(items.item_id AS CHAR)) AS sku, "
        "COALESCE((SELECT quantity FROM ospos_item_quantities q WHERE q.item_id = items.item_id AND q.location_id = 1), 0) AS stock, "
        "COALESCE(NULLIF(items.pic_filename, ''), NULLIF(items.pic_filename, 'NULL'), NULL) AS pic_filename, "
        "items.last_modified"
    )

    # Minimal columns for change detection (cheap query, runs every delta).
    DIFF_FIELDS = (
        "items.item_id, items.name, items.unit_price, "
        "COALESCE((SELECT quantity FROM ospos_item_quantities q WHERE q.item_id = items.item_id AND q.location_id = 1), 0) AS stock, "
        "COALESCE(NULLIF(items.pic_filename, ''), NULLIF(items.pic_filename, 'NULL'), NULL) AS pic_filename"
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

        await self._dedupe_store()

        logger.info(
            "StoreSync: done — %d created, %d updated, %d skipped, %d errors",
            self._stats["created"],
            self._stats["updated"],
            self._stats["skipped"],
            self._stats["errors"],
        )

        if settings.push_sync_url:
            await self._push_to_remote()

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
        upserted_ids: set[int] = set()

        # Get the max ospos_id we've synced
        async with async_session_factory() as session:
            result = await session.execute(
                select(StoreProduct.ospos_id)
            )
            existing_ids = {row[0] for row in result.all()}

        last_id = max(existing_ids, default=0)

        # Phase 1: items that are NEWER than the last synced id OR missing from
        # store_products entirely (e.g. products created before the sync
        # system existed, with item_id below the max already synced).
        rows = await self._fetch_ospos_items(
            min_stock=min_stock,
            since_id=last_id,
            exclude_ids=existing_ids,
        )
        if not rows:
            logger.info("StoreSync (delta): no new products since OSPOS id %d", last_id)

        async with async_session_factory() as session:
            for row in rows:
                try:
                    await self._upsert_product(session, row, only_with_images)
                    upserted_ids.add(row.item_id)
                except Exception as exc:
                    logger.error("StoreSync: error upserting OSPOS item %d: %s", row.item_id, exc)
                    self._stats["errors"] += 1

            await session.commit()

        # Phase 2: EXISTING items whose stock/price/name/photo changed.
        # Sales decrement ospos_item_quantities without bumping
        # items.last_modified, so compare live values instead of timestamps.
        changed_rows = await self._fetch_changed_existing()
        if changed_rows:
            async with async_session_factory() as session:
                for row in changed_rows:
                    try:
                        await self._upsert_product(session, row, only_with_images)
                        upserted_ids.add(row.item_id)
                    except Exception as exc:
                        logger.error("StoreSync: error upserting OSPOS item %d: %s", row.item_id, exc)
                        self._stats["errors"] += 1

                await session.commit()

        await self._dedupe_store()

        logger.info(
            "StoreSync (delta): done — %d created, %d updated, %d skipped, %d errors",
            self._stats["created"],
            self._stats["updated"],
            self._stats["skipped"],
            self._stats["errors"],
        )

        # Push only what actually changed — never the whole catalog.
        if settings.push_sync_url and upserted_ids:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(StoreProduct).where(StoreProduct.ospos_id.in_(upserted_ids))
                )
                pushed_products = list(result.scalars().all())
            await self._push_to_remote(pushed_products)

        return self._stats

    # ── Duplicate (barcode) resolution ───────────────────────────────

    async def _dedupe_store(self) -> None:
        """Ensure only ONE product per SKU is store_visible.

        When two or more store products share the same SKU (barcode),
        keep visible only the best candidate (most recent / most data).
        """
        from app.services.duplicate_rule import group_duplicates, pick_best_duplicate

        async with async_session_factory() as session:
            result = await session.execute(select(StoreProduct))
            products = result.scalars().all()

            groups = group_duplicates(products)
            if not groups:
                return

            visible_fixed = 0
            for sku, items in groups.items():
                best = pick_best_duplicate(items)
                if best is None:
                    continue
                for p in items:
                    should_be = p is best and p.stock > 0 and bool(p.image_url)
                    if p.store_visible != should_be:
                        p.store_visible = should_be
                        visible_fixed += 1

            await session.commit()
            if visible_fixed:
                logger.info("StoreSync (dedupe): adjusted visibility for %d product(s)", visible_fixed)

    # ── OSPOS MySQL access ───────────────────────────────────────────

    async def _fetch_ospos_items(
        self,
        min_stock: int = 0,
        since_id: int = 0,
        exclude_ids: set[int] | None = None,
        ids: set[int] | None = None,
    ) -> list[OSPOSRow]:
        """Fetch products from OSPOS MySQL.

        Only returns active (deleted=0) products with stock >= min_stock.

        With ``since_id`` set, returns items with ``item_id > since_id``.
        With ``exclude_ids`` set, also returns items whose ``item_id`` is
        NOT already in the local ``store_products`` table (catches products
        created before the sync existed, whose id is below the max synced id).
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
                    where_parts = [
                        "items.deleted = 0",
                        "COALESCE((SELECT quantity FROM ospos_item_quantities q WHERE q.item_id = items.item_id AND q.location_id = 1), 0) >= %s",
                    ]
                    params: list[Any] = [min_stock]

                    if ids is not None:
                        # Fetch full rows for a specific set of item_ids
                        # (used by the change-detection phase).
                        id_list = sorted(ids)
                        if not id_list:
                            return rows
                        chunk_size = 1000
                        in_sql = " OR ".join(
                            "items.item_id IN (" + ",".join(["%s"] * len(c)) + ")"
                            for c in (id_list[i:i + chunk_size] for i in range(0, len(id_list), chunk_size))
                        )
                        where_parts = ["items.deleted = 0", f"({in_sql})"]
                        params = []
                        for c in (id_list[i:i + chunk_size] for i in range(0, len(id_list), chunk_size)):
                            params.extend(c)
                    elif exclude_ids is not None:
                        # Fetch items that are NEW (id > since_id) OR MISSING
                        # from store_products entirely.
                        exclude_list = sorted(exclude_ids)
                        # Chunked NOT IN() to keep the query size manageable
                        chunk_size = 1000
                        chunks = [
                            exclude_list[i:i + chunk_size]
                            for i in range(0, len(exclude_list), chunk_size)
                        ]
                        not_in_sql = " AND ".join(
                            "items.item_id NOT IN (" + ",".join(["%s"] * len(c)) + ")"
                            for c in chunks
                        )

                        if since_id > 0:
                            # Placeholders order: since_id first, then chunk ids.
                            where_parts.append(f"(items.item_id > %s OR ({not_in_sql}))")
                            params.append(since_id)
                        else:
                            where_parts.append(f"({not_in_sql})")
                        for c in chunks:
                            params.extend(c)
                    elif since_id > 0:
                        where_parts.append("items.item_id > %s")
                        params.append(since_id)

                    where_sql = " AND ".join(where_parts)

                    sql = (
                        f"SELECT {self.PRODUCT_FIELDS} "
                        f"FROM ospos_items AS items "
                        f"WHERE {where_sql} "
                        f"ORDER BY items.item_id ASC"
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
                            # MySQL quantity is DECIMAL — SQLite refuses to bind
                            # Decimal, so coerce to int here (whole units).
                            stock=int(row[6] or 0),
                            pic_filename=row[7],
                            last_modified=row[8],
                        ))
        finally:
            pool.close()
            await pool.wait_closed()

        return rows

    @staticmethod
    def _pic_covers(pic_filename: str | None, image_url: str | None) -> bool:
        """True if the local image already reflects the OSPOS pic_filename.

        Locally-uploaded images (product_*.png managed outside OSPOS) are
        treated as covered so they don't flap as 'changed' on every diff.
        """
        pic = (pic_filename or "").strip()
        if not pic or pic.lower() in ("null", "none"):
            return True
        fname = (image_url or "").rsplit("/", 1)[-1]
        if fname.startswith("product_"):
            return True
        return fname == pic

    async def _fetch_changed_existing(self) -> list[OSPOSRow]:
        """Detect EXISTING items whose stock/price/name/photo changed in OSPOS.

        Sales decrement ospos_item_quantities without touching
        items.last_modified, so a timestamp filter can't catch them —
        compare live values instead. Returns full rows only for the
        items that actually differ from store_products.
        """
        import aiomysql  # type: ignore[import-untyped]

        async with async_session_factory() as session:
            result = await session.execute(
                select(
                    StoreProduct.ospos_id,
                    StoreProduct.name,
                    StoreProduct.price,
                    StoreProduct.stock,
                    StoreProduct.image_url,
                )
            )
            local_rows = result.all()

        local = {
            r.ospos_id: (r.name, float(r.price or 0), int(r.stock or 0), r.image_url)
            for r in local_rows
        }

        pool = await aiomysql.create_pool(
            host=settings.ospos_db_host,
            port=settings.ospos_db_port,
            user=settings.ospos_db_user,
            password=settings.ospos_db_pass,
            db=settings.ospos_db_name,
            charset="utf8",
            autocommit=True,
        )

        changed_ids: list[int] = []
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    sql = (
                        f"SELECT {self.DIFF_FIELDS} "
                        f"FROM ospos_items AS items "
                        f"WHERE items.deleted = 0 "
                        f"ORDER BY items.item_id ASC"
                    )
                    await cur.execute(sql)
                    fetched = await cur.fetchall()

                    for item_id, name, price, stock, pic in fetched:
                        loc = local.get(item_id)
                        if loc is None:
                            continue  # new item — handled by phase 1
                        l_name, l_price, l_stock, l_img = loc
                        stock_i = int(stock or 0)
                        if (
                            stock_i != l_stock
                            or abs(float(price or 0) - l_price) > 0.005
                            or _clean_product_name(name or "") != l_name
                            or not self._pic_covers(pic, l_img)
                        ):
                            changed_ids.append(item_id)
        finally:
            pool.close()
            await pool.wait_closed()

        if not changed_ids:
            return []

        logger.info(
            "StoreSync (delta): %d existing product(s) changed in OSPOS",
            len(changed_ids),
        )
        return await self._fetch_ospos_items(ids=set(changed_ids))

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

        # Check if product already exists (by ospos_id) — needed before the
        # image fallback so we can preserve locally-uploaded images across syncs.
        result = await session.execute(
            select(StoreProduct).where(StoreProduct.ospos_id == row.item_id)
        )
        existing = result.scalar_one_or_none()

        # If OSPOS has no pic_filename, look for a locally-uploaded image:
        #   1. an image_url already stored on the local row (if file exists), or
        #   2. a file named product_{local_id}.{ext} or product_{ospos_id}.{ext}
        #      on disk (the naming used by the store/capture upload endpoint).
        # This prevents the sync from wiping uploads whose source is not in OSPOS.
        if image_url is None:
            if existing is not None and existing.image_url:
                local_url = existing.image_url
                fname = local_url.rsplit("/", 1)[-1]
                if (IMAGE_DIR / fname).exists():
                    image_url = local_url
                    has_image = True
            if image_url is None:
                for pid in {existing.id if existing else None, row.item_id}:
                    if pid is None:
                        continue
                    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                        candidate = IMAGE_DIR / f"product_{pid}{ext}"
                        if candidate.exists():
                            image_url = f"/v1/store/images/{candidate.name}"
                            has_image = True
                            break
                    if has_image:
                        break

        # If only_with_images and no image → skip
        if only_with_images and not has_image:
            self._stats["skipped"] += 1
            return

        clean_name = _clean_product_name(row.name)

        # store_visible requires BOTH stock > 0 AND image present
        store_visible = row.stock > 0 and has_image

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
            existing.image_url = image_url
            existing.last_modified = row.last_modified
            existing.last_sync_at = now
            existing.updated_at = now

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
                last_modified=row.last_modified,
                last_sync_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(product)
            self._stats["created"] += 1

    # ── Push sync (local → remote Render) ────────────────────────────

    async def _push_to_remote(self, products: list | None = None) -> None:
        """Push local products to the remote Render instance.

        With ``products`` set, pushes only those (delta mode). With ``None``,
        pushes the whole local catalog (full sync / initial population).
        """
        import httpx

        url = settings.push_sync_url.rstrip("/")
        api_key = settings.push_sync_api_key or settings.api_key

        try:
            if products is None:
                async with async_session_factory() as session:
                    result = await session.execute(select(StoreProduct))
                    products = result.scalars().all()

            if not products:
                logger.info("Push sync: no products to push")
                return

            logger.info("Push sync: pushing %d products to %s", len(products), url)

            batch_size = 100
            total_created = 0
            total_updated = 0

            for i in range(0, len(products), batch_size):
                batch = products[i:i + batch_size]
                payload = {
                    "products": [
                        {
                            "ospos_id": p.ospos_id,
                            "sku": p.sku,
                            "name": p.name,
                            "description": p.description or "",
                            "price": p.price,
                            "category": p.category,
                            "stock": p.stock,
                            "image_url": p.image_url,
                            "store_visible": p.store_visible,
                            "last_modified": p.last_modified.isoformat() if p.last_modified else None,
                        }
                        for p in batch
                    ],
                    "api_key": api_key,
                }

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(f"{url}/v1/store/sync/push", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    total_created += data.get("created", 0)
                    total_updated += data.get("updated", 0)

            logger.info("Push sync: done — %d created, %d updated on remote", total_created, total_updated)

            await self._push_images_to_remote(url, api_key, products)

        except Exception as exc:
            logger.warning("Push sync failed (non-fatal): %s", exc)

    async def _push_images_to_remote(
        self, url: str, api_key: str, products: list
    ) -> None:
        """Push local images that aren't already available on the remote."""
        import httpx

        from app.services import r2_storage

        pushed = 0
        failed = 0

        async with httpx.AsyncClient(timeout=60) as client:
            for p in products:
                if not p.image_url:
                    continue

                filename = p.image_url.rsplit("/", 1)[-1]
                local_path = IMAGE_DIR / filename
                if not local_path.exists():
                    continue

                # Skip images the remote already serves (R2 or local fallback).
                if p.image_url.startswith("/v1/store/images/"):
                    r2_key = p.image_url.removeprefix("/v1/store/images/")
                    try:
                        head = await client.head(f"{url}/v1/store/images/{r2_key}")
                        if head.status_code == 200:
                            continue
                    except Exception:
                        pass  # remote unreachable — try the upload anyway

                data = local_path.read_bytes()
                orig_size = len(data)

                # Remote rejects files above 5 MB (MAX_IMAGE_SIZE) — downscale.
                if len(data) > PUSH_IMAGE_MAX_BYTES:
                    try:
                        data = await asyncio.to_thread(_downscale_image, local_path)
                        logger.info(
                            "Push image: %s downscaled %.1fMB -> %.1fMB",
                            filename,
                            orig_size / 1e6,
                            len(data) / 1e6,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Push image: downscale failed for %s: %s", filename, exc
                        )
                        failed += 1
                        continue

                try:
                    resp = await client.post(
                        f"{url}/v1/store/sync/push-image",
                        params={"ospos_id": p.ospos_id, "api_key": api_key},
                        files={
                            "file": (
                                filename,
                                data,
                                r2_storage.get_content_type(filename),
                            )
                        },
                    )
                    resp.raise_for_status()
                    pushed += 1
                except Exception as exc:
                    failed += 1
                    logger.debug(
                        "Push image failed for ospos_id %d: %s", p.ospos_id, exc
                    )

        if pushed:
            logger.info("Push sync: pushed %d images to remote", pushed)
        if failed:
            logger.warning(
                "Push sync: %d image push(es) failed — if AccessDenied, "
                "the remote's R2 credentials lack write permission",
                failed,
            )
