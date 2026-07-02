<p align="center">
  <strong>🇧🇷 Português</strong> &nbsp;|&nbsp; <a href="README.en.md">🇺🇸 English</a>
</p>

# Inventory Service — Omnichannel Adapter

Bridge entre **OSPOS** (PDV local) e marketplaces digitais (Shopee, Mercado Livre, WooCommerce), com loja online, pipeline de vendas e onboarding inteligente via IA.

**Status:** Production-ready — 4 fases implementadas, 75 testes passando.

---

## Índice

- [Arquitetura](#arquitetura)
- [Stack](#stack)
- [Quickstart](#quickstart)
- [Configuração](#configuração)
- [API Reference](#api-reference)
- [Fluxo de Eventos](#fluxo-de-eventos)
- [Sell Pipeline](#sell-pipeline)
- [Adapters](#adapters)
- [AI Onboarding](#ai-onboarding)
- [Modelos de Dados](#modelos-de-dados)
- [Testes](#testes)
- [Estrutura do Projeto](#estrutura-do-projeto)

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────────────┐
│                       PC da Loja (OSPOS)                        │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐  │
│  │  MySQL       │◄───│   CDC Agent  │───►│   EventStore      │  │
│  │  ospos_items │    │  (polling)   │    │   (event sourcing)│  │
│  └──────────────┘    └──────────────┘    └────────┬──────────┘  │
│                                                    │             │
└────────────────────────────────────────────────────┼─────────────┘
                                                     │
                     ┌───────────────────────────────▼──────────────┐
                     │           Inventory Service (FastAPI)         │
                     │                                               │
                     │  ┌─────────────────┐  ┌──────────────────┐   │
                     │  │ Event Processor │  │  Sell Pipeline   │   │
                     │  │ (state machine) │  │  reserve→confirm │   │
                     │  └────────┬────────┘  │  →commit         │   │
                     │           │           └────────┬─────────┘   │
                     │           ▼                     ▼            │
                     │  ┌─────────────────────────────────────┐     │
                     │  │         Adapter Registry            │     │
                     │  │  WooCommerce │ ML │ Shopee          │     │
                     │  └─────────────────────────────────────┘     │
                     │                                               │
                     │  ┌─────────────────────────────────────┐     │
                     │  │      AI Onboarding (LLM Vision)     │     │
                     │  │  classifica produtos por foto       │     │
                     │  └─────────────────────────────────────┘     │
                     │                                               │
                     │  ┌─────────────────────────────────────┐     │
                     │  │      Store API (loja-online)        │     │
                     │  │  /v1/store/products, /scan, /ws     │     │
                     │  └─────────────────────────────────────┘     │
                     └──────────────────────────────────────────────┘
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
        ┌────────────┐     ┌──────────────┐     ┌──────────────┐
        │ WooCommerce │     │ Mercado Livre│     │   Shopee     │
        └────────────┘     └──────────────┘     └──────────────┘
```

### Componentes

| Camada | Componente | Responsabilidade |
|--------|-----------|-----------------|
| Fonte física | OSPOS (MySQL) | Estoque real + vendas locais |
| Observador | CDC Agent | Captura mudanças no MySQL (polling + fallback REST) |
| Cérebro | Inventory Service | Orquestra tudo — eventos, vendas, adapters |
| Armazenamento | EventStore | Event sourcing com state machine |
| Vendas | Sell Pipeline | Reserva → Confirmação → Commit |
| Distribuição | WooCommerce | Hub de canais |
| Canais | Shopee + Mercado Livre | Vendas externas |
| IA | Onboarding | Classifica produtos por imagem via LLM |

---

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Framework | FastAPI (Python 3.12+) |
| ORM | SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| DB (dev) | SQLite (aiosqlite) |
| DB (prod) | PostgreSQL 16 (asyncpg) |
| HTTP | httpx |
| Auth | HMAC-SHA256 (Shopee), OAuth 2.0 (ML), Basic Auth (WooCommerce) |
| IA | OpenAI-compatible API (9Router, OpenAI, Anthropic) |
| Testes | pytest + pytest-asyncio |

---

## Quickstart

```bash
# 1. Clone + ambiente
git clone https://github.com/ismaeldouglasdev/inventory-service.git
cd inventory-service
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,images]"

# 2. Configurar
cp .env.example .env
# Edite .env com suas credenciais

# 3. Banco
alembic upgrade head

# 4. Rodar
uvicorn app.main:app --reload
# → http://localhost:8000
# → Swagger: http://localhost:8000/docs
```

### Docker

```bash
docker compose up -d
```

---

## Configuração

Todas as configurações via variáveis de ambiente (`.env`). Ver [`.env.example`](.env.example).

### Banco de Dados

| Variável | Default | Descrição |
|----------|---------|-----------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/inventory.db` | SQLite dev / PostgreSQL prod |

### FastAPI

| Variável | Default | Descrição |
|----------|---------|-----------|
| `HOST` | `0.0.0.0` | Host do servidor |
| `PORT` | `8000` | Porta do servidor |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | Origens permitidas |
| `LOG_LEVEL` | `INFO` | Nível de log |

### WooCommerce

| Variável | Descrição |
|----------|-----------|
| `WOOD_COMMERCE_URL` | URL da loja WooCommerce |
| `WOOD_COMMERCE_CONSUMER_KEY` | Consumer Key (Basic Auth) |
| `WOOD_COMMERCE_CONSUMER_SECRET` | Consumer Secret |

### Mercado Livre

| Variável | Descrição |
|----------|-----------|
| `ML_CLIENT_ID` | App ID do Mercado Livre |
| `ML_CLIENT_SECRET` | App Secret |
| `ML_REDIRECT_URI` | Callback URL (registrada no app) |
| `ML_ACCESS_TOKEN` | Token após OAuth |
| `ML_REFRESH_TOKEN` | Refresh token |
| `ML_USER_ID` | User ID do vendedor |

### Shopee

| Variável | Descrição |
|----------|-----------|
| `SHOPEE_PARTNER_ID` | Partner ID da Shopee |
| `SHOPEE_API_KEY` | API Key |
| `SHOPEE_REDIRECT_URI` | Callback URL |
| `SHOPEE_SANDBOX` | `true` = sandbox, `false` = produção |
| `SHOPEE_ACCESS_TOKEN` | Token após OAuth |
| `SHOPEE_REFRESH_TOKEN` | Refresh token |
| `SHOPEE_SHOP_ID` | Shop ID |

### AI / LLM (Onboarding)

| Variável | Default | Descrição |
|----------|---------|-----------|
| `AI_API_URL` | — | URL da API OpenAI-compatível (ex: `http://localhost:20128`) |
| `AI_API_KEY` | — | Chave de API |
| `AI_MODEL` | `gpt-4o` | Modelo de visão |
| `AI_MAX_IMAGES` | `4` | Máx. imagens por análise |

### OSPOS / CDC

| Variável | Default | Descrição |
|----------|---------|-----------|
| `OSPOS_DB_HOST` | `localhost` | Host do MySQL do OSPOS |
| `OSPOS_DB_PORT` | `3306` | Porta MySQL |
| `OSPOS_DB_NAME` | `ospos` | Nome do banco |
| `OSPOS_DB_USER` | `root` | Usuário MySQL |
| `OSPOS_DB_PASS` | — | Senha MySQL |
| `CDC_ENABLED` | `true` | Liga/desliga o CDC Agent |
| `CDC_POLL_INTERVAL` | `30` | Segundos entre polls |

---

## API Reference

### Health

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/health` | Status do serviço + adapters registrados |

### Store (loja online)

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/store/products` | Lista produtos visíveis |
| `GET` | `/v1/store/products/{id}` | Detalhe do produto |
| `GET` | `/v1/store/categories` | Categorias com contagem |
| `GET` | `/v1/store/images/{filename}` | Servir imagem |
| `POST` | `/v1/store/products/{id}/image` | Upload de foto (+ rembg) |
| `POST` | `/v1/store/sync` | Gatilha sync do OSPOS |
| `POST` | `/v1/store/scan/{barcode}` | Registra scan do código de barras |
| `GET` | `/v1/store/scan/last` | Último scan registrado |
| `WS` | `/v1/store/scan/ws` | WebSocket para notificações em tempo real |

### Sell Pipeline

| Método | Rota | Descrição |
|--------|------|-----------|
| `POST` | `/v1/sell/reserve` | Reserva estoque para um pedido |
| `POST` | `/v1/sell/confirm` | Confirma reserva (OSPOS deduziu) |
| `POST` | `/v1/sell/commit` | Marca como commitado (canal propagou) |
| `POST` | `/v1/sell/cancel` | Cancela reserva e restaura estoque |
| `POST` | `/v1/sell/sell` | Fluxo completo: reserve → confirm → propagate → commit |
| `GET` | `/v1/sell/reservations/{id}` | Detalhe da reserva |
| `GET` | `/v1/sell/reservations` | Lista reservas com filtros |

### Onboarding

| Método | Rota | Descrição |
|--------|------|-----------|
| `POST` | `/v1/onboarding/session` | Cria sessão de onboarding |
| `GET` | `/v1/onboarding/session/{id}` | Detalhe da sessão |
| `GET` | `/v1/onboarding/sessions` | Lista sessões |
| `POST` | `/v1/onboarding/session/{id}/images` | Upload de fotos |
| `POST` | `/v1/onboarding/session/{id}/analyze` | Executa análise por IA |
| `POST` | `/v1/onboarding/session/{id}/apply` | Aplica atributos ao produto |

### WooCommerce

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/woocommerce/status` | Status do adapter |

### Mercado Livre

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/mercadolivre/auth-url` | URL de autorização OAuth |
| `GET` | `/v1/mercadolivre/callback` | Callback OAuth |

### Shopee

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/shopee/auth-url` | URL de autorização |
| `GET` | `/v1/shopee/callback` | Callback OAuth |
| `GET` | `/v1/shopee/status` | Status do adapter |
| `POST` | `/v1/shopee/refresh` | Refresh manual do token |

### Products (eventos)

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/v1/products/events` | Lista eventos |
| `GET` | `/v1/products/events/{id}` | Detalhe do evento |

---

## Fluxo de Eventos

### State Machine

Cada evento no EventStore segue uma máquina de estados:

```
                    ┌──────────┐
                    │ PENDING  │
                    └────┬─────┘
                         ▼
                    ┌────────────┐
              ┌────►│ PROCESSING │
              │     └─────┬──────┘
              │           │
              │    ┌──────┼──────────┐
              │    ▼      ▼          ▼
              │ ┌──────┐ ┌──────┐ ┌──────┐
              │ │FAILED│ │PARTIAL│ │COMPL.│
              │ └──┬───┘ └──┬───┘ └──────┘
              │    │        │
              └────┘        └──────────┐
                                       ▼
                                  ┌────────┐
                                  │  DEAD  │
                                  └────────┘
```

### Transições válidas

| De | Para | Quando |
|----|------|--------|
| PENDING | PROCESSING | Worker pega o evento |
| PROCESSING | COMPLETED | Todos os canais OK |
| PROCESSING | FAILED | Todos os canais falharam |
| PROCESSING | PARTIAL | Alguns canais falharam |
| FAILED | PROCESSING | Retry (com backoff) |
| FAILED | DEAD | Retries exauridos |
| PARTIAL | PROCESSING | Retry dos canais que falharam |
| DEAD | PENDING | Apenas intervenção manual |

### CDC Agent

O **CDC Agent** observa o MySQL do OSPOS periodicamente:

1. **Polling**: consulta `ospos_items` a cada N segundos
2. **Hash diff**: compara hash dos campos relevantes com `product_mapping.last_hash`
3. **Evento**: se mudou, cria evento no EventStore (ex: `stock.updated`, `product.created`, `price.updated`)
4. **Fallback**: se MySQL falha, tenta REST API

### EventStore Processor

O **EventStore Processor** é um worker loop que:

1. Busca eventos PENDING/FAILED/PARTIAL
2. Para cada evento, executa o adapter do canal correspondente
3. Atualiza o estado conforme resultado
4. Se falhou, aplica exponential backoff: `2^retry_count * 10s`
5. Se exauriu retries → DEAD

---

## Sell Pipeline

### Ciclo de vida da reserva

```
┌──────────┐    ┌──────────┐    ┌──────────┐
│ RESERVED │───►│CONFIRMED │───►│COMMITTED │
└──────────┘    └──────────┘    └──────────┘
     │                              │
     ▼                              ▼
 cancelled                      done
```

1. **RESERVED**: estoque reservado (venda online recebida)
   - Valida estoque disponível
   - Decrementa `store_products.stock`
   - Rejeita duplicatas (mesmo `order_id` + `sku`)
2. **CONFIRMED**: OSPOS deduziu o estoque físico
   - Vincula `ospos_sale_id` para rastreabilidade
3. **COMMITTED**: canal externo confirmou a propagação
   - Estado terminal de sucesso
4. **CANCELLED**: reversão — restaura estoque

### Idempotência

A tabela `processed_actions` (PK: `event_id` + `target_system`) garante que um evento nunca seja aplicado duas vezes ao mesmo canal.

### Circuit Breaker

Cada canal tem um circuit breaker com estados:

- **CLOSED**: normal — requisições passam
- **OPEN**: após N falhas consecutivas — requisições são rejeitadas
- **HALF_OPEN**: após cooldown — permite um request de teste

Transições:
- CLOSED → OPEN: `failure_count >= threshold` (default: 5)
- OPEN → HALF_OPEN: após cooldown (default: 30s)
- HALF_OPEN → CLOSED: sucesso
- HALF_OPEN → OPEN: falha

---

## Adapters

### Interface comum

Todos os adapters implementam `MarketplaceAdapter`:

| Método | Descrição |
|--------|-----------|
| `authenticate()` | Verifica credenciais |
| `update_stock(sku, qty)` | Atualiza estoque |
| `update_price(sku, price)` | Atualiza preço |
| `publish_product(product)` | Publica produto novo |
| `parse_webhook(payload)` | Normaliza webhook |
| `get_external_id(sku)` | Resolve SKU → ID externo |

### WooCommerce
- REST API v3 com Basic Auth
- Endpoints: `/wp-json/wc/v3/*`

### Mercado Livre
- OAuth 2.0 com refresh automático
- API: `https://api.mercadolibre.com`
- SKU armazenado em `seller_custom_field`

### Shopee
- Open Platform v2 com HMAC-SHA256
- Sandbox: `https://partner.test-stable.shopeemobile.com`
- Produção: `https://partner.shopeemobile.com`
- Stock: `/api/v2/product/update_stock`
- Price: `/api/v2/product/update_price`
- Produto: `/api/v2/product/add`

---

## AI Onboarding

### Fluxo

1. **Criar sessão**: `POST /v1/onboarding/session?sku=ABC-123`
2. **Upload de fotos**: `POST /v1/onboarding/session/{id}/images` (1-4 imagens)
3. **Analisar**: `POST /v1/onboarding/session/{id}/analyze`
   - Envia imagens para LLM vision
   - Extrai: categoria, marca, nome sugerido, descrição, atributos (cor, material, tamanho)
4. **Aplicar**: `POST /v1/onboarding/session/{id}/apply`
   - Atualiza `store_products` com dados extraídos

### Provider de IA

O serviço é compatível com qualquer API OpenAI-compatível:

- **9Router** (recomendado): `http://localhost:20128`
- **OpenAI**: `https://api.openai.com`
- **Anthropic**: via roteador compatível

Se `AI_API_URL` não estiver configurado, o serviço usa fallback com dados genéricos.

---

## Modelos de Dados

### `store_products`
Produtos sincronizados do OSPOS para exibição na loja online.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | int | PK |
| `ospos_id` | int | ID no OSPOS (unique) |
| `sku` | string | SKU do produto |
| `name` | string | Nome |
| `description` | text | Descrição |
| `price` | float | Preço |
| `category` | string | Categoria |
| `stock` | int | Estoque |
| `image_url` | string? | URL da imagem |
| `store_visible` | bool | Visível na loja (stock > 0 + imagem) |

### `event_store`
Event sourcing para change-data-capture.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string (UUID) | PK |
| `event_type` | string | Tipo do evento |
| `payload` | JSON | Dados do evento |
| `state` | string | pending/processing/completed/failed/partial/dead |
| `sku` | string? | SKU associado |
| `channel` | string? | Canal alvo |
| `ospos_synced` | bool | Sincronizado com OSPOS |
| `retry_count` | int | Tentativas atuais |
| `max_retries` | int | Máx. tentativas (default: 5) |

### `inventory_state`
Pipeline de vendas (reserva → confirmação → commit).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | int | PK |
| `sku` | string | SKU |
| `order_id` | string | ID do pedido |
| `channel` | string | Canal da venda |
| `state` | string | reserved/confirmed/committed/cancelled |
| `quantity` | int | Quantidade |
| `unit_price` | float | Preço unitário |
| `total` | float | Total |
| `ospos_sale_id` | string? | ID da venda no OSPOS |

### `processed_actions`
Idempotência — garante que cada evento seja processado uma única vez por canal.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `event_id` | string | PK (parte 1) |
| `target_system` | string | PK (parte 2) — ex: woocommerce |
| `status` | string | ok/failed/skipped |
| `action_type` | string | update_stock/update_price/publish_product |
| `duration_ms` | int? | Tempo de execução |

### `channel_state`
Circuit breaker por canal.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `channel` | string | PK — nome do canal |
| `status` | string | CLOSED/OPEN/HALF_OPEN |
| `active` | bool | Ativo? |
| `failure_count` | int | Falhas consecutivas |
| `open_until` | datetime? | Até quando fica OPEN |

### `onboarding_sessions` / `onboarding_images`
Sessões de onboarding com IA.

---

## Testes

```bash
# Todos os testes
pytest -v

# Por módulo
pytest tests/test_sell_pipeline.py -v
pytest tests/test_circuit_breaker.py -v
pytest tests/test_shopee_adapter.py -v
pytest tests/test_onboarding.py -v
pytest tests/test_event_processor.py -v

# Com cobertura
pip install pytest-cov
pytest --cov=app tests/
```

**75 testes** divididos em:

| Módulo | Testes | O que cobre |
|--------|--------|-------------|
| event_processor | 21 | State machine, criação de eventos, processamento, retry, backoff |
| sell_pipeline | 17 | Reserve, confirm, commit, cancel, sell, idempotência |
| circuit_breaker | 9 | CLOSED→OPEN→HALF_OPEN, context guard, ProcessedAction |
| shopee_adapter | 14 | HMAC signing, auth, stock, price, product, webhook |
| onboarding | 14 | Session, image upload, AI analysis, apply, LLM parsing |

---

## Estrutura do Projeto

```
inventory-service/
├── app/
│   ├── main.py                  # FastAPI app + lifespan
│   ├── config.py                # Pydantic Settings
│   ├── database.py              # Async engine + session factory
│   ├──── models/
│   │   ├── store_product.py     # Produto da loja online
│   │   ├── event_store.py       # Event sourcing
│   │   ├── inventory_state.py   # Pipeline de vendas
│   │   ├── processed_action.py  # Idempotência
│   │   ├── channel_state.py     # Circuit breaker
│   │   ├── channel_fee_config.py
│   │   ├── channel_product_mapping.py / channel_variant_mapping.py
│   │   ├── product_mapping.py   # Mapping OSPOS → serviço
│   │   ├── onboarding.py        # Sessões de onboarding
│   ├── schemas/
│   │   ├── health.py
│   │   └── product.py
│   ├── adapters/
│   │   ├── base.py              # MarketplaceAdapter ABC
│   │   ├── registry.py          # AdapterRegistry
│   │   └── implementations/
│   │       ├── woocommerce.py   # WooCommerce REST API v3
│   │       ├── mercadolivre.py  # Mercado Livre OAuth 2.0
│   │       └── shopee.py        # Shopee Open Platform v2 (HMAC)
│   ├── api/v1/
│   │   ├── health.py
│   │   ├── products.py          # Eventos
│   │   ├── store.py             # Loja online
│   │   ├── sell.py              # Pipeline de vendas
│   │   ├── onboarding.py        # AI Onboarding
│   │   ├── woocommerce.py
│   │   ├── mercadolivre.py
│   │   └── shopee.py
│   └── services/
│       ├── cdc_agent.py         # Change Data Capture
│       ├── event_processor.py   # State machine worker
│       ├── store_sync.py        # Sync OSPOS → SQLite
│       ├── sell_pipeline.py     # Pipeline de vendas
│       ├── circuit_breaker.py   # Circuit breaker
│       └── onboarding.py        # AI onboarding service
├── alembic/                     # Migrations
├── tests/                       # 75 testes
├── data/                        # SQLite + imagens (gitignored)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Fases Implementadas

| Fase | Status | Descrição |
|------|--------|-----------|
| 0-B | ✅ | Skeleton, DB, adapter interface, WooCommerce |
| 1 | ✅ | EventStore Processor, State Machine, CDC Agent |
| 2 | ✅ | Sell Pipeline, Inventory State, Circuit Breaker |
| 3 | ✅ | Shopee + Mercado Livre adapters |
| 4 | ✅ | AI Onboarding & Enrichment (LLM Vision) |
