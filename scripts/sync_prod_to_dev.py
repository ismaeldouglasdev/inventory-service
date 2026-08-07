#!/usr/bin/env python3
"""Sync contínuo PROD → DEV usando o repositório git como canal.

Puxa do PC da loja (prod) via API e materializa em data/sync/:
  1. Catálogo completo (sync-total paginado, limit máx 5000)  → data/sync/catalog.json
  2. Metadados (stats, categories, photos/recent)             → data/sync/metadata/*.json
  3. Fotos (ospos-item-images + images) incremental por hash  → data/sync/photos/

Uso:
  python scripts/sync_prod_to_dev.py [--prod URL] [--force]

Variáveis de ambiente:
  PROD_URL  (default: http://192.168.15.6:8000)
  SYNC_DIR  (default: <repo>/data/sync)

O commit/push é feito pelo wrapper (sync_prod_to_dev.sh) ou manualmente —
este script apenas materializa os dados e reporta o que mudou.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

PROD_URL_DEFAULT = os.environ.get("PROD_URL", "http://192.168.15.6:8000")
BASE = Path(__file__).resolve().parent.parent
SYNC_DIR_DEFAULT = Path(os.environ.get("SYNC_DIR", str(BASE / "data" / "sync")))

CATALOG_FILE = "catalog.json"
MANIFEST_FILE = "manifest.json"
METADATA_DIR = "metadata"
PHOTOS_DIR = "photos"
PHOTO_INDEX_FILE = "photo_index.json"  # filename → sha256 (do prod)

PAGE_SIZE = 5000  # limit máximo aceito pelo sync-total


@dataclass
class SyncResult:
    catalog_count: int = 0
    catalog_changed: bool = False
    metadata: dict = field(default_factory=dict)
    photos_downloaded: int = 0
    photos_skipped: int = 0
    photos_failed: list = field(default_factory=list)
    photo_index: dict = field(default_factory=dict)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_catalog(session: requests.Session, prod_url: str) -> list[dict]:
    """Baixa o catálogo completo paginando (limit máx 5000, offset)."""
    all_items: list[dict] = []
    offset = 0
    while True:
        resp = session.get(
            f"{prod_url}/v1/store/sync-total",
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        all_items.extend(page)
        total = int(resp.headers.get("x-total-count", str(len(all_items))))
        offset += len(page)
        if offset >= total or not page:
            break
    return all_items


def fetch_metadata(session: requests.Session, prod_url: str) -> dict:
    """Puxa stats, categorias e fotos recentes."""
    out: dict = {}
    endpoints = {
        "stats": "/v1/admin/stats",
        "categories": "/v1/store/categories",
        "photos_recent": "/v1/store/photos/recent",
    }
    for name, path in endpoints.items():
        try:
            resp = session.get(f"{prod_url}{path}", timeout=30)
            if resp.status_code == 200:
                out[name] = resp.json()
            else:
                out[name] = {"error": f"HTTP {resp.status_code}"}
        except requests.RequestException as e:
            out[name] = {"error": str(e)}
    return out


def build_photo_index(catalog: list[dict]) -> dict[str, str]:
    """Extrai do catálogo os arquivos de imagem que o prod referencia.

    image_url vem como /v1/store/ospos-item-images/{filename} ou
    /v1/store/images/{filename}. Normaliza para filename + rota.
    """
    index: dict[str, str] = {}  # filename → rota (ospos-item-images|images)
    for p in catalog:
        url = p.get("image_url") or ""
        if not url:
            continue
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2:
            route = parts[-2]
            filename = parts[-1]
            if route in ("ospos-item-images", "images"):
                index[filename] = route
    return index


def download_photo(
    session: requests.Session,
    prod_url: str,
    route: str,
    filename: str,
    dest_dir: Path,
) -> tuple[str, int | None]:
    """Baixa uma foto. Retorna (sha256, size_bytes) ou (None, None) em erro."""
    url = f"{prod_url}/v1/store/{route}/{filename}"
    try:
        resp = session.get(url, timeout=60)
        if resp.status_code != 200:
            return None, None
        data = resp.content
        if not data:
            return None, None
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_bytes(data)
        return sha256_bytes(data), len(data)
    except requests.RequestException:
        return None, None


def load_prev_manifest(sync_dir: Path) -> dict:
    mf = sync_dir / MANIFEST_FILE
    if mf.exists():
        try:
            return json.loads(mf.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_manifest(sync_dir: Path, result: SyncResult, catalog_sha: str) -> None:
    manifest = {
        "schema_version": 1,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prod_url": PROD_URL_DEFAULT,
        "catalog": {
            "count": result.catalog_count,
            "sha256": catalog_sha,
            "changed": result.catalog_changed,
        },
        "metadata": result.metadata,
        "photos": {
            "downloaded": result.photos_downloaded,
            "skipped": result.photos_skipped,
            "failed": result.photos_failed,
            "index_count": len(result.photo_index),
        },
        "photo_index": result.photo_index,
    }
    (sync_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", default=PROD_URL_DEFAULT, help="URL do PC da loja (prod)")
    parser.add_argument("--force", action="store_true", help="Rebaixa fotos mesmo se já existirem")
    args = parser.parse_args()

    prod_url = args.prod.rstrip("/")
    sync_dir = SYNC_DIR_DEFAULT
    sync_dir.mkdir(parents=True, exist_ok=True)
    (sync_dir / METADATA_DIR).mkdir(exist_ok=True)
    photos_dir = sync_dir / PHOTOS_DIR
    photos_dir.mkdir(exist_ok=True)

    session = requests.Session()
    result = SyncResult()

    print(f"🔌 Prod: {prod_url}")
    print(f"📁 Sync dir: {sync_dir}")

    # ── 1. Catálogo ────────────────────────────────────────────────
    print("⬇️  Baixando catálogo...")
    try:
        catalog = fetch_catalog(session, prod_url)
    except requests.RequestException as e:
        print(f"❌ Falha ao baixar catálogo: {e}")
        return 1

    result.catalog_count = len(catalog)
    catalog_bytes = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    catalog_sha = sha256_bytes(catalog_bytes)

    prev = load_prev_manifest(sync_dir)
    prev_catalog_sha = prev.get("catalog", {}).get("sha256")
    result.catalog_changed = prev_catalog_sha != catalog_sha

    (sync_dir / CATALOG_FILE).write_text(
        json.dumps(catalog, indent=1, ensure_ascii=False)
    )
    print(f"   ✅ {result.catalog_count} produtos (sha256 {catalog_sha[:12]}…)"
          f" {'MUDOU' if result.catalog_changed else 'sem mudanças'}")

    # ── 2. Metadados ────────────────────────────────────────────────
    print("⬇️  Metadados (stats, categories, photos/recent)...")
    result.metadata = fetch_metadata(session, prod_url)
    for name, data in result.metadata.items():
        (sync_dir / METADATA_DIR / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
        status = "✅" if "error" not in data else f"⚠️  {data.get('error')}"
        print(f"   {status} {name}")

    # ── 3. Fotos incrementais ───────────────────────────────────────
    print("⬇️  Fotos (incremental por hash)...")
    result.photo_index = build_photo_index(catalog)
    print(f"   📸 {len(result.photo_index)} fotos referenciadas no catálogo")

    # Estado anterior por hash: filename → sha256 que JÁ baixamos
    prev_photo_hashes: dict[str, str] = {}
    if not args.force:
        prev_photos = prev.get("photos", {})
        prev_idx = prev.get("photo_index", {})
        # Hashes que já temos localmente (do manifest anterior)
        for filename, route in prev_idx.items():
            local = photos_dir / route / filename
            if local.exists():
                prev_photo_hashes[f"{route}/{filename}"] = sha256_bytes(local.read_bytes())

    for filename, route in sorted(result.photo_index.items()):
        rel = f"{route}/{filename}"
        local_file = photos_dir / route / filename

        # Já temos e hash confere? → pula
        if local_file.exists() and prev_photo_hashes.get(rel) is not None and not args.force:
            result.photos_skipped += 1
            continue

        photo_sha, size = download_photo(session, prod_url, route, filename, photos_dir / route)
        if photo_sha is None:
            result.photos_failed.append(rel)
            print(f"   ❌ {filename}")
            continue
        result.photos_downloaded += 1
        print(f"   ✅ {filename} ({size} bytes)")

    if result.photos_failed:
        print(f"   ⚠️  {len(result.photos_failed)} falharam: {result.photos_failed[:5]}…")

    # ── Manifest ────────────────────────────────────────────────────
    save_manifest(sync_dir, result, catalog_sha)
    print("📄 Manifest salvo")

    print()
    print("📊 Resumo:")
    print(f"   Catálogo:   {result.catalog_count} produtos"
          f" ({'mudou' if result.catalog_changed else 'sem mudança'})")
    print(f"   Metadados:  {', '.join(result.metadata.keys())}")
    print(f"   Fotos:      {result.photos_downloaded} baixadas, "
          f"{result.photos_skipped} puladas (hash ok), {len(result.photos_failed)} falhas")
    print()
    print("👉 Próximo passo: git add data/sync && git commit && git push (branch sync/prod-data)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
