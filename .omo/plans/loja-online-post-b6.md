# Loja Online — Próximos Passos (Pós B1-B6)

**Status:** awaiting-approval
**Created:** 2026-09-11
**Scope:** Backend cleanup + Security hardening + Frontend integration (checkout completo guest+customer)

---

## Context

B1-B6 concluído: Customer auth, pedidos (POST/GET/PUT), status, frete, basedpyright LSP, deploy Render. 132 testes passing, 0 LSP errors. Push para GitHub `sync/prod-data` + `main`.

O frontend loja-online NÃO usa os novos endpoints. Checkout não existe. Admin password é `admin123`. Existe auth JWT implementada mas não aplicada a todos os endpoints. Vercel deploy morto.

---

## Fases

### Fase C1: Backend Cleanup + Segurança (BLOQUEANTE)

**Motivo:** Antes de integrar o frontend, o backend precisa estar seguro e limpo.

- [ ] 1. Commit de mudanças pendentes: `app/api/v1/admin.py` (refactor paths) + `app/api/v1/agent_bridge.py` (+339 SSE/dashboard) + `agent-workers/`
  - Arquivos: `app/api/v1/admin.py`, `app/api/v1/agent_bridge.py`, `agent-workers/`
  - QA: `git status --short` mostra working tree limpo após commit

- [ ] 2. Remover TODOs stale em `app/models/onboarding.py` (linhas 21 e 52) — Phase 4 já implementada
  - Arquivo: `app/models/onboarding.py`
  - QA: `grep -n "TODO" app/models/onboarding.py` retorna vazio

- [ ] 3. Criar model SQLAlchemy para `event_store_archive` (migration existe, model não)
  - Arquivo: `app/models/event_store_archive.py` + adicionar em `app/models/__init__.py`
  - QA: `py_compile app/models/event_store_archive.py` OK; migration não tenta dropar tabela

- [ ] 4. Proteger endpoints admin com JWT: todas as rotas `/v1/admin/*` exigem Bearer token
  - Arquivo: `app/api/v1/admin.py` — aplicar `Depends(verify_admin_auth)` em todas as rotas
  - Referência: `app/utils/security.py` já tem `verify_admin_auth` e `create_admin_token`
  - QA: `curl /v1/admin/products` sem token → 401; com token válido → 200

- [ ] 5. Proteger endpoints sensíveis: sell, orders admin, onboarding admin
  - Arquivos: `app/api/v1/sell.py` (rotas admin: reserve/confirm/commit/cancel), `app/api/v1/orders.py` (admin_orders_router), `app/api/v1/onboarding.py`
  - QA: `POST /v1/sell/reserve` sem token → 401 (ou rate-limited); com token → 200

- [ ] 6. Remover ou proteger `/v1/mercadolivre/token-debug` (expõe tokens raw em plaintext)
  - Arquivo: `app/api/v1/mercadolivre.py` — remover endpoint ou adicionar `Depends(verify_admin_auth)`
  - QA: `GET /v1/mercadolivre/token-debug` sem token → 401 ou 404

- [ ] 7. Atualizar `PROJECT-HANDOFF.md` seção 12 (pendências) — marcar P1/P2 como resolvidos, atualizar status
  - Arquivo: `PROJECT-HANDOFF.md`
  - QA: `grep "RESOLVIDO" PROJECT-HANDOFF.md` mostra P1/P2/P3 como resolvidos

- [ ] 8. Rodar suíte completa e confirmar verde
  - QA: `venv/bin/python -m pytest -q` → 132+ passed, 0 failed

### Fase C2: Frontend — Checkout Completo (guest + customer)

**Motivo:** O loja-online precisa usar os novos endpoints para completar o fluxo de compra.

#### C2a: API layer no frontend

- [ ] 9. Criar `src/lib/orderApi.ts` — cliente para endpoints de pedidos
  - Endpoints: `POST /v1/orders`, `GET /v1/orders/{id}`, `GET /v1/shipping/quote`
  - Tipos: `OrderCreate`, `OrderItemIn`, `OrderOut`, `ShippingQuote`
  - QA: `npx tsc --noEmit` sem erros em orderApi.ts

- [ ] 10. Criar `src/lib/authApi.ts` — cliente para customer auth
  - Endpoints: `POST /v1/customer/auth/register`, `POST /v1/customer/auth/login`, `GET /v1/customer/auth/me`
  - Persistência: token JWT em localStorage, refresh automático
  - QA: `npx tsc --noEmit` sem erros em authApi.ts

#### C2b: Páginas de checkout

- [ ] 11. Criar página `src/pages/Checkout.tsx` — fluxo de compra completo
  - Guest: formulário nome/email/telefone/whatsapp → resumo → criar pedido
  - Customer logado: dados preenchidos do perfil → resumo → criar pedido
  - Frete: chamar `GET /v1/shipping/quote` ao carregar, mostrar opções (Padrão/Expresso)
  - CTA final: "Confirmar Pedido via WhatsApp" (abre link wa.me/{whatsapp}?text=...)
  - QA: Playwright test — fluxo guest completo (fill form → select shipping → confirm → redirect WhatsApp)

- [ ] 12. Criar componente `src/components/OrderSummary.tsx`
  - Itens do carrinho (nome, qty, preço unitário, subtotal)
  - Opção de frete selecionada (preço + prazo)
  - Total = subtotal + frete
  - QA: Renderiza corretamente com dados mock

- [ ] 13. Criar componente `src/components/ShippingSelector.tsx`
  - Lista opções de frete do endpoint `/v1/shipping/quote`
  - Radio buttons Padrão/Expresso com preço + prazo
  - Seleção atualizada no carrinho
  - QA: Seleciona opção → preço atualiza no OrderSummary

#### C2c: Auth no frontend

- [ ] 14. Criar página `src/pages/Login.tsx` — login de customer
  - Formulário: email + senha
  - Botão: "Criar conta" → redireciona para Registro
  - Botão: "Continuar como convidado" → volta pro checkout
  - QA: Playwright — login com credenciais válidas → redireciona para checkout

- [ ] 15. Criar página `src/pages/Register.tsx` — registro de customer
  - Formulário: nome, email, telefone, senha
  - Validação client-side (email format, phone format)
  - Após registro → login automático → checkout
  - QA: Playwright — registro completo → volta para checkout com dados preenchidos

- [ ] 16. Criar hook `src/hooks/useAuth.ts` — gerenciamento de estado de autenticação
  - Estado: user, token, isAuthenticated, isLoading
  - Persistência: localStorage
  - Métodos: login, register, logout, refreshProfile
  - QA: Unit test — login salva token, logout limpa, refreshProfile busca dados

#### C2d: Carrinho → Checkout

- [ ] 17. Modificar `src/pages/Cart.tsx` (ou criar se não existe) — botão "Finalizar Compra"
  - Botão leva para `/checkout` com itens do carrinho
  - Se customer logado, dados preenchidos automaticamente
  - Se guest, formulário de dados obrigatório no checkout
  - QA: Carrinho com itens → botão → navega para /checkout com dados

- [ ] 18. Integrar carrinho existente (`localStorage key elshaday_utilidades_cart`) com checkout
  - Ler itens do localStorage ao carregar checkout
  - Atualizar localStorage após pedido criado
  - QA: Add item to cart → go to checkout → items appear → create order → cart clears

### Fase C3: Deploy + Verificação Final

- [ ] 19. Atualizar `AGENTS.md` do projeto com novas rotas e padrões de auth
  - Arquivo: `AGENTS.md`
  - QA: Documentação reflete todas as rotas protegidas por JWT

- [ ] 20. Push para GitHub + deploy Render automático
  - Branch: `sync/prod-data`
  - QA: `GET /v1/health` → `{"status":"ok"}`; checkout fluxo completo funciona em produção

- [ ] 21. Verificar Vercel status — confirmar morto, adicionar nota no PROJECT-HANDOFF.md
  - QA: `https://lojaonline-murex.vercel.app` → 404 ou deploy not found

---

## Final verification wave

- [ ] F1. Suíte backend completa: `venv/bin/python -m pytest -q` → 0 failures
- [ ] F2. LSP zero errors: `venv/bin/basedpyright app/ tests/ --outputjson` → 0 errors
- [ ] F3. Frontend compila: `cd ~/loja-online && npx tsc --noEmit` → 0 errors
- [ ] F4. E2E checkout guest: Playwright → product → cart → checkout → confirm → WhatsApp redirect
- [ ] F5. E2E checkout customer: register → login → checkout → confirm
- [ ] F6. Auth: admin endpoints retornam 401 sem token, 200 com token
- [ ] F7. Deploy Render health: `GET /v1/health` → `{"status":"ok"}`
- [ ] F8. Plexo atualizado: task marcada como done

---

## Must-NOT-Have

- NÃO trocar o banco SQLite por PostgreSQL no dev (só via docker-compose em prod)
- NÃO remover endpoints existentes (backward compat)
- NÃO quebrar o fluxo WhatsApp CTA existente (mantém como fallback)
- NÃO adicionar dependências externas pesadas (mantém stack leve)
- NÃO deployar sem approval explícito do usuário

---

## Dependências entre fases

```
C1 (cleanup+segurança) → C2 (frontend) → C3 (deploy)
```

C2a e C2b podem rodar em paralelo. C2c depende de C2a. C2d depende de C2b + C2c.
C3 depende de todas as fases anteriores.
