#!/usr/bin/env python3
"""Seed do banco para o Render (ou qualquer ambiente sem o OSPOS local).

Reconstrói a tabela ``store_products`` a partir do catálogo versionado no git
(``data/sync/catalog.json``) e das fotos baixadas pelo sync
(``data/sync/photos/ospos-item-images/``).

Isso permite rodar o inventory-service num Web Service do Render sem acesso
ao MySQL do OSPOS (que fica na rede local da loja).

Fluxo:
  1. Lê data/sync/catalog.json (produtos + pic_filename)
  2. Copia fotos de data/sync/photos/ospos-item-images/ para data/images/
  3. Popula store_products com store_visible = (stock > 0 AND tem foto)
  4. Idempotente: upsert por ospos_id, não apaga nada

Uso:
    python3 scripts/seed_render.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "data" / "sync" / "catalog.json"
SYNC_PHOTOS = BASE_DIR / "data" / "sync" / "photos" / "ospos-item-images"
IMAGES_DIR = BASE_DIR / "data" / "images"
DB_PATH = BASE_DIR / "data" / "inventory.db"


def _db_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Cria a tabela store_products se não existir (compat com alembic)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_products (
            id INTEGER NOT NULL PRIMARY KEY,
            ospos_id INTEGER NOT NULL UNIQUE,
            sku VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            price FLOAT NOT NULL,
            category VARCHAR(128) NOT NULL,
            stock INTEGER NOT NULL,
            image_url VARCHAR(512),
            store_visible BOOLEAN NOT NULL,
            last_modified DATETIME,
            last_sync_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_store_products_category ON store_products (category)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_store_products_sku ON store_products (sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_store_products_stock ON store_products (stock)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_store_products_store_visible ON store_products (store_visible)")
    conn.commit()


def load_catalog() -> list[dict]:
    if not CATALOG_PATH.exists():
        logger.error("❌ %s não encontrado. Rode o sync primeiro (sync_prod_to_dev.py).", CATALOG_PATH)
        sys.exit(1)
    with open(CATALOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    logger.info("📦 %d produtos no catálogo", len(data))
    return data


def copy_photos(products: list[dict]) -> dict[str, str]:
    """Copia fotos do sync para data/images/. Retorna pic_filename → image_url."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image_urls: dict[str, str] = {}

    if not SYNC_PHOTOS.exists():
        logger.warning("⚠️  data/sync/photos/ospos-item-images/ não existe — nenhuma foto será copiada")
        return image_urls

    for prod in products:
        pic = (prod.get("pic_filename") or "").strip()
        if not pic or pic.lower() in ("null", "none"):
            continue
        # Sanitiza: só nome de arquivo, sem path
        filename = Path(pic).name
        src = SYNC_PHOTOS / filename
        if not src.exists():
            continue
        dest = IMAGES_DIR / filename
        if not dest.exists():
            shutil.copy2(src, dest)
        image_urls[filename] = f"/v1/store/images/{filename}"

    logger.info("🖼️  %d fotos disponíveis em data/images/", len(image_urls))
    return image_urls


def seed(products: list[dict], image_urls: dict[str, str]) -> None:
    conn = _db_conn()
    ensure_schema(conn)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    upserted = 0
    visible = 0
    with_image = 0

    for prod in products:
        item_id = int(prod["item_id"])
        pic = (prod.get("pic_filename") or "").strip()
        filename = Path(pic).name if pic and pic.lower() not in ("null", "none") else ""
        image_url = image_urls.get(filename)
        stock = max(0, int(float(prod.get("stock") or 0)))
        deleted = bool(prod.get("deleted"))

        has_image = bool(image_url)
        store_visible = 1 if (has_image and stock > 0 and not deleted) else 0

        conn.execute(
            """
            INSERT INTO store_products
                (ospos_id, sku, name, description, price, category,
                 stock, image_url, store_visible, last_sync_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ospos_id) DO UPDATE SET
                sku=excluded.sku,
                name=excluded.name,
                description=excluded.description,
                price=excluded.price,
                category=excluded.category,
                stock=excluded.stock,
                image_url=excluded.image_url,
                store_visible=excluded.store_visible,
                last_sync_at=excluded.last_sync_at,
                updated_at=excluded.updated_at
            """,
            (
                item_id,
                str(prod.get("sku") or item_id),
                (prod.get("name") or "").strip(),
                (prod.get("description") or "").strip(),
                float(prod.get("unit_price") or 0),
                (prod.get("category") or "Geral").strip(),
                stock,
                image_url,
                store_visible,
                now, now, now,
            ),
        )
        upserted += 1
        if has_image:
            with_image += 1
        if store_visible:
            visible += 1

    conn.commit()

    row = conn.execute("SELECT COUNT(*) AS total, SUM(store_visible) AS vis FROM store_products").fetchone()
    conn.close()

    logger.info("✅ Seed concluído!")
    logger.info("   🔁 %d upserts", upserted)
    logger.info("   🖼️  %d com imagem", with_image)
    logger.info("   👁️  %d visíveis na loja (total %d)", visible, row["total"] or 0)


def main() -> None:
    logger.info("🚀 Seed para Render iniciando...")
    products = load_catalog()
    image_urls = copy_photos(products)
    seed(products, image_urls)
    logger.info("🎉 Pronto!")


if __name__ == "__main__":
    main()
