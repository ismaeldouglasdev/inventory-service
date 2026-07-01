# Template: Mapear Fotos de Alta Resolução

## Como usar

1. Abra `http://192.168.15.6:9998/` no PC da loja (desktop_photos)
2. Compare cada foto com os produtos listados abaixo
3. Preencha o mapping.json com `foto → ospos_id`

## Lista de produtos SEM foto de qualidade

Para cada produto abaixo, descubra qual foto em `desktop_photos/` corresponde:

```json
{
  "fotos_da_loja_IMG_20220820_184056035_HDR.jpg": 182,
  "fotos_da_loja_IMG_20220820_184118634_HDR.jpg": 239,
  "fotos_da_loja_IMG_20220820_184125188_HDR.jpg": 262
}
```

## Para enviar o mapping

```bash
# Salvar como photo_mapping.json
nano photo_mapping.json

# Enviar para a API do Sisyphus (quando HTTP funcionar)
curl -s -X POST http://192.168.15.41:8000/v1/admin/images/map \
  -H 'Content-Type: application/json' \
  -d '{"photo_filename": "fotos_da_loja_IMG_20220820_184056035_HDR.jpg", "item_id": 182}'

# Ou subir no Git
cp photo_mapping.json /caminho/inventory-service/
cd /caminho/inventory-service
git add photo_mapping.json
git commit -m "mapping: fotos HQ para produtos"
git push origin main
```
