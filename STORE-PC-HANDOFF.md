# Store PC Setup — Elshaday Utilidades

## What was built (dev PC)

Admin panel for managing the store. Runs locally at `localhost:8080/admin`.

- **Product list** with search, filters (photo, visibility, category), pagination
- **Toggle visibility** — show/hide products from the store
- **Image editor** — rotate, crop, AI inpaint (remove price tags)
- **Product edit** — name, price, stock, description, category, visibility

Auth: password stored in `ADMIN_PASSWORD` env var (default: `admin123`).

## Sync approach: Git

Both PCs share the same repos via GitHub:

```
inventory-service (branch: sync/prod-data) → backend + SQLite DB + images
loja-online (branch: master)               → frontend
```

### Workflow

1. **Dev PC**: make changes in admin panel → edits saved to SQLite + `data/images/`
2. **Git sync**: commit + push changes from dev PC
3. **Store PC**: pull latest → restart inventory-service → changes are live

### What to sync

After pulling, the store PC needs these files updated:

- `data/inventory.db` — SQLite database with product edits (visibility, name, price, etc.)
- `data/images/*` — cropped/rotated/inpainted images

### Store PC commands

```bash
# Pull latest
cd ~/inventory-service  # adjust path
git pull origin sync/prod-data

# If DB or images changed, restart the service
systemctl --user restart inventory-service
# or
pkill -f "uvicorn app.main:app"
cd ~/inventory-service && source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
```

### Important notes

- The **SQLite DB is the source of truth** for the online store
- OSPOS uses MySQL separately — changes here do NOT automatically go back to OSPOS
- If you need to sync visibility changes back to OSPOS, you'll need to export from admin and import manually
- Images are served by inventory-service at `/v1/store/images/{filename}`
- The frontend reads from the same API, so changes are visible immediately

## Environment variables

```
ADMIN_PASSWORD=admin123    # Admin panel password
CORS_ORIGINS=["*"]         # Allow all origins
LOG_LEVEL=info
CDC_ENABLED=false
```

## Backend API endpoints (admin)

| Method | Path | Description |
|--------|------|-------------|
| GET | /v1/admin/stats | Dashboard stats |
| GET | /v1/admin/products | List products (search, filter, sort) |
| GET | /v1/admin/products/:id | Get product detail |
| PUT | /v1/admin/products/:id | Update product |
| POST | /v1/admin/products/:id/image/rotate | Rotate image |
| POST | /v1/admin/products/:id/image/crop | Crop image |
| POST | /v1/admin/products/:id/image/inpaint | AI inpaint |
| POST | /v1/admin/products/:id/image/restore | Restore original image |
| GET | /v1/admin/images/map | Image filename mapping |
| GET | /v1/admin/health | Health check |
| GET | /v1/admin/metrics | Request metrics |
