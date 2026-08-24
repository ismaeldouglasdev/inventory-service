# Elshaday Utilidades — Handoff Completo

**Última atualização:** 2026-08-19
**Status:** MVP funcional em produção

---

## 1. Visão Geral

Loja online "Elshaday Utilidades" conectada ao OSPOS (PDV). Dois repositórios:

| Repo | Stack | Deploy | URL |
|------|-------|--------|-----|
| **inventory-service** | Python/FastAPI + SQLite | Render (Docker, free) | https://loja-online-82t7.onrender.com |
| **loja-online** | React + TypeScript + Vite + Tailwind | Vercel | https://lojaonline-murex.vercel.app |

---

## 2. Arquitetura

```
loja-online (React SPA)
  → fetch → inventory-service (FastAPI)
              → SQLite (inventory.db)
              → data/images/ (produtos)
              → data/sync/ (catalog.json, photos/)
```

### Rotas da API

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/v1/store/products` | Produtos visíveis (loja) |
| GET | `/v1/store/products/{id}` | Detalhe produto |
| GET | `/v1/store/products/{id}/related` | Produtos relacionados |
| GET | `/v1/store/images/{filename}` | Imagens |
| GET | `/v1/admin/products` | Admin: lista com filtros |
| POST | `/v1/admin/products/{id}` | Admin: editar produto |
| POST | `/v1/admin/products/{id}/visibility` | Admin: toggle visibilidade |
| POST | `/v1/admin/products/{id}/rotate-image` | Admin: rotacionar imagem |
| POST | `/v1/admin/products/{id}/crop-image` | Admin: recortar imagem |
| POST | `/v1/admin/products/{id}/inpaint` | Admin: inpainting (AI) |
| POST | `/v1/admin/products/{id}/restore-image` | Admin: restaurar imagem original |
| POST | `/v1/admin/auth/login` | Login admin |

### Parâmetros de filtro (admin)

`store_visible` → `"true"/"1"/"yes"` ou `"false"/"0"/"no"`
`has_image` → mesma lógica
`q` → busca por nome/SKU
`page`, `per_page`, `sort`, `order`, `category`

---

## 3. Variáveis de Ambiente

### Render (inventory-service)

| Var | Valor | Descrição |
|-----|-------|-----------|
| `ADMIN_PASSWORD` | `admin123` | Senha do painel admin |
| `CORS_ORIGINS` | `["*"]` | CORS (restringir em produção) |
| `LOG_LEVEL` | `info` | Log level |
| `CDC_ENABLED` | `false` | Change data capture |
| `INPAINT_URL` | `http://localhost:20131/v1/images/generations` | 9router inpainting (não funciona no Render — precisa proxy local) |
| `INPAINT_KEY` | _(vazio)_ | API key do 9router |

### PC local

| Var | Descrição |
|-----|-----------|
| `RENDER_API_KEY` | `rnd_k9dykRn0ZnQpoluSEf1lAUsBXL9S` (salvo no .bashrc) |

---

## 4. Sync entre PCs (dev ↔ loja)

### PC da loja → pull
```bash
cd ~/Desktop/code_study/MeusProjetos/inventory-service
git pull origin sync/prod-data
```

### PC da loja → push (alterações no catálogo)
```bash
# Após editar catalog.json ou adicionar fotos em data/sync/photos/
cd ~/Desktop/code_study/MeusProjetos/inventory-service
git add data/sync/
git commit -m "sync: update catalog"
git push origin sync/prod-data
# Render faz auto-deploy (2-3 min)
```

### Ver detalhes completos
→ Ver `STORE-PC-HANDOFF.md` neste mesmo diretório

---

## 5. Funcionalidades Implementadas

### Frontend (loja-online)

- **Catálogo público** — grid responsivo, busca, categorias, paginação
- **Produto detalhe** — galeria, preço, WhatsApp CTA, produtos relacionados
- **Página inicial** — hero, categorias, produtos em destaque

### Admin (acesso: /admin → senha `admin123`)

- **Login simples** — senha via `ADMIN_PASSWORD`
- **Dashboard** — grid/lista de todos os produtos, paginação server-side
- **Filtros** — busca, visibilidade, presença de imagem, ordenação (persistem via URL)
- **Toggle visibilidade** — botão visual (emerald-500, com tooltip)
- **Editor de imagem** — rotacionar 90°, recortar (CSS overlay), restaurar original
- **Inpainting** — preencher fundo com AI via Cloudflare SD v1.5 (9router)
- **Upload** — drag & drop ou click
- **Cache-busting** — thumbnails atualizam após qualquer operação de imagem
- **Navegação** — ProductEdit mantém filtros ao voltar pro dashboard

### Backend (inventory-service)

- **FastAPI** — async, Pydantic, auto-docs em `/docs`
- **SQLite** — `inventory.db`, tabela `store_products`
- **Sync OSPOS** — `catalog.json` + `data/sync/photos/`
- **Seed Render** — `scripts/seed_render.py` importa catálogo no primeiro boot
- **Alembic** — migrations do banco
- **Imagens** — `data/images/`, servidas via `/v1/store/images/`
- **Crop** — recebe `x`, `y`, `w`, `h` em pixels reais, aplica com Pillow
- **Rotate** — Pillow `Image.rotate(90, expand=True)`
- **Inpainting** — Cloudflare SD v1.5 via 9router (usa `INPAINT_URL` e `INPAINT_KEY`)
- **Static files** — SPA fallback via `StaticFiles`

---

## 6. Deploy

### Render (backend)
- **Serviço:** `loja-online` (ID: `srv-da2s5grm8hqs73e9hjo0`)
- **Dashboard:** https://dashboard.render.com/web/srv-da2s5grm8hqs73e9hjo0
- **Branch:** `sync/prod-data`
- **Auto-deploy:** sim (a cada push no GitHub)
- **Plano:** Free (512MB RAM, spins down after inactivity)
- **Health check:** `GET /v1/health`

### Vercel (frontend)
- **Projeto:** `lojaonline-murex`
- **URL:** https://lojaonline-murex.vercel.app
- **Variáveis de env:** `VITE_API_URL=https://loja-online-82t7.onrender.com`

---

## 7. Endpoints Importantes

| URL | Descrição |
|-----|-----------|
| `/` | Loja pública (React SPA) |
| `/admin/login` | Login do admin |
| `/admin` | Dashboard do admin |
| `/admin/product/:id` | Editar produto |
| `/docs` | Swagger da API |
| `/redoc` | ReDoc da API |
| `/v1/health` | Health check |

---

## 8. Bugs Conhecidos / Limitações

1. **Inpainting não funciona no Render** — `INPAINT_URL` aponta pra `localhost:20131` (só existe no PC local). Solução: criar proxy ou usar API cloud direta.
2. **Render free tier** — service spin down após 15min de inactivity. Primeiro request leva ~30s.
3. **CORS** — `"*"` (liberado pra qualquer origem). Restringir em produção.
4. **Auth simples** — senha em texto plano. Considerar JWT em produção.
5. **SQLite** — sem concorrência. OK para loja pequena.
6. **Imagens** — servidas direto do filesystem. Em produção, usar S3/R2.

---

## 9. Estrutura de Arquivos

```
inventory-service/
├── app/
│   ├── api/v1/
│   │   ├── admin.py          # 11 rotas admin
│   │   ├── store.py          # rotas públicas (produtos, imagens)
│   │   └── observability.py  # dashboard de observabilidade
│   ├── core/
│   │   ├── database.py       # SQLite session
│   │   └── config.py         # Settings (pydantic-settings)
│   ├── models/
│   │   └── product.py        # SQLAlchemy + Pydantic models
│   ├── crud/
│   │   └── product.py        # CRUD operations
│   ├── seed.py               # Import catálogo
│   └── main.py               # FastAPI app + mounts
├── data/
│   ├── images/               # Imagens dos produtos
│   ├── sync/
│   │   ├── catalog.json      # Catálogo do OSPOS
│   │   └── photos/           # Fotos do OSPOS
│   └── inventory.db          # SQLite database
├── scripts/
│   └── seed_render.py        # Seed para Render
├── static/                   # Build do React (copiado do loja-online)
│   ├── index.html
│   └── assets/
├── alembic/                  # Migrations
├── Dockerfile                # Multi-stage Docker build
├── render.yaml               # Render blueprint
├── STORE-PC-HANDOFF.md       # Instruções pro PC da loja
└── PROJECT-HANDOFF.md        # Este arquivo

loja-online/
├── src/
│   ├── pages/
│   │   ├── Home.tsx          # Página inicial
│   │   ├── Catalog.tsx       # Catálogo completo
│   │   ├── ProductDetail.tsx # Detalhe do produto
│   │   └── admin/
│   │       ├── AdminLogin.tsx
│   │       ├── AdminDashboard.tsx
│   │       ├── ProductEdit.tsx
│   │       └── ImageEditor.tsx
│   ├── lib/
│   │   ├── api.ts            # API pública
│   │   ├── adminApi.ts       # API admin + bustImgCache()
│   │   └── types.ts          # TypeScript types
│   └── App.tsx               # Router
├── dist/                     # Build output
├── vercel.json               # Vercel config
└── package.json
```

---

## 10. Próximos Passos (sugeridos)

1. **CORS restrito** — trocar `["*"]` por domínios específicos
2. **Auth JWT** — substituir senha simples por tokens
3. **Inpainting cloud** — usar Cloudflare Workers API ou outro provider
4. **Upload para S3/R2** — imagens em storage remoto
5. **Sync bidirecional** — OSPOS ↔ inventory-service via API
6. **Categorias** — CRUD de categorias no admin
7. **SEO** — meta tags, sitemap, Open Graph
8. **Analytics** — tracking de visualizações

---

## 11. Credenciais

| Item | Valor |
|------|-------|
| Senha admin | `admin123` |
| Render API Key | `rnd_k9dykRn0ZnQpoluSEf1lAUsBXL9S` |
| GitHub repo | `ismaeldouglasdev/inventory-service` (público) |
| 9router Cloudflare | `REDACTED_KEY_REMOVED` (em INPAINT_KEY) |
| Render service ID | `srv-da2s5grm8hqs73e9hjo0` |

---

## 12. Pendências ativas (21/ago/2026 — p/ agente do outro PC)

> **✅ RESOLVIDAS em 23/ago/2026:** P1 (novo Account API Token Object Read & Write scope `loja-images` criado; env vars atualizadas via API por-chave `PUT /env-vars/{KEY}`; deploy LIVE validado) e P2 (auto-deploy confirmado funcionando). Credenciais persistidas localmente em `~/.cloudflare-r2.env`. Executor reutilizável: `scripts/activate_r2_credentials.py`. O token antigo (`calm-art-8536`, sem bucket escopado) pode ser deletado no dashboard.

### P1 — R2 write AccessDenied no Render (bloqueia fotos novas)
- Sintoma: `POST /v1/store/sync/push-image` → 500 `R2 upload failed: AccessDenied (PutObject)`.
- Causa: credenciais R2 do Render são só-leitura (ou expiraram). Leitura OK (imagens servem normal).
- Fix: dashboard Cloudflare → R2 API Token com **Object Read & Write** → atualizar `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` nas env vars do serviço `srv-da2s5grm8hqs73e9hjo0`.
- As 130 fotos atuais já estão no R2 (chaves SEM prefixo `images/`) — nada a migrar.
- Após o fix: capturar uma foto nova no celular e validar `push-image` ponta-a-ponta.

### P2 — Auto-deploy GitHub→Render morto
- Repo `ismaeldouglasdev/inventory-service` tem ZERO webhooks; último auto-deploy foi 20/ago 23:35.
- Fix: dashboard Render → Service Settings → Build & Deploy → reconectar GitHub.
- Enquanto isso, deploy manual: `POST https://api.render.com/v1/services/srv-da2s5grm8hqs73e9hjo0/deploys` (Bearer key na seção 11), body `{"clearCache": "do_not_clear"}`.

### P3 — Push sync já operacional (contexto)
- Delta a cada 5min detecta mudanças de estoque/preço/nome/foto em itens existentes (diff contra MySQL, não `last_modified`) e empurra SÓ o delta pro Render. Imagens puladas via HEAD no remote; >4MB downscaled (Pillow). Ver `app/services/store_sync.py` (commits `fc7e167`, `1b31e4e`).

### P4 — OSPOS fork: feature addAjax em WIP
- Commitado em `ade6672f6` (branch `merge-staging`): AJAX add-to-cart (`POST sales/addAjax`), itemSearch estruturado, mudanças em register.php + modern.css (~689 linhas).
- **NÃO está em prod.** Validar, testar no ambiente de teste e só então deployar.

### P5 — Menores
- APK do app Android (fix #32 AGENTS.md): reinstalar no celular (re-parear wireless debugging, porta 44507).
- Consolidar os 3 backups sobrepostos (AGENTS.md #34): `pos-backups/backup.sh` (hora), `backup_ospos.sh` (15min), `backup.sh` (3am).
- Remover `public/js/checkout.js` do fork (código morto com `<?= ?>` — AGENTS.md #11).

---

## 13. Progresso 23/ago/2026 (dev PC)

- ✅ P2 auto-deploy: CONFIRMADO funcionando (deploy LIVE 22/ago veio de push automático)
- ✅ loja-online master: fixes same-origin commitados (`c409ee9`) e pushados
- ✅ render.yaml: marcadores de conflito de merge removidos (`2ff8a08`)
- ✅ Reconciliação: `main` = `sync/prod-data` = `6b0a604` (AGENTS.md unificado c/ versão rica)
- 🔴 P1 R2 REDEFINIDO → **RESOLVIDO em 23-24/ago**: token totalmente morto foi substituído; credenciais novas em `~/.cloudflare-r2.env`; push-image→R2 validado ponta-a-ponta via API (produto 1, PNG teste gravado e servido 200). Orfão: chave `1.png` (73 bytes) no R2 — limpar quando quiser.
- ⚠️ Vercel (lojaonline-murex): **DEPLOYMENT_NOT_FOUND** (projeto sem deploy ativo). Fix local do vercel.json commitado (`73a7ea4`). Decisão pendente: arquivar OU `vercel login` + redeploy.

---

## 14. Progresso 24/ago/2026 (dev PC) — Segurança + Features

**Deploy LIVE:** commit `13fb0f1` (sync/prod-data). Todos os checks de produção passaram.

### Feito
1. **🔒 JWT admin (buraco crítico fechado)**: `/v1/admin/*` estava ABERTO (verify_api_key liberava tudo sem API_KEY; frontend mandava Bearer senha que backend ignorava). Agora: `POST /v1/admin/auth/login` emite HS256 24h; todas as rotas admin exigem Bearer. Env vars no Render: `JWT_SECRET` (gerado, não versionado) + `CORS_ORIGINS` restrita (antes `["*"]`).
   - Senha continua `admin123` (env `ADMIN_PASSWORD`) — trocar quando quiser.
2. **CORS restrito**: default novo no config.py inclui Render + Vercel + LAN dev.
3. **SEO**: `GET /robots.txt` (Disallow /admin) + `GET /sitemap.xml` (75 urls, produtos visíveis cap 500) no FastAPI; frontend com meta/OG defaults + per-page via `src/lib/seo.ts`.
4. **Analytics de views**: `POST /v1/store/products/{id}/view` → `data/product_views.jsonl`; `GET /v1/admin/analytics?days=N`. JSONL é efêmero no Render (zera a cada deploy) — OK pra MVP.
5. **Categorias**: rename bulk `POST /v1/admin/categories/rename {from,to}`; aba Categorias no admin com filtro e rename inline. Endpoint público `GET /v1/store/categories` já existia (campo `name`, não `category`).
6. **Frontend admin**: fluxo JWT completo (`admin_token` em sessionStorage, evento global `admin:unauthorized`, fallback same-origin `/v1`), card Visualizações (30d), view tracking once-per-session no ProductDetail.
7. **Dockerfile**: pyjwt adicionado à lista HARDCODED do pip (o Dockerfile NÃO lê pyproject.toml — causa raiz do primeiro deploy `update_failed`: import crash → health check fail).

### Commits
- inventory-service `7501955` (feat) + `13fb0f1` (fix Dockerfile)
- loja-online `73a7ea4` (frontend completo + vercel.json rewrite→Render)

### Armadilhas encontradas
- **Env var CORS_ORIGINS sobrescreve default do pydantic-settings** — restrição só vale se a env var no Render for atualizada/removida (feito).
- **Render auto-deploy não disparou no push** desta vez (deploy manual via API funcionou: POST /deploys → 202/201). Monitorar P2.
- `tsc --noEmit` solto passou num arquivo com JSX quebrado; `tsc -b` (usado pelo build) pegou — sempre validar com o comando do build real.
- LSP pode reportar erros stale durante edições longas — confiar no tsc/build como gate final.

### Próximos passos sugeridos
1. Trocar `ADMIN_PASSWORD` no Render (ainda `admin123`)
2. Testar painel admin no browser pós-JWT (login, categorias, analytics) — validado só por API
3. Decisão Vercel: arquivar ou recriar
4. Limpar chave órfã `1.png` no R2 (teste do P1)
5. Analytics persistente se quiser histórico entre deploys (hoje zera)
