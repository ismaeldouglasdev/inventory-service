# 🤝 Ponte entre Agentes — PC da Loja ↔ Sisyphus

## Visão Geral

Sua máquina (`192.168.15.6`, lubuntu-pc) pode se comunicar com o Sisyphus
(`192.168.15.41`) via HTTP. A ponte permite:

- Enviar/ler instruções entre agentes
- Fazer upload de imagens dos produtos (NEX → inventory-service)
- Sincronizar produtos do OSPOS
- Verificar status da loja

---

## 🔧 Configuração no PC da Loja

### 1. Testar Conexão

Abra o terminal no PC da loja e teste se alcança o Sisyphus:

```bash
# Teste básico
curl -s http://192.168.15.41:8000/v1/agent/ping

# Deve retornar: {"ok":true,"agent":"sisyphus-bridge","time":...}
```

### 2. Configurar o OpenCode para usar a ponte

Crie o arquivo de conexão:

```bash
mkdir -p ~/.opencode/peers
cat > ~/.opencode/peers/sisyphus.json << 'EOF'
{
  "name": "sisyphus",
  "url": "http://192.168.15.41:8000/v1/agent",
  "type": "agent-bridge",
  "version": "1.0"
}
EOF
```

---

## 📡 Endpoints da Ponte

| Método | Rota | Pra quê |
|--------|------|---------|
| `GET` | `/v1/agent/ping` | Testar se a ponte está no ar |
| `GET` | `/v1/agent/pending?target=store-agent` | Ler instruções pendentes pra você |
| `POST` | `/v1/agent/send` | Enviar mensagem pro Sisyphus |
| `POST` | `/v1/agent/status` | Reportar seu status |
| `GET` | `/v1/agent/status` | Ver status dos dois lados |

---

## 📋 Fluxo de Trabalho

### Quando você (agente no PC da loja) iniciar uma sessão:

```bash
# 1. Avisar Sisyphus que está online
curl -s -X POST http://192.168.15.41:8000/v1/agent/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"sisyphus","type":"status","body":"store-agent online"}'

# 2. Ver se tem instruções pendentes
curl -s "http://192.168.15.41:8000/v1/agent/pending?target=store-agent&limit=5"

# 3. Reportar status
curl -s -X POST http://192.168.15.41:8000/v1/agent/status \
  -H 'Content-Type: application/json' \
  -d '{"status":"online","agent":"store-agent"}'
```

### Para fazer sync dos produtos:

```bash
# Já funciona! Chama direto a API
curl -s -X POST "http://192.168.15.41:8000/v1/store/sync?mode=full"
```

### Para enviar fotos dos produtos (NEX → loja):

```bash
# Upload de imagem pra um produto específico (product_id = 1)
curl -s -X POST "http://192.168.15.41:8000/v1/store/products/1/image" \
  -F "file=@/caminho/da/foto.jpg"
```

### Para enviar várias fotos de uma vez (script):

```bash
#!/bin/bash
# Envia todas as imagens de uma pasta
for img in /caminho/das/imagens/nex/*.jpg; do
  # Extrai o código do nome do arquivo (ex: "12345.jpg" → sku=12345)
  sku=$(basename "$img" .jpg)
  
  # Primeiro descobre qual product_id corresponde ao SKU
  prod_id=$(curl -s "http://192.168.15.41:8000/v1/store/products?search=$sku" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['products'][0]['id'] if d['products'] else '')" 2>/dev/null)
  
  if [ -n "$prod_id" ]; then
    curl -s -X POST "http://192.168.15.41:8000/v1/store/products/$prod_id/image" \
      -F "file=@$img" -F "remove_bg=false"
    echo "Upload: $img → product #$prod_id"
  else
    echo "SKU $sku não encontrado na loja"
  fi
done
```

---

## 🔄 Ciclo de Comunicação Típico

```
  Sisyphus (dev)                    PC da Loja (store)
       │                                  │
       │── POST /agent/send {"to":        │
       │   "store-agent",                 │
       │   "body": "Encontre as fotos     │
       │   do NEX em C:\NEX\..."}         │
       │                                  │── GET /agent/pending?target=store-agent
       │                                  │── Lê instrução
       │                                  │── Localiza as imagens
       │                                  │── POST /agent/send {"to":"sisyphus",
       │                                  │     "body":"Fotos encontradas, 
       │                                  │      começando upload..."}
       │── GET /agent/pending             │
       │── Lê resposta                    │
       │                                  │── POST /store/products/{id}/image
       │                                  │   (upload de cada foto)
```

---

## ✅ Verificação Rápida

Depois de configurar, rode no PC da loja:

```bash
echo "=== 1. Ping ==="
curl -s http://192.168.15.41:8000/v1/agent/ping

echo -e "\n=== 2. Status ==="
curl -s http://192.168.15.41:8000/v1/agent/status

echo -e "\n=== 3. Produtos na loja ==="
curl -s "http://192.168.15.41:8000/v1/store/products" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'{d[\"total\"]} produtos visíveis')
for p in d['products']:
    print(f'  #{p[\"id\"]} {p[\"name\"]} - R\${p[\"price\"]} (estoque: {p[\"stock\"]})')
"
```
