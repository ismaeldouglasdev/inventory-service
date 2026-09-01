# Handoff: Push Sync Integration — PC da Loja → Render

## Contexto

O site da loja (https://loja-online-kmg8.onrender.com) roda no Render com SQLite próprio.
O PC da loja roda inventory-service local com OSPOS MySQL.
**Problema:** atualizações no PC da loja (estoque, fotos) não chegam no Render.

## O que foi feito (esta sessão)

1. **`app/config.py`** — novas settings:
   - `push_sync_url` — URL do Render para push
   - `push_sync_api_key` — chave de auth (fallback para `api_key`)

2. **`app/api/v1/store.py`** — 2 novos endpoints:
   - `POST /v1/store/sync/push` — recebe lista de produtos e upsert no SQLite
   - `POST /v1/store/sync/push-image` — recebe imagem e upload para R2
   - R2 upload automático no capture app (foto já vai pro R2 quando tirada)

3. **`app/services/store_sync.py`** — push trigger automático:
   - Após cada sync (full ou delta), se `PUSH_SYNC_URL` estiver configurado, empurra todos os produtos + imagens pro Render
   - Não falha o sync se o push der erro (log warning)

4. **Commits:** `1e99367` (feat) + `b2757cf` (data) — branch `sync/prod-data`

## O que falta fazer (amanhã no PC da loja)

### Passo 1: Pull das alterações

```bash
cd /home/ismael/inventory-service
git pull origin sync/prod-data
```

### Passo 2: Adicionar env vars no .env

```bash
# Editar /home/ismael/inventory-service/.env e adicionar:
PUSH_SYNC_URL=https://loja-online-kmg8.onrender.com
PUSH_SYNC_API_KEY=<a-chave-que-voce-quiser>
```

**Nota:** o `PUSH_SYNC_API_KEY` pode ser qualquer string segura. Ela será usada para autenticar o push entre as duas instâncias.

### Passo 3: Reiniciar o inventory-service

```bash
sudo systemctl restart inventory.service
```

### Passo 4: Testar o push manualmente

```bash
# Trigger sync completo (vai fazer push automático pro Render)
curl -X POST "http://localhost:8000/v1/store/sync?mode=full&min_stock=0"
```

### Passo 5: Verificar no Render

```bash
# Verificar se os produtos chegaram
curl -s "https://loja-online-kmg8.onrender.com/v1/store/products?per_page=5" | python3 -m json.tool

# Verificar se o produto "TÁBUA DE CORTES DE CARNE ARQPLAST" aparece
curl -s "https://loja-online-kmg8.onrender.com/v1/store/products?search=tábua" | python3 -m json.tool
```

## Fluxo automático (depois de configurado)

1. OSPOS sync roda a cada 5min (cron delta)
2. Após cada sync, `_push_to_remote()` é chamado automaticamente
3. Produtos são empacotados em batches de 100 e enviados via POST
4. Imagens locais são enviadas pro R2 automaticamente
5. Render recebe e upsert no SQLite local

## Deploy no Render

O push pro GitHub (`sync/prod-data`) já dispara auto-deploy no Render.
Verificar status: https://dashboard.render.com/srv-da2s5grm8hqs73e9hjo0

## Troubleshooting

- **Push falha com 401:** verificar se `PUSH_SYNC_API_KEY` no .env do PC da loja bate com o que o Render espera
- **Push não roda:** verificar logs com `journalctl -u inventory.service -f`
- **Produtos não aparecem:** verificar se `store_visible` está true (precisa ter estoque > 0 E imagem)
- **Imagens não carregam:** verificar se R2 está configurado no Render (env vars R2_*)
