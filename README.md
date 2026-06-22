# Inventory Service — Omnichannel Adapter

Bridge between **OSPOS** (ERP/aplicação PHP legada) e marketplaces externos (Shopee, Mercado Livre, WooCommerce).

> **Status:** Phase 0-B — Foundation Skeleton  
> Plano completo: `plano_omnichannel_v5.docx`

---

## Arquitetura (resumo)

```
OSPOS (MySQL)
    │
    ▼
┌─────────────────────┐
│   CDC Agent (Fase1) │  Observa binlog do MySQL
└──────┬──────────────┘
       │ eventos
       ▼
┌─────────────────────┐
│   EventStore        │  Tabela event_store no SQLite/Postgres
└──────┬──────────────┘
       │
       ▼
┌─────────────────────┐
│   Adapters          │  WooCommerce, Shopee, Mercado Livre
└─────────────────────┘
```

Cada marketplace é um **Adapter** que implementa a interface `MarketplaceAdapter`.

---

## Stack

| Camada       | Tecnologia                           |
|-------------|--------------------------------------|
| Framework   | FastAPI (Python 3.12+)               |
| ORM         | SQLAlchemy 2.0 (async)               |
| Migrations  | Alembic                              |
| DB (dev)    | SQLite (aiosqlite)                    |
| DB (prod)   | PostgreSQL 16 (asyncpg)              |
| Adapter WWC | HTTPX (REST para WooCommerce API v3) |

---

## Desenvolvimento Local

### 1. Clone + ambiente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configuração

```bash
cp .env.example .env
# Edite .env com suas credenciais
```

### 3. Banco de dados

```bash
alembic upgrade head
```

### 4. Rodar

```bash
uvicorn app.main:app --reload
# → http://localhost:8000
# → Docs: http://localhost:8000/docs
```

### Docker Compose

```bash
docker compose up -d
```

---

## Health Check

```
GET /v1/health
```

Resposta:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "database": "connected",
  "adapters": ["woocommerce"]
}
```

---

## Estrutura

```
inventory-service/
├── app/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Pydantic Settings
│   ├── database.py          # Async engine + session
│   ├── models/              # SQLAlchemy declarative models
│   ├── schemas/             # Pydantic request/response
│   ├── adapters/
│   │   ├── base.py          # MarketplaceAdapter ABC
│   │   ├── registry.py      # AdapterRegistry
│   │   └── implementations/ # WooCommerce, futuros: shopee, ml
│   ├── api/v1/              # Rotas versionadas
│   └── utils/               # Helpers
├── alembic/                 # Migrations
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## Fases Futuras

| Fase | O que faz                              |
|------|----------------------------------------|
| 0-B  | ✅ Skeleton, DB, adapter interface, WooCommerce |
| 1    | EventStore Processor, State Machine, CDC Agent  |
| 2    | Saga Orchestrator, Locking, Sell Pipeline       |
| 3    | Shopee + Mercado Livre adapters                 |
| 4    | AI Onboarding & Enrichment (LLM)                |
