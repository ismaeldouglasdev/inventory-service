#!/usr/bin/env python3
"""Importa produtos reais do OSPOS + fotos do NEX para a loja.

Fluxo:
  1. Lê ospos_export.csv (todos os produtos)
  2. Lê final_mapping.json (códigos NEX → descrição)
  3. Lê as imagens em data/images/nex_originals/
  4. Copia imagens para data/images/ nomeadas como product_{item_id}.jpg
  5. Popula store_products no SQLite
  6. Marca como visíveis os que têm foto

Uso:
    cd ~/inventory-service
    source .venv/bin/activate
    python3 scripts/import_nex_full.py
"""

import csv
import json
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = BASE_DIR / "ospos_export.csv"
MAPPING_PATH = BASE_DIR / "final_mapping.json"
NEX_ORIGINALS = BASE_DIR / "data" / "images" / "nex_originals"
IMAGES_DIR = BASE_DIR / "data" / "images"
DB_PATH = BASE_DIR / "data" / "inventory.db"


def load_ospos_products() -> list[dict]:
    """Carrega produtos do CSV exportado do OSPOS."""
    products = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            products.append({
                "item_id": int(row["item_id"]),
                "name": row["name"].strip(),
                "description": row.get("description", "").strip(),
                "price": float(row["unit_price"]),
                "category": row.get("category", "Geral").strip(),
                "sku": row.get("sku", str(row["item_id"])).strip(),
                "stock": max(0, int(float(row.get("stock", 0) or 0))),
            })
    logger.info("📦 %d produtos carregados do OSPOS", len(products))
    return products


def load_nex_mapping() -> dict[str, str]:
    """Carrega mapping: código NEX → descrição."""
    with open(MAPPING_PATH) as f:
        data = json.load(f)
    # Keys são strings como "182", "239" etc.
    logger.info("🗺️  %d códigos NEX carregados", len(data))
    return data


def get_nex_images() -> dict[int, Path]:
    """Mapeia imagens da pasta nex_originals para item_id do OSPOS.

    O nome do arquivo segue o padrão:
      "00182_BRINQUEDOS.jpg" → item_id = 182
      "01401_KIT FUNIL.jpg"  → item_id = 1401
    """
    images: dict[int, Path] = {}
    for f in NEX_ORIGINALS.iterdir():
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        # Extrai o código numérico do início do nome
        stem = f.stem  # "00182_BRINQUEDOS" ou "02130_prod_2130"
        # Pega o primeiro grupo de dígitos
        import re
        match = re.match(r"0*(\d+)", stem)
        if match:
            code = int(match.group(1))
            images.setdefault(code, f)
    
    logger.info("🖼️  %d imagens mapeadas para códigos NEX", len(images))
    return images


def copy_images_to_store(images: dict[int, Path]) -> dict[int, str]:
    """Copia imagens para data/images/ como product_{item_id}.jpg.
    
    Retorna dict: item_id → image_url
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image_map: dict[int, str] = {}

    for code, src_path in images.items():
        ext = src_path.suffix.lower()
        dest_name = f"product_{code}{ext}"
        dest_path = IMAGES_DIR / dest_name

        # Só copia se não existir
        if not dest_path.exists():
            shutil.copy2(src_path, dest_path)
            logger.debug("  Copiado: %s → %s", src_path.name, dest_name)
        else:
            logger.debug("  Já existe: %s", dest_name)

        image_map[code] = f"/v1/store/images/{dest_name}"

    logger.info("✅ %d imagens copiadas para %s", len(image_map), IMAGES_DIR)
    return image_map


def populate_store_products(
    products: list[dict],
    image_map: dict[int, str],
) -> None:
    """Popula store_products no SQLite com os produtos + imagens."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Backup do estado atual
    cursor.execute("SELECT COUNT(*) FROM store_products")
    current = cursor.fetchone()[0]
    logger.info("📊 store_products atual: %d produtos", current)

    # Apaga tudo e recria (ou faz upsert)
    cursor.execute("DELETE FROM store_products")
    logger.info("🗑️  store_products limpo")

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    inserted = 0
    with_images = 0
    for prod in products:
        item_id = prod["item_id"]
        has_image = item_id in image_map
        image_url = image_map.get(item_id)

        store_visible = 1 if has_image and prod["stock"] > 0 else 0

        cursor.execute("""
            INSERT INTO store_products 
                (ospos_id, sku, name, description, price, category, 
                 stock, image_url, store_visible, 
                 last_sync_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item_id,
            prod["sku"],
            prod["name"],
            prod["description"],
            prod["price"],
            prod["category"],
            prod["stock"],
            image_url,
            store_visible,
            now, now, now,
        ))
        inserted += 1
        if has_image:
            with_images += 1

    conn.commit()

    # Verificar resultado
    cursor.execute("SELECT COUNT(*) as total, SUM(store_visible) as visible FROM store_products")
    row = dict(cursor.fetchone())
    conn.close()

    logger.info("✅ Importação concluída!")
    logger.info("   📦 %d produtos inseridos", inserted)
    logger.info("   🖼️  %d produtos com imagem", with_images)
    logger.info("   👁️  %d produtos visíveis na loja", row["visible"])


def main():
    logger.info("🚀 Iniciando importação OSPOS + NEX...")

    if not CSV_PATH.exists():
        logger.error("❌ ospos_export.csv não encontrado em %s", CSV_PATH)
        sys.exit(1)

    products = load_ospos_products()
    nex_mapping = load_nex_mapping()
    images = get_nex_images()

    # Verificar quantos códigos NEX têm produto correspondente
    codes_in_ospos = {p["item_id"] for p in products}
    matched = sum(1 for code in images if code in codes_in_ospos)
    unmatched = sum(1 for code in images if code not in codes_in_ospos)
    logger.info("🔗 Mapping: %d códigos com produto OSPOS, %d sem correspondência", matched, unmatched)
    if unmatched > 0:
        unmatched_codes = [str(c) for c in images if c not in codes_in_ospos]
        logger.warning("   Códigos sem produto: %s", ", ".join(unmatched_codes[:10]))

    image_map = copy_images_to_store(images)
    populate_store_products(products, image_map)

    logger.info("🎉 Importação completa!")


if __name__ == "__main__":
    main()
