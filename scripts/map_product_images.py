#!/usr/bin/env python3
"""Mapeia imagens para store_products e ativa vitrine.

Fontes de imagem:
  1. data/images/product_{id}.jpg  → store_product.id = {id}
  2. data/images/nex_originals/{ospos_id}_{name}.jpg → ospos_id match
  3. data/images/desktop_photos/ → sem mapeamento direto, ignorado

Para cada match, atualiza image_url e store_visible=True (se stock>0).
"""
import os
import re
import sqlite3
import shutil
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "data" / "inventory.db"
IMAGES_DIR = BASE / "data" / "images"
NEX_DIR = IMAGES_DIR / "nex_originals"
DESKTOP_DIR = IMAGES_DIR / "desktop_photos"

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ── 1. product_{id}.jpg ──────────────────────────────────────
product_imgs = {}
for f in os.listdir(str(IMAGES_DIR)):
    m = re.match(r"product_(\d+)\.(jpg|jpeg|png|webp|gif)", f, re.I)
    if m:
        product_imgs[int(m.group(1))] = f

print(f"📸 product_*.jpg encontrados: {len(product_imgs)}")

# Buscar produtos que existem no DB
ids = list(product_imgs.keys())
placeholders = ",".join("?" for _ in ids)
cur.execute(
    f"SELECT id, ospos_id, sku, name, stock, image_url, store_visible "
    f"FROM store_products WHERE id IN ({placeholders})",
    ids,
)
existing = {row["id"]: row for row in cur.fetchall()}
print(f"📦 Match no DB: {len(existing)}/{len(product_imgs)}")

updated_product = 0
for pid, fname in product_imgs.items():
    if pid not in existing:
        continue
    row = existing[pid]
    # Ja tem imagem?
    if row["image_url"] and row["image_url"] != "":
        continue
    image_url = f"/v1/store/images/{fname}"
    stock = row["stock"] or 0
    visible = 1 if stock > 0 else 0
    cur.execute(
        "UPDATE store_products SET image_url=?, store_visible=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (image_url, visible, pid),
    )
    updated_product += 1

conn.commit()
print(f"✅ product_*.jpg: {updated_product} produtos atualizados")

# ── 2. nex_originals/{ospos_id}_{name}.jpg ────────────────────
nex_codes = {}  # ospos_id → [filenames]
for f in os.listdir(str(NEX_DIR)):
    m = re.match(r"(\d+)_(.+)", f)
    if m:
        code = int(m.group(1))
        nex_codes.setdefault(code, []).append(f)

print(f"\n📸 nex_originals códigos únicos: {len(nex_codes)}")

# Buscar produtos por ospos_id
codes = list(nex_codes.keys())
placeholders = ",".join("?" for _ in codes)
cur.execute(
    f"SELECT id, ospos_id, sku, name, stock, image_url, store_visible "
    f"FROM store_products WHERE ospos_id IN ({placeholders})",
    codes,
)
nex_rows = {row["ospos_id"]: row for row in cur.fetchall()}
print(f"📦 Match no DB (por ospos_id): {len(nex_rows)}/{len(nex_codes)}")

updated_nex = 0
for code, fnames in nex_codes.items():
    if code not in nex_rows:
        continue
    row = nex_rows[code]
    # Pula se já tem imagem do product_*
    if row["image_url"] and row["image_url"] != "":
        continue
    pid = row["id"]
    # Pega primeira imagem disponível
    src = NEX_DIR / fnames[0]
    # Renomeia pra product_{id}.jpg e copia pra raiz
    dst = IMAGES_DIR / f"product_{pid}.jpg"
    shutil.copy2(str(src), str(dst))
    image_url = f"/v1/store/images/product_{pid}.jpg"
    stock = row["stock"] or 0
    visible = 1 if stock > 0 else 0
    cur.execute(
        "UPDATE store_products SET image_url=?, store_visible=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (image_url, visible, pid),
    )
    updated_nex += 1

conn.commit()
print(f"✅ nex_originals: {updated_nex} produtos atualizados")

# ── 3. Sumário final ─────────────────────────────────────────
cur.execute(
    "SELECT COUNT(*) as total, SUM(store_visible) as visible, "
    "SUM(CASE WHEN image_url IS NOT NULL AND image_url != '' THEN 1 ELSE 0 END) as has_image "
    "FROM store_products"
)
row = cur.fetchone()
print(f"\n📊 TOTAL: {row['total']} produtos | Visíveis: {row['visible']} | Com imagem: {row['has_image']}")

# 10 exemplos
cur.execute(
    "SELECT id, sku, name, image_url, store_visible FROM store_products "
    "WHERE store_visible=1 LIMIT 10"
)
print("\n🔍 Amostra produtos visíveis:")
for r in cur.fetchall():
    print(f"  #{r['id']} {r['name']:30s} | img={r['image_url'][:40] if r['image_url'] else 'NONE'}")

conn.close()

# ── 4. desktop_photos ────────────────────────────────────────
# Sem mapeamento direto. Copia pra raiz com hash pra consulta manual.
desktop_count = len(os.listdir(str(DESKTOP_DIR)))
print(f"\n📸 desktop_photos: {desktop_count} imagens — sem mapeamento direto (fotos avulsas)")
