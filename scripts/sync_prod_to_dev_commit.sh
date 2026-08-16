#!/usr/bin/env bash
# Wrapper: roda o sync e commita no git branch sync/prod-data se mudou algo.
# Idempotente: se nada mudou, não faz commit.
set -euo pipefail

REPO="/home/ismaeldev/Desktop/code_study/MeusProjetos/inventory-service"
cd "$REPO"

# 1. Garante branch certa
BRANCH="sync/prod-data"
CURRENT=$(git branch --show-current)
if [ "$CURRENT" != "$BRANCH" ]; then
    echo "🔀 Trocando para $BRANCH"
    git checkout "$BRANCH"
fi

# 2. Roda o sync (baixa catálogo + metadados + fotos incrementais)
echo "⬇️  Rodando sync..."
.venv/bin/python scripts/sync_prod_to_dev.py

# 3. Se houver mudanças em data/sync/ ou no próprio script, commita
CHANGED=$(git status --porcelain | grep -E '^( M| M|\?\?|A  |AM |M  )' | wc -l)
if [ "$CHANGED" -gt 0 ]; then
    git add data/sync/ scripts/sync_prod_to_dev.py .gitignore 2>/dev/null || true
    # Filtra para ignorar mudanças em código não relacionado (app/*)
    git reset app/ 2>/dev/null || true
    MSG="sync: snapshot prod $(date +%Y-%m-%d_%H:%M)"
    git commit -m "$MSG" -- data/sync/ scripts/sync_prod_to_dev.py .gitignore
    echo "✅ Commit criado: $MSG"
    # 4. Push (não bloqueia se rede cair)
    if git push origin "$BRANCH" 2>/dev/null; then
        echo "🚀 Push OK"
    else
        echo "⚠️  Push falhou (rede?). Próxima execução retenta."
    fi
else
    echo "✓ Nada mudou — sem commit"
fi
