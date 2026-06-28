# 🚀 Produção — Passo a Passo

Checklist completo para colocar o inventory-service + loja-online em operação.

---

## Índice

- [Fase 1: Infraestrutura](#fase-1-infraestrutura)
- [Fase 2: Conectar OSPOS](#fase-2-conectar-ospos)
- [Fase 3: Configurar Adaptadores](#fase-3-configurar-adaptadores)
- [Fase 4: Loja Online](#fase-4-loja-online)
- [Fase 5: Scanner + WebSocket](#fase-5-scanner--websocket)
- [Fase 6: AI Onboarding](#fase-6-ai-onboarding)
- [Fase 7: Automação](#fase-7-automação)
- [Fase 8: Deploy Final](#fase-8-deploy-final)

---

## Fase 1: Infraestrutura

### 1.1 Escolher o servidor

| Opção | Custo | Quando usar |
|-------|-------|-------------|
| **PC da Loja (local)** | Grátis | Já tem o OSPOS rodando, ideal para começar |
| **VPS (DigitalOcean, etc.)** | ~$6-12/mês | Precisa de 24/7, loja online pública |
| **Docker no PC da Loja** | Grátis | Já tem Docker, fácil de gerenciar |

### 1.2 Instalar dependências (servidor)

```bash
# Python 3.12+
python3 --version

# Git
git --version

# (Opcional) Docker + Docker Compose
docker --version
docker compose version
```

### 1.3 Clonar os repositórios

```bash
cd ~
git clone https://github.com/ismaeldouglasdev/inventory-service.git
git clone https://github.com/ismaeldouglasdev/loja-online.git
```

### 1.4 Configurar inventory-service

```bash
cd ~/inventory-service
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,images]"

# Configurar ambiente
cp .env.example .env
nano .env  # Preencher conforme fases abaixo
```

### 1.5 Inicializar banco

```bash
cd ~/inventory-service
source .venv/bin/activate
alembic upgrade head
# → Cria todas as tabelas: event_store, store_products,
#   inventory_state, processed_actions, channel_state,
#   onboarding_sessions, onboarding_images, event_store_archive
```

### 1.6 Testar se sobe

```bash
cd ~/inventory-service
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Em outro terminal:
curl http://localhost:8000/v1/health
# → {"status":"ok","version":"0.1.0","database":"connected","adapters":[]}
```

---

## Fase 2: Conectar OSPOS

### 2.1 Localizar o MySQL do OSPOS

No PC da loja, o OSPOS roda MySQL. Descubra as credenciais:

```bash
# Geralmente em:
cat /var/www/html/ospos/application/config/database.php
# ou no docker-compose.yml do OSPOS
```

### 2.2 Preencher .env

```env
# No ~/inventory-service/.env
OSPOS_DB_HOST=127.0.0.1
OSPOS_DB_PORT=3306
OSPOS_DB_NAME=ospos
OSPOS_DB_USER=admin
OSPOS_DB_PASS=pointofsale
```

### 2.3 Testar conexão

O CDC Agent já tenta conectar automaticamente na inicialização. Para testar manualmente:

```bash
# Ver logs do servidor:
# Se aparecer "CDC Agent started" sem "MySQL fetch failed" → conectou
```

### 2.4 Fazer sync manual

```bash
# Sync completo (traz todos os produtos)
curl -X POST "http://localhost:8000/v1/store/sync?mode=full"

# Verificar:
curl http://localhost:8000/v1/store/products
# → Deve mostrar os produtos do OSPOS
```

### 2.5 Resolução de problemas

| Erro | Causa | Solução |
|------|-------|---------|
| `Access denied for user` | Credenciais erradas | Verificar `database.php` do OSPOS |
| `Can't connect to MySQL` | MySQL não está rodando | `systemctl restart mysql` |
| `Unknown database 'ospos'` | Nome do banco errado | `SHOW DATABASES;` no MySQL |
| 502 no sync | Timeout de conexão | Aumentar `timeout` no `store_sync.py` |

---

## Fase 3: Configurar Adaptadores

### 3.1 WooCommerce

**Pré-requisitos:** Loja WooCommerce criada e instalada.

```bash
# No .env:
WOOD_COMMERCE_URL=https://sualoja.com
WOOD_COMMERCE_CONSUMER_KEY=ck_...
WOOD_COMMERCE_CONSUMER_SECRET=cs_...
```

**Onde obter:**
1. WooCommerce Admin → Settings → Advanced → REST API
2. "Add Key" → Read/Write → gerar Consumer Key + Secret

**Testar:**
```bash
curl http://localhost:8000/v1/woocommerce/status
```

### 3.2 Mercado Livre

**Pré-requisitos:** App criado no [DevCenter do ML](https://developers.mercadolivre.com.br/).

```bash
# No .env:
ML_CLIENT_ID=3439612454641866
ML_CLIENT_SECRET=YQ3GOSYnvQsY...
ML_REDIRECT_URI=https://seudominio.com/v1/mercadolivre/callback
```

**Fluxo OAuth:**
```bash
# 1. Obter URL de autorização
curl http://localhost:8000/v1/mercadolivre/auth-url
# → Abrir URL no navegador, autorizar app

# 2. ML redireciona para /callback com ?code=...
# O token é automaticamente armazenado no .env

# 3. Verificar status
curl http://localhost:8000/v1/mercadolivre/status
```

**Para deploy com SSL (obrigatório ML):**

| Método | Como fazer |
|--------|-----------|
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8000` |
| **ngrok** | `ngrok http 8000` |
| **Nginx + Let's Encrypt** | Proxy reverso com certbot |

Atualizar `ML_REDIRECT_URI` para a URL pública do tunnel/nginx.

### 3.3 Shopee

**Pré-requisitos:** Conta de desenvolvedor na [Shopee Open Platform](https://open.shopee.com/).

```bash
# No .env:
SHOPEE_PARTNER_ID=123456
SHOPEE_API_KEY=sua_api_key_aqui
SHOPEE_REDIRECT_URI=https://seudominio.com/v1/shopee/callback
SHOPEE_SANDBOX=true  # → false em produção
```

**Fluxo OAuth:**
```bash
# 1. URL de autorização
curl http://localhost:8000/v1/shopee/auth-url
# → Abrir no navegador

# 2. Shopee redireciona para /callback?code=...&shop_id=...
#    O token é automaticamente armazenado

# 3. Verificar
curl http://localhost:8000/v1/shopee/status
```

**⚠️ Shopee exige HTTPS para callback.** Use Cloudflare Tunnel ou ngrok.

### 3.4 Verificar todos os adapters

```bash
curl http://localhost:8000/v1/health
# → {"adapters":["woocommerce","mercadolivre","shopee"]}
```

---

## Fase 4: Loja Online

### 4.1 Build do frontend

```bash
cd ~/loja-online
npm install
npm run build
# → dist/ gerado
```

### 4.2 Servir com Nginx

```nginx
# /etc/nginx/sites-available/loja
server {
    listen 80;
    server_name sualoja.com;

    # Frontend estático
    root /home/ismaeldev/loja-online/dist;
    index index.html;

    # API proxy
    location /v1/ {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;  # WebSocket
    }

    # Fallback SPA
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/loja /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
```

### 4.3 HTTPS com Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d sualoja.com
```

### 4.4 Configurar número do WhatsApp

Editar os arquivos no frontend:

- `src/components/WhatsAppButton.tsx` → linha 1: `WHATSAPP_NUMBER`
- `src/pages/Checkout.tsx` → linha 7: `WHATSAPP_NUMBER`

Formato: `5511999999999` (código país + DDD + número, sem + ou espaços).

```bash
cd ~/loja-online
nano src/components/WhatsAppButton.tsx
nano src/pages/Checkout.tsx
npm run build
```

### 4.5 Testar

```bash
# Frontend
curl http://localhost
# → Deve mostrar a página da loja

# API via proxy
curl http://localhost/v1/health
# → Deve responder
```

---

## Fase 5: Scanner + WebSocket

### 5.1 Script do scanner

No **PC da loja** (onde o scanner está conectado), criar:

```bash
sudo nano /usr/local/bin/scan-to-store.sh
```

```bash
#!/bin/bash
# Scanner → Inventory Service
# O scanner envia código de barras + ENTER como se fosse teclado

BARCODE="${1:-$(cat)}"
SERVER_URL="http://SERVIDOR_DO_INVENTORY:8000"

curl -s -X POST "$SERVER_URL/v1/store/scan/$BARCODE"
```

```bash
sudo chmod +x /usr/local/bin/scan-to-store.sh
```

### 5.2 Integrar com OSPOS (opção 1 — plugin)

Criar um hook no OSPOS que chama o scan endpoint quando um item é adicionado à venda:

```php
// Em application/hooks/ScanHook.php
// Chamado após add_item() no OSPOS
$barcode = $this->cart->get_item($item_id)->item_number;
$ch = curl_init();
curl_setopt($ch, CURLOPT_URL, "http://SERVIDOR:8000/v1/store/scan/$barcode");
curl_setopt($ch, CURLOPT_POST, 1);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_exec($ch);
curl_close($ch);
```

### 5.3 Integrar com scanner USB (opção 2 — script Python)

Se o scanner se comporta como teclado (input focus):

```python
#!/usr/bin/env python3
"""Escuta input do scanner e envia para inventory-service."""
import sys
import requests
import subprocess

# O scanner envia ENTER após ler. Capturamos via xinput/test.
# Alternativa: usar `evtest` para capturar eventos do teclado.
```

### 5.4 Testar o fluxo completo

```bash
# Simular scan
curl -X POST "http://localhost:8000/v1/store/scan/7891234567890"

# Ver no WebSocket (Capture.tsx aberto no celular)
# → Deve aparecer o produto na tela de captura
```

---

## Fase 6: AI Onboarding

### 6.1 Subir o 9Router

```bash
# Verificar se o 9Router está rodando
curl http://localhost:20128/api/health

# Se não estiver, subir:
# (instruções específicas do 9Router)
```

### 6.2 Configurar .env

```env
AI_API_URL=http://localhost:20128
AI_API_KEY=sk-sua-chave
AI_MODEL=gpt-4o  # ou outro modelo de visão
AI_MAX_IMAGES=4
```

### 6.3 Testar classificação

```bash
# 1. Criar sessão
curl -X POST "http://localhost:8000/v1/onboarding/session?sku=ABC-123"

# 2. Upload de imagem
curl -X POST "http://localhost:8000/v1/onboarding/session/1/images" \
  -F "files=@/caminho/para/foto.jpg"

# 3. Analisar
curl -X POST "http://localhost:8000/v1/onboarding/session/1/analyze"
# → Retorna JSON com categoria, marca, nome sugerido, etc.

# 4. Aplicar ao produto
curl -X POST "http://localhost:8000/v1/onboarding/session/1/apply"
```

### 6.4 Resolução de problemas

| Problema | Causa | Solução |
|----------|-------|---------|
| `AI analysis failed` | 9Router offline | `curl http://localhost:20128/api/health` |
| Modelo não encontrado | Modelo não suportado | Testar com `gpt-4o` ou `claude-3-sonnet` |
| Imagem muito grande | >5MB | Redimensionar antes de enviar |

---

## Fase 7: Automação

### 7.1 Systemd — inventory-service

```ini
# /etc/systemd/system/inventory.service
[Unit]
Description=Inventory Service
After=network.target mysql.service

[Service]
Type=simple
User=ismaeldev
WorkingDirectory=/home/ismaeldev/inventory-service
ExecStart=/home/ismaeldev/inventory-service/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/home/ismaeldev/inventory-service/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inventory
sudo systemctl status inventory
```

### 7.2 Cron — Sync automático

```bash
crontab -e
```

```cron
# Sync do OSPOS a cada 30 minutos
*/30 * * * * curl -X POST http://localhost:8000/v1/store/sync?mode=delta

# Archive de eventos toda madrugada (03:00)
0 3 * * * curl -X POST http://localhost:8000/v1/archive/run  # se endpoint existir

# Health check a cada 5 minutos (opcional, monitoramento)
*/5 * * * * curl -f http://localhost:8000/v1/health > /dev/null 2>&1 || systemctl restart inventory
```

### 7.3 Systemd — Frontend (se não usar Nginx)

```ini
# /etc/systemd/system/loja-online.service
[Unit]
Description=Loja Online Frontend
After=network.target

[Service]
Type=simple
User=ismaeldev
WorkingDirectory=/home/ismaeldev/loja-online
ExecStart=/usr/bin/npm run preview -- --host 0.0.0.0 --port 5173
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Fase 8: Deploy Final

### 8.1 Checklist de produção

- [ ] **OSPOS conectado** — sync funciona, produtos aparecem em `/v1/store/products`
- [ ] **CDC Agent rodando** — logs mostram polls sem erro
- [ ] **WooCommerce configurado** — stock/price sync funcionando
- [ ] **Mercado Livre conectado** — OAuth completo, token válido
- [ ] **Shopee conectado** — OAuth completo, sandbox→produção
- [ ] **HTTPS configurado** — Certbot/Cloudflare Tunnel
- [ ] **WebSocket funcional** — Scanner → Celular
- [ ] **AI Onboarding testado** — Classificação de produto por imagem
- [ ] **Systemd ativo** — Serviço sobe sozinho após reboot
- [ ] **Cron configurado** — Sync automático
- [ ] **Backup do banco** — `data/inventory.db` ou PostgreSQL
- [ ] **Monitoramento** — health check periódico
- [ ] **90 testes passando** — `pytest -v`

### 8.2 Comandos rápidos

```bash
# Status geral
curl http://localhost:8000/v1/health

# Logs
journalctl -u inventory -f --no-hostname

# Sync manual completo
curl -X POST "http://localhost:8000/v1/store/sync?mode=full"

# Ver produtos na loja
curl http://localhost:8000/v1/store/products | python3 -m json.tool

# Ver eventos recentes
curl http://localhost:8000/v1/products/events | python3 -m json.tool

# Ver arquivo de log
tail -f /var/log/syslog | grep inventory
```

### 8.3 Manutenção

```bash
# Atualizar código
cd ~/inventory-service && git pull
source .venv/bin/activate && pip install -e ".[dev,images]"
alembic upgrade head
sudo systemctl restart inventory

cd ~/loja-online && git pull
npm install && npm run build
sudo systemctl restart nginx
```

### 8.4 Rollback

```bash
# Se algo der errado:
cd ~/inventory-service
git log --oneline -10
git checkout <commit-anterior>
sudo systemctl restart inventory

# Reverter migration (se necessário):
alembic downgrade -1
```
