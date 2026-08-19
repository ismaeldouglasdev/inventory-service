#!/bin/bash
# Sync catálogo + fotos (thumbs WebP) para a branch sync/catalog do GitHub.
# Roda em PC A. Lê o catálogo via HTTP local e as fotos do disco local.
# Leve: páginas de 1000, um thumb por vez. flock p/ não rodar 2x.
set -euo pipefail

REPO=/home/ismael/inventory-service
VENV_PY="$REPO/.venv/bin/python"
LOCK=/tmp/sync-catalog.lock

exec 9>"$LOCK"
flock -n 9 || { echo "sync já rodando; saindo"; exit 0; }

cd "$REPO"
export GIT_TERMINAL_PROMPT=0

echo "[1/5] fetch + branch sync/catalog"
git fetch origin --quiet || true
git checkout -q sync/catalog 2>/dev/null || git checkout -q -b sync/catalog origin/main
git merge -q --ff-only origin/sync/catalog 2>/dev/null || true
if ! git merge -q --no-edit origin/main 2>/dev/null; then
  git merge --abort
  echo "merge de main falhou — abortando" >&2
  exit 1
fi

echo "[2/5] gerando catalog.json + thumbs"
"$VENV_PY" "$REPO/scripts/sync_catalog.py" --with-deleted

echo "[3/5] git add"
git add catalog.json photos/

echo "[4/5] commit (se houver mudança)"
if ! git diff --cached --quiet; then
  git commit -q -m "sync: catálogo + fotos ($(date +%Y%m%d_%H%M))"
fi

echo "[5/5] push"
if git rev-parse --verify -q origin/sync/catalog >/dev/null 2>&1; then
  if git rev-list --count origin/sync/catalog..HEAD 2>/dev/null | grep -q '[1-9]'; then
    git push -q origin sync/catalog
    echo "sync completo: $(git rev-parse --short HEAD)"
  else
    echo "nada novo para push"
  fi
else
  git push -q origin sync/catalog
  echo "primeira sync: $(git rev-parse --short HEAD)"
fi
