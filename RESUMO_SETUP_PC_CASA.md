# Resumo — Setup Inventory Service + Loja Online (PC de Casa)

**Data:** 02 Ago 2025  
**Status:** Funcional localmente com dados seed (15 produtos). Sync real com OSPOS da loja fica para amanhã no PC da loja.

---

## O que foi feito

### 1. Inventory Service (FastAPI)

- **Servidor:** rodando em `http://localhost:8000` (PID ativo)
- **Banco SQLite:** `data/inventory.db` (automático)
- **MySQL Docker:** container `mysql` (mariadb:10.5) na porta 3306, DB `ospos` com 15 produtos seed
- **Dependências:** `rembg` + `Pillow` instaladas via `pip install -e ".[images]"`
- **Endpoints ativos:** store API, sync, scan, health, admin, sell, onboarding

### 2. Loja Online (React + Vite)

- **Dev server:** `http://localhost:5173` (PID ativo)
- **Repo:** https://github.com/ismaeldouglasdev/loja-online
- **Build:** TypeScript + Tailwind, sem erros

### 3. Sync OSPOS → Inventory Service

- **Manual:** `curl -X POST http://localhost:8000/v1/store/sync?mode=full` → HTTP 202 (created=1, updated=14)
- **Automático:** systemd user timer `inventory-sync.timer` a cada 30min
  - Service: `~/.config/systemd/user/inventory-sync.service`
  - Timer: `~/.config/systemd/user/inventory-sync.timer`
  - Ativado com `systemctl --user enable --now inventory-sync.timer`

### 4. Scanner Script

- Arquivo: `~/bin/scan-to-store.sh`
- Testado: `echo "12345" | ~/bin/scan-to-store.sh` → `{"barcode":"12345","product_id":null,"found:false}`
- No PC da loja: instalar em `/usr/local/bin/scan-to-store.sh`

### 5. Repos GitHub

- inventory-service: https://github.com/ismaeldouglasdev/inventory-service — pushed
- loja-online: https://github.com/ismaeldouglasdev/loja-online — pushed

---

## O que falta (PC da loja — amanhã)

| Passo | Comando/Ação |
|-------|-------------|
| 1. Confirmar MySQL do OSPOS online no PC da loja | `mysql -u admin -ppointofsale -e "SHOW DATABASES;"` |
| 2. Ajustar `.env` se IP/credenciais diferentes | Editar `OSPOS_DB_HOST`, `OSPOS_DB_USER`, `OSPOS_DB_PASS` |
| 3. Subir inventory-service | `cd ~/inventory-service && source .venv/bin/activate && uvicorn app.main:app --reload` |
| 4. Subir loja-online | `cd ~/loja-online && npm run dev` |
| 5. Sync full (puxa 10k produtos) | `curl -X POST http://localhost:8000/v1/store/sync?mode=full` |
| 6. Instalar scanner script | `sudo cp scan-to-store.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/scan-to-store.sh` |
| 7. Testar scanner físico | Ler código real → verificar `curl http://localhost:8000/v1/store/scan/last` |

---

## Arquivos-chave modificados

| Arquivo | Mudança |
|---------|---------|
| `.env` | OSPOS_DB_HOST=127.0.0.1, credenciais MySQL |
| `app/api/v1/store.py` | +31 linhas (sync, scan, image upload) |
| `app/main.py` | +33 linhas (lifespan, store sync init) |
| `app/api/v1/admin.py` | admin stats + image map endpoint |
| `app/api/v1/sell.py` | sell pipeline ajustado |
| `app/utils/metrics.py` | +27 linhas (métricas Prometheus) |
| `app/api/v1/agent_bridge.py` | +24 linhas (bridge de agente) |
| `app/config.py` | +6 settings novas |

---

## Comandos rápidos

```bash
# Subir tudo
cd ~/Desktop/code_study/MeusProjetos/inventory-service && source .venv/bin/activate && uvicorn app.main:app --reload &
cd ~/Desktop/code_study/MeusProjetos/loja-online && npm run dev &

# Sync manual
curl -X POST http://localhost:8000/v1/store/sync?mode=full

# Último scan
curl http://localhost:8000/v1/store/scan/last

# Health check
curl http://localhost:8000/v1/health

# Ver timer
systemctl --user list-timers inventory-sync.timer
```
