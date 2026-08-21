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
