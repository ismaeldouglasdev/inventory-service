#!/usr/bin/env bash
# Sync automático: catálogo + fotos → data/sync/ → git push → Render re-deploy.
# Roda via cron (a cada 30min). Leve: ~5s, flock evita concorrência.
# A API local (localhost:8000) precisa estar rodando.
set -euo pipefail

REPO=/home/ismael/inventory-service
VENV_PY="$REPO/.venv/bin/python"
LOCK=/tmp/sync-render-auto.lock
ITEM_PICS="/var/www/html/pos/public/uploads/item_pics"
TARGET_BRANCH="sync/prod-data"

exec 9>"$LOCK"
flock -n 9 || { echo "sync já rodando; saindo"; exit 0; }

cd "$REPO"
export GIT_TERMINAL_PROMPT=0

# Salva branch atual e troca pra sync/prod-data
ORIG_BRANCH=$(git branch --show-current)
if [ "$ORIG_BRANCH" != "$TARGET_BRANCH" ]; then
    git checkout -q "$TARGET_BRANCH" 2>/dev/null || git checkout -q -b "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
fi

# Pull remoto antes de tudo (evita conflito)
git pull -q --ff-only origin "$TARGET_BRANCH" 2>/dev/null || true

# 1. Gera catalog.json (lê de /v1/store/sync-total)
echo "[1/4] gerando catalog.json..."
"$VENV_PY" "$REPO/scripts/sync_catalog.py" --with-deleted

# 2. Copia catalog.json + fotos para data/sync/ (onde o Dockerfile lê)
echo "[2/4] copiando para data/sync/..."
cp "$REPO/catalog.json" "$REPO/data/sync/catalog.json"

# Copia fotos PNG originais (não as WebP) para data/sync/photos/
DEST_PHOTOS="$REPO/data/sync/photos/ospos-item-images"
mkdir -p "$DEST_PHOTOS"
COPIED=0
for webp in "$REPO"/photos/*.webp; do
    [ -f "$webp" ] || continue
    base=$(basename "$webp" .webp)
    for ext in png jpg jpeg webp gif; do
        src="$ITEM_PICS/${base}.${ext}"
        if [ -f "$src" ]; then
            dest="$DEST_PHOTOS/${base}.${ext}"
            if [ ! -f "$dest" ] || [ "$src" -nt "$dest" ]; then
                cp "$src" "$dest"
                COPIED=$((COPIED + 1))
            fi
            break
        fi
    done
done
echo "   fotos copiadas: $COPIED"

# 3. Commit (se houver mudança)
echo "[3/4] commit..."
git add catalog.json data/sync/ photos/
if ! git diff --cached --quiet; then
    MSG="sync: catalog $(date +%Y%m%d_%H%M)"
    git commit -q -m "$MSG"
    echo "   commit: $MSG"
else
    echo "   nada mudou — sem commit"
    # Volta pra branch original antes de sair
    [ "$ORIG_BRANCH" != "$TARGET_BRANCH" ] && git checkout -q "$ORIG_BRANCH" 2>/dev/null || true
    exit 0
fi

# 4. Push (Render re-deploya automaticamente)
echo "[4/4] push..."
if git push -q origin "$TARGET_BRANCH" 2>/dev/null; then
    echo "🚀 Push OK — Render vai re-deployar"
else
    echo "⚠️  Push falhou — retry na próxima"
fi

# Volta pra branch original
[ "$ORIG_BRANCH" != "$TARGET_BRANCH" ] && git checkout -q "$ORIG_BRANCH" 2>/dev/null || true
