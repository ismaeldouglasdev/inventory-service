# 🔒 Auditoria de Segurança — Inventory Service

**Data:** 29/ago/2026
**Escopo:** Pipeline completa (FastAPI `app/`) — autenticação, exposição de dados, tokens ML, endpoints sensíveis.
**Método:** Revisão manual de código (team-mode indisponível neste ambiente).

---

## Resumo Executivo

A API **não tem autenticação efetiva em praticamente nenhum endpoint sensível**. O mecanismo de API key existe mas está **desativado por design** (bypass em `security.py`), e o `API_KEY` no `.env` está **vazio**. Isso significa que **qualquer pessoa com acesso à rede pode**:

- Ler o catálogo inteiro do OSPOS (nomes, preços, custo, estoque)
- **Publicar itens no Mercado Livre** (vender em nome da loja)
- **Ler os tokens OAuth do Mercado Livre** (access_token + refresh_token em texto puro)
- Disparar syncs pesados (DoS por I/O)
- Baixar o APK do app Android (revela host/endpoints internos)

**Severidade geral: CRÍTICA.** Recomenda-se corrigir antes de expor o serviço fora da LAN.

---

## Achados por Severidade

### 🔴 CRÍTICO

| # | Endpoint / Arquivo | Problema | Impacto |
|---|--------------------|----------|---------|
| C1 | `app/api/v1/mercadolivre.py` — `GET /v1/mercadolivre/token-debug` | Expõe `access_token` + `refresh_token` do ML em texto puro, **sem autenticação** | Atacante rouba tokens e opera a conta ML da loja (publicar, alterar preços, ler vendas) |
| C2 | `app/api/v1/mercadolivre.py` — `POST /v1/mercadolivre/publish` | **Sem autenticação** | Qualquer um publica itens no ML em nome da loja |
| C3 | `app/utils/security.py:90` — `verify_api_key` | `if not api_key: return` — **ausência de chave permite acesso** (inverso do esperado) | Bypass total da autenticação por API key |
| C4 | `app/utils/security.py:84` | `if not settings.api_key: return` — API_KEY vazio = acesso aberto | `.env` atual tem `API_KEY` vazio → **tudo aberto** |
| C5 | `app/utils/security.py:91` | `"dummy-key"` hardcoded aceita chave fixa conhecida | Qualquer um que conheça `dummy-key` (pública no código) passa na auth |
| C6 | `app/api/v1/store.py` — `GET /v1/store/sync-total` | **Sem autenticação** — expõe catálogo inteiro (nomes, preços, custo, estoque) | Vazamento de dados comerciais completos |
| C7 | `app/api/v1/store.py` — `POST /v1/store/sync` | **Sem autenticação** — dispara sync full (varre 8.5k produtos) | DoS por I/O; consumo de recursos |

### 🟠 ALTO

| # | Endpoint / Arquivo | Problema | Impacto |
|---|--------------------|----------|---------|
| A1 | `app/config.py` | `admin_password = "admin123"` como default | Credencial fraca/conhecida se não sobrescrita por env |
| A2 | `app/utils/security.py:28-46` | `jwt_secret` vazio → secret **efêmero por processo** | Tokens admin invalidados a cada restart; se 2 processos rodarem, tokens não validam entre eles |
| A3 | `app/api/v1/store.py` — `GET /v1/store/ospos-item-images/{filename}` | **Sem autenticação** — serve fotos do OSPOS | Exposição de imagens de produtos (menor, mas sem controle) |
| A4 | `app/api/v1/store.py` — `POST /v1/store/products/{id}/image/link` | **Sem autenticação** — linka imagens | Modificação não autorizada de produtos |
| A5 | `app/api/v1/store.py` — `GET /v1/store/scan/{barcode}` | **Sem autenticação** | Leitura de dados de produto por barcode |
| A6 | `app/api/v1/store.py` — `GET /v1/store/photos/recent` | **Sem autenticação** | Vaza histórico de uploads de fotos |
| A7 | `app/main.py:237` — `GET /metrics` | **Sem autenticação** — métricas Prometheus | Vaza info interna (endpoints, latências, contagens) |
| A8 | `app/main.py:266` — `GET /app-debug.apk` | **Sem autenticação** — serve o APK | Revela host/endpoints internos do app Android |

### 🟡 MÉDIO

| # | Endpoint / Arquivo | Problema | Impacto |
|---|--------------------|----------|---------|
| M1 | `app/api/v1/admin.py` — `GET /v1/admin/health/detailed` | **Sem autenticação** | Info detalhada de health/registry |
| M2 | `app/utils/security.py:137-141` — `_client_ip` | Confia em `X-Forwarded-For` sem validar proxy | Spoofing de IP → bypass de rate limit |
| M3 | `app/main.py:178-184` — CORS | `allow_origins=settings.cors_origins_list` — verificar se não é `*` | Se `*` com `allow_credentials=True`, qualquer origem pode fazer requests autenticados |

---

## Caminho de Ataque (cenário realista)

1. Atacante na LAN (ou via túnel Cloudflare se exposto) chama `GET /v1/mercadolivre/token-debug`
2. Recebe `access_token` + `refresh_token` do ML em texto puro
3. Usa os tokens para operar a conta ML: publicar itens, alterar preços/estoque, ler vendas
4. Alternativamente, chama `POST /v1/mercadolivre/publish` direto para publicar itens arbitrários

**Tudo isso sem nenhuma credencial.**

---

## Correções Recomendadas

### 1. Corrigir `verify_api_key` (C3, C4, C5) — `app/utils/security.py`

```python
async def verify_api_key(request: Request, api_key: Optional[str] = Depends(api_key_header)) -> None:
    """Protect sensitive endpoints. REQUIRES a valid API key."""
    if not settings.api_key:
        # Em produção, API_KEY DEVE estar configurada. Se não estiver, nega.
        raise HTTPException(status_code=503, detail="API key not configured")
    if not api_key:
        api_key = request.query_params.get("api_key")
    if not api_key or api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "API-Key"},
        )
```

- **Remover** o `"dummy-key"` hardcoded.
- **Remover** o bypass `if not api_key: return`.

### 2. Proteger endpoints ML (C1, C2) — `app/api/v1/mercadolivre.py`

- `GET /v1/mercadolivre/token-debug` → **remover** ou proteger com `verify_admin_auth` (JWT admin). Nunca expor tokens em texto puro.
- `POST /v1/mercadolivre/publish` → adicionar `Depends(verify_api_key)` (após corrigir o bypass).

### 3. Proteger endpoints store (C6, C7, A3-A6) — `app/api/v1/store.py`

- `GET /v1/store/sync-total`, `POST /v1/store/sync`, `GET /v1/store/ospos-item-images/{filename}`, `POST /v1/store/products/{id}/image/link`, `GET /v1/store/scan/{barcode}`, `GET /v1/store/photos/recent` → adicionar `Depends(verify_api_key)`.

### 4. Configurar secrets reais — `app/config.py` + `.env`

- Definir `API_KEY` forte no `.env` (ex.: `openssl rand -hex 32`).
- Definir `JWT_SECRET` forte (ex.: `openssl rand -hex 32`).
- Definir `ADMIN_PASSWORD` forte (não `admin123`).

### 5. Proteger `/metrics` e `/app-debug.apk` (A7, A8) — `app/main.py`

- `/metrics` → restringir a IP interno ou exigir auth.
- `/app-debug.apk` → remover ou proteger (o APK contém host/endpoints internos).

### 6. Validar `X-Forwarded-For` (M2)

- Só confiar no header se o serviço estiver atrás de um proxy confiável (ex.: Cloudflare). Caso contrário, usar `request.client.host`.

### 7. Revisar CORS (M3)

- Garantir que `cors_origins_list` não seja `["*"]` com `allow_credentials=True`.

---

## Prioridade de Correção

1. **C1/C2** — tokens ML + publish (risco financeiro imediato)
2. **C3/C4/C5** — corrigir bypass da API key (habilita todas as outras proteções)
3. **C6/C7** — sync-total + sync (vazamento de dados + DoS)
4. **A1/A2** — secrets reais
5. **A7/A8** — metrics + APK
6. **A3-A6, M1-M3** — demais endpoints

---

## Nota

Este relatório é uma **auditoria manual** (a skill `security-review` requer team-mode, indisponível neste ambiente). Recomenda-se, após as correções, rodar uma auditoria automatizada (ex.: `bandit`, `semgrep`) e re-testar com a skill quando o team-mode estiver disponível.
