# Contexto — Sync catálogo entre PCs via Git (GitHub)

Objetivo: expor o catálogo de produtos e fotos do PC A (192.168.15.6:8000) para outro PC (B) via git/GitHub, sem expor porta pública.

## 1. Ambiente-fonte (PC A)
- Inventory-service (FastAPI) em `http://192.168.15.6:8000`, unit `inventory.service` (systemctl, reinicia leve).
- Endpoint já existente:
  - `GET /v1/store/sync-total?limit=1000&offset=N&include_deleted=&since=YYYY-MM-DD HH:MM:SS`
    - Headers: `X-Total-Count` (ex.: 10113), `X-Limit`, `X-Offset`.
    - Retorna JSON array: `item_id, sku, name, category, description, cost_price, unit_price, stock, image_url, pic_filename, last_modified, deleted`.
  - `GET /v1/store/ospos-item-images/{pic_filename}` — serve foto full-res do write-back (PNG ~6,3MB).
  - `data/photo_uploads.jsonl` — feed de eventos de upload (tempo real).
  - WS `/v1/store/photo/ws` — broadcast de eventos de foto (tempo real).
- `gh` CLI logado nesse PC (GitHub).

## 2. Decisões para implementação (confirmadas)
- Repo destino do sync: `inventory-service`, branch nova `sync/catalog`.
- Script de sync roda no PC A, parcelado em batches paginados (limit=1000) pra não travar PC (3.7Gi RAM — nada de carregar tudo em memória).
- Imagens: **thumbnail WebP ~110x110 (~30KB cada)**, via Pillow (sem rembg → leve, sem onnxruntime). Full-res PNG só puxado via HTTP em LAN quando necessário.
- Outputs gerados: `catalog.json` (catálogo) + `photos/<item_id>.webp` (thumbnails).
- `.gitignore` atualizado: não versionar full-res PNG, node_modules, .env, *.log, etc.

## 3. Plano de implementação (para retomar amanhã)
- `/home/ismael/inventory-service/scripts/sync_total_to_git.sh`:
  1. `git fetch origin` + checkout `sync/catalog` (ou cria).
  2. Loop paginado: enquanto offset < X-Total-Count:
     - `curl .../sync-total?limit=1000&offset=$offset` → append em `catalog.json` (build incremental ou temp).
     - Para cada item com `pic_filename`: fetch `/ospos-item-images/<pic>` → resize/thumbnail WebP p/ `photos/<item_id>.webp` (via Python Pillow, 1 imagem por vez, sem rembg).
     - `git add` incremental por batch (ou a cada N itens) → commit → push? (push periodicamente pra não travar, ex.: a cada batch ou só no fim).
  3. `git add catalog.json photos/ && git commit -m "sync: $(date +%Y%m%d_%H%M)" && git push origin sync/catalog`.
- systemd: `sync-catalog.service` (oneshot) + `sync-catalog.timer` (30 min).
- PC B: `git pull` + consome `catalog.json`; opcional: WS `/v1/store/photo/ws` notifica PC B (hospedeira) quando item sync. (Decidir amanhã: timer vs WS notificação.)

## 4. Pendências para confirmar/implementar amanhã
- [ ] criar pasta `docs/` + este md (ok).
- [ ] script `scripts/sync_total_to_git.sh`.
- [ ] dependency Pillow? (venv tem? `pip show pillow`; se não, instalar — leve.)
- [ ] systemd unit+timer.
- [ ] primeira sync (executar + validar + push).
- [ ] decidir: PC B notifica via WS, timer pull, ou ambos?
- [ ] branch `sync/catalog` pode precisar de permissão no GitHub — validar `gh auth login` status na hora do push.

## 5. Observações de capacidade (PC A)
- RAM 3.7Gi — proibir sync full + rembg em lote simultaneamente; batches pequenos e thumbnail sem rembg.
- Evitar `sync?mode=full` no inventory-service (8492 produtos) sem confirmação — aqui o sync-total é só leitura HTTP (leve), sem DB-heavy.

> Criado: 2026-08-05 (sessão). Retomar amanhã.

## 6. Status — 16/ago/2026 (implementado)

- **Scripts:**
  - `scripts/sync_catalog.py` — pagina `sync-total` (limit=1000), escreve `catalog.json`
    (JSON compacto, inclui deletados via `--with-deleted`) e gera thumbs WebP ~110px
    em `photos/<item_id>.webp` lendo as fotos **locais** (`uploads/item_pics`) — só
    quando o thumb ainda não existe (delta). Sem download full-res.
  - `scripts/sync_total_to_git.sh` — wrapper: fetch, checkout `sync/catalog`,
    merge de `origin/main` (mantém código atualizado), roda o python, commit
    incremental + push. `flock` p/ não rodar 2x.
- **Timer (user):** `~/.config/systemd/user/sync-catalog.{service,timer}` — a cada 30min
  (`systemctl --user enable --now sync-catalog.timer`). Validação: push funciona no
  contexto systemd (keyring do `gh` acessível).
- **Branch:** `origin/sync/catalog` = código de `main` + `catalog.json` (10254 itens) +
  `photos/*.webp` (129). Atualizada a cada ciclo.
- **Primeira sync:** 2026-08-16, commit `861601c`; merge de main `767030d`.

### PC B — como consumir
```bash
git clone -b sync/catalog https://github.com/ismaeldouglasdev/inventory-service.git
# catálogo + thumbs disponíveis imediatamente (catalog.json, photos/<item_id>.webp)
# foto full-res quando precisar (LAN, PC A):
curl -o /tmp/foto.png "http://192.168.15.6:8000/v1/store/ospos-item-images/10113.png"
# atualizar (dados + código):
git pull origin sync/catalog
```
- Para desenvolver: usar `main` (ou `sync/catalog`) — o merge diário mantém o código sincronizado.
- Timer roda no PC A; PC B só dá `git pull`.
