#!/usr/bin/env python3
"""
Sync catálogo + fotos (thumbs WebP) do OSPOS para a branch sync/catalog.

- Página o catálogo via GET /v1/store/sync-total (leve, limit=1000).
- Escreve catalog.json (JSON array compacto).
- Gera thumbnails WebP ~110px em photos/<item_id>.webp a partir das fotos
  LOCAIS (uploads/item_pics) — só quando o thumb ainda não existe.
- NÃO baixa foto full-res: o PC B puxa da LAN quando precisar.

Uso: python sync_catalog.py [--with-deleted]
"""
import glob
import json
import os
import sys
import urllib.request
from pathlib import Path

from PIL import Image

BASE_URL = "http://localhost:8000/v1/store/sync-total"
ITEM_PICS = "/var/www/html/pos/public/uploads/item_pics"
LIMIT = 1000
THUMB = (110, 110)

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "catalog.json"
PHOTOS_DIR = ROOT / "photos"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
                if key:
                    headers["X-API-Key"] = key
                break
    return headers


def fetch_catalog(with_deleted: bool) -> list[dict]:
    items: list[dict] = []
    offset = 0
    total = 0
    while True:
        url = f"{BASE_URL}?limit={LIMIT}&offset={offset}"
        if with_deleted:
            url += "&include_deleted=true"
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=90) as resp:
            total = int(resp.headers.get("X-Total-Count", 0) or 0)
            batch = json.load(resp)
        items.extend(batch)
        offset += len(batch)
        print(f"  catálogo: {offset}/{total}", flush=True)
        if not batch or offset >= total:
            break
    return items


def locate_photo(pic_filename: str):
    if not pic_filename:
        return None
    exact = os.path.join(ITEM_PICS, pic_filename)
    if os.path.isfile(exact):
        return exact
    base = os.path.join(ITEM_PICS, os.path.splitext(pic_filename)[0])
    for match in glob.glob(base + ".*"):
        return match
    return None


def make_thumb(item_id: int, src: str) -> str:
    out = PHOTOS_DIR / f"{item_id}.webp"
    if out.exists():
        return "keep"
    try:
        img = Image.open(src)
        img.thumbnail(THUMB)
        img.save(out, "WEBP", quality=85)
        return "new"
    except Exception as exc:  # noqa: BLE001
        print(f"    [aviso] thumb {item_id}: {exc}", flush=True)
        return "error"


def main() -> int:
    with_deleted = "--with-deleted" in sys.argv
    print("Página do catálogo...", flush=True)
    items = fetch_catalog(with_deleted)
    print(f"Total itens: {len(items)}", flush=True)

    CATALOG_PATH.write_text(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    PHOTOS_DIR.mkdir(exist_ok=True)
    with_photo = [i for i in items if i.get("pic_filename")]
    print(f"Com foto: {len(with_photo)}", flush=True)
    new_thumbs = 0
    errors = 0
    for i, item in enumerate(with_photo, 1):
        src = locate_photo(item["pic_filename"])
        if src is None:
            print(f"    [aviso] foto não local {item['item_id']} ({item['pic_filename']})", flush=True)
            errors += 1
            continue
        result = make_thumb(item["item_id"], src)
        if result == "new":
            new_thumbs += 1
        elif result == "error":
            errors += 1
        if i % 25 == 0:
            print(f"  fotos: {i}/{len(with_photo)} (novas: {new_thumbs})", flush=True)

    print(
        f"OK: catalog.json ({len(items)} itens), thumbs novos: {new_thumbs}, erros: {errors}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
