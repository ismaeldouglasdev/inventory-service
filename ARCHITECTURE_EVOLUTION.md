# Plano de Evolução — Inventory Service

**Data:** 29 de Junho de 2026
**Ambiente:** Dev machine (ismaeldev)
**Meta:** Robuster production readiness + observabilidade

---

## Fases

| Fase | O que | Impacto | Esforço |
|------|-------|---------|---------|
| **1** | Observabilidade (métricas + logging) | 🔴 Crítico | 2-3 dias |
| **2** | Rate Limiting Distribuído | 🔴 Crítico | 2 dias |
| **3** | Account Health Tracking | 🟡 Alto | 1 dia |
| **4** | Dead Letter Queue + Recovery | 🟡 Alto | 1 dia |
| **5** | Proxy Management / IP Failover | 🟢 Médio | 2 dias |
| **6** | Content Fingerprint | 🟢 Médio | 1 dia |
| **7** | LGPD / Compliance | 🟢 Médio | 2 dias |
| **8** | Cache Layer + Search | 🟢 Baixo | 1 dia |

---

## Fase 1 — Observabilidade

### 1.1 Métricas Prometheus

Adicionar `prometheus_fastapi_instrumentator` ou métricas manuais via `prometheus_client`:

**Endpoint:** `GET /metrics`

**Métricas:**

```
# Request
inventory_requests_total{method, endpoint, status} Counter
inventory_request_duration_seconds{method, endpoint} Histogram

# Channel / Adapter
inventory_adapter_requests_total{channel, operation, status} Counter
inventory_adapter_duration_seconds{channel, operation} Histogram
inventory_adapter_failures_total{channel, operation} Counter
inventory_adapter_rate_limit_remaining{channel} Gauge

# Circuit Breaker
inventory_circuit_breaker_state{channel} Gauge  # 0=CLOSED, 1=HALF_OPEN, 2=OPEN
inventory_circuit_breaker_failures{channel} Gauge

# Event Store
inventory_events_total{state} Gauge
inventory_events_dead_total Counter
inventory_events_processed_total Counter

# CDC Agent
inventory_cdc_polls_total Counter
inventory_cdc_changes_detected Counter

# System
inventory_db_connections Gauge
inventory_db_query_duration_seconds Histogram
inventory_queue_depth{queue} Gauge
```

### 1.2 Structured Logging

Trocar `logging.basicConfig` por:
- **JSON logging** em produção (via `python-json-logger`)
- Suporte a `LOG_FORMAT=json` ou `LOG_FORMAT=text`

### 1.3 Health Check Aprimorado

- `GET /health` → status básico
- `GET /health/ready` → readiness (DB + adapters)
- `GET /health/live` → liveness (só processo vivo)
- `GET /health/channels` → saúde detalhada por canal

---

## Fase 2 — Rate Limiting Distribuído

### 2.1 Token Bucket por Canal

```
inventory-service/
└── app/services/rate_limiter.py
```

- Token bucket com Redis (ou SQLite fallback)
- Configuração por canal via `.env`:
  ```
  RATE_LIMIT_ML=1000/h
  RATE_LIMIT_SHOPEE=3000/d
  RATE_LIMIT_WC=100/m
  ```
- Descobre limites reais dos headers `X-RateLimit-Remaining`
- Backpressure no EventProcessor

### 2.2 Queue com Backpressure

Quando estourar rate limit, ao invés de falhar:
1. Enfileira o evento
2. Espera o bucket recuperar
3. Re-processa

---

## Fase 3 — Account Health Tracking

### 3.1 Modelo `channel_health`

```sql
CREATE TABLE channel_health (
    channel         TEXT PRIMARY KEY,
    status          TEXT NOT NULL,  -- healthy, warning, critical, suspended
    daily_requests  INTEGER DEFAULT 0,
    daily_limit     INTEGER,
    last_error      TEXT,
    last_error_at   TIMESTAMP,
    violations      INTEGER DEFAULT 0,
    checked_at      TIMESTAMP
);
```

### 3.2 Endpoints

- `GET /v1/admin/health` → status consolidado dos canais
- `POST /v1/admin/health/check` → força verificação
- `GET /v1/admin/health/history` → histórico de saúde

### 3.3 Alertas

Quando status mudar pra `warning` ou `critical`, logar com `WARNING` e expor em métrica.

---

## Fase 4 — Dead Letter Queue + Recovery

### 4.1 Endpoints de Recovery

- `GET /v1/admin/events/dead` → lista eventos DEAD
- `POST /v1/admin/events/dead/{id}/reprocess` → reprocessa UM evento
- `POST /v1/admin/events/dead/reprocess-all` → reprocessa todos
- `DELETE /v1/admin/events/dead/{id}` → remove evento (ack)

### 4.2 Notificação de DEAD

Quando um evento vai pra DEAD:
1. Métrica `inventory_events_dead_total` incrementa
2. Log com `ERROR`
3. Opcional: notificação desktop (`notify-send`)

---

## Fase 5 — Proxy Management / IP Failover

### 5.1 Proxy Pool

```
app/services/proxy_manager.py
```

- Pool de proxies rotativos
- Health check de cada proxy
- Failover automático
- Config via `.env`: `PROXY_POOL=http://proxy1:8080,http://proxy2:8080`

---

## Fase 6 — Content Fingerprint

### 6.1 Hash de Imagens

- SHA-256 das imagens no upload
- Tabela `content_fingerprint` pra evitar duplicatas
- Skip de rembg se hash já processado

---

## Fase 7 — LGPD / Compliance

### 7.1 Data Retention

- Política configurável: `DATA_RETENTION_DAYS=180`
- Job que deleta dados de cliente após N dias
- Anonimização (hash dos campos PII)

### 7.2 Consentimento

- Tabela `customer_consent`
- Endpoint `GET /v1/customer/data` → exportar dados
- Endpoint `DELETE /v1/customer/data` → esquecer cliente

---

## Fase 8 — Cache Layer

### 8.1 Redis Cache

- Cache de `store_products` (mais acessados)
- Cache de `categories` com contagem
- Invalidação por evento no EventStore
- Fallback pra SQLite se Redis offline

---

## Ordem de Implementação

```
Semana 1:  Fase 1 (Observabilidade)
Semana 2:  Fase 2 (Rate Limiting)
Semana 3:  Fase 3 + 4 (Health + Dead Letter)
Semana 4+: Fase 5-8 (melhorias contínuas)
```
