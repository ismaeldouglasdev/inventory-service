# 🛒 Handoff — PC da Loja (OSPOS + Scanner)

**Última atualização:** 27 de Junho de 2026
**Projetos:** `inventory-service` + `loja-online`

---

## 📋 O Que Falta pra Produção

### 1. Conectar ao OSPOS da Loja

O OSPOS da loja roda no MySQL **deste PC**. O inventory-service precisa acessá-lo pra sincronizar os produtos.

**Como fazer:**

1. Editar `/home/ismaeldev/inventory-service/.env` com os dados corretos:

```env
OSPOS_DB_HOST=127.0.0.1
OSPOS_DB_PORT=3306
OSPOS_DB_USER=admin
OSPOS_DB_PASS=pointofsale
OSPOS_DB_NAME=ospos
```

2. Rodar o sync manual pra testar:

```bash
curl -X POST http://localhost:8000/v1/store/sync?mode=full
```

3. Agendar sync automático (cron job neste PC, enquanto ligado):

```bash
# Roda a cada 30 minutos enquanto o PC estiver ligado
*/30 * * * * curl -X POST http://localhost:8000/v1/store/sync?mode=delta
```

---

### 2. Scanner de Código de Barras

O scanner do PC precisa chamar um endpoint quando ler um código.

**Setup no PC da loja:**

Criar script `/usr/local/bin/scan-to-store.sh`:

```bash
#!/bin/bash
# Chamado pelo scanner ao ler um código de barras
BARCODE=$(cat)
curl -s -X POST "http://SERVIDOR:8000/v1/store/scan/$BARCODE"
```

---

### 3. Instalar Dependências no Servidor

```bash
cd ~/inventory-service
source .venv/bin/activate

# Instalar suporte a imagens (rembg + Pillow)
pip install "inventory-service[images]"
```

---

### 4. Fluxo de Fotos (Scanner → Celular)

| Passo | Onde | O que acontece |
|-------|------|----------------|
| 1 | **PC (scanner)** | Lê código de barras → `POST /v1/store/scan/{barcode}` |
| 2 | **Servidor** | Busca produto no DB local, armazena scan, notifica WebSocket |
| 3 | **Celular** | Página `/capturar` no navegador do celular recebe scan via WebSocket |
| 4 | **Celular** | Mostra produto + abre câmera |
| 5 | **Celular** | Tira foto → faz upload → rembg remove fundo |
| 6 | **Servidor** | Salva imagem, marca produto como `store_visible=True` (se stock > 0) |

---

### 5. Fluxo de Onboarding (Classificação por IA)

Se o 9Router estiver rodando, o onboarding pode classificar produtos automaticamente:

```bash
# 1. Criar sessão
curl -X POST "http://localhost:8000/v1/onboarding/session?sku=ABC-123"

# 2. Upload da foto
curl -X POST "http://localhost:8000/v1/onboarding/session/1/images" \
  -F "files=@/path/to/foto.jpg"

# 3. Analisar com IA
curl -X POST "http://localhost:8000/v1/onboarding/session/1/analyze"

# 4. Aplicar ao produto
curl -X POST "http://localhost:8000/v1/onboarding/session/1/apply"
```

---

## 📡 Endpoints da Store API

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/store/products` | Lista produtos (só `store_visible=True`) |
| `GET` | `/v1/store/products/{id}` | Detalhe do produto |
| `GET` | `/v1/store/categories` | Categorias com contagem |
| `GET` | `/v1/store/images/{filename}` | Serve imagem |
| `POST` | `/v1/store/products/{id}/image` | Upload de foto (+ rembg automático) |
| `POST` | `/v1/store/sync` | Gatilha sync do OSPOS |
| `POST` | `/v1/store/scan/{barcode}` | Registra scan do scanner |
| `GET` | `/v1/store/scan/last` | Último scan registrado |
| `WS` | `/v1/store/scan/ws` | WebSocket pra notificações em tempo real |

---

## 🏗️ Arquitetura Geral

```
┌─────────────────────┐   Sync (qdo online)    ┌──────────────────────┐
│  PC da Loja (OSPOS) │◄──────────────────────►│  inventory-service   │
│  ─ MySQL 10k+ prod  │    POST /v1/store/sync  │  ─ FastAPI + SQLite  │
│  ─ Scanner código   │                         │  ─ store_products    │
│  ─ Só online 8-18h  │                         │  ─ rembg             │
└─────────────────────┘                         │  ─ CDC Agent         │
       │                                        │  ─ Sell Pipeline     │
       │ POST /v1/store/scan/{barcode}          │  ─ Circuit Breaker   │
       └────────────────────────────────────────│  ─ AI Onboarding     │
                                                 │  ─ WooCommerce       │
                                                 │  ─ Mercado Livre     │
                                                 │  ─ Shopee            │
                                                 └──────┬───────────────┘
                                                         │
                                               ┌─────────┴──────────┐
                                               │  loja-online       │
                                               │  (React, 24/7)     │
                                               │                    │
                                               │  / → Vitrine       │
                                               │  /produto/:id →    │
                                               │  Detalhe           │
                                               │  /checkout →       │
                                               │  WhatsApp          │
                                               │  /capturar →       │
                                               │  Câmera + Upload   │
                                               └────────────────────┘
```

---

## 🧪 Testando Localmente

```bash
# 1. Subir o inventory-service
cd ~/inventory-service
source .venv/bin/activate
uvicorn app.main:app --reload

# 2. Em outro terminal — subir o frontend
cd ~/loja-online
npm run dev

# 3. No celular — abrir
http://PC_IP:5173/capturar
```

---

## 🐛 Possíveis Problemas

| Problema | Causa | Solução |
|----------|-------|---------|
| Sync retorna 502 | OSPOS MySQL offline/inacessível | Verificar se PC da loja está ligado e MySQL rodando |
| WebSocket desconecta | Proxy nginx sem upgrade | Verificar `proxy_http_version 1.1` e `proxy_set_header Upgrade` |
| rembg não funciona | Modelo não baixado | Primeira execução baixa ~200MB, aguardar |
| Foto não aparece | Arquivo não salvou em `data/images/` | Verificar permissões de escrita |
| AI analysis falha | 9Router não rodando | `curl http://localhost:20128/api/health` |
