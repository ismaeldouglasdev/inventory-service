<p align="center">
  <a href="README.md">🇧🇷 Português</a> &nbsp;|&nbsp; <strong>🇺🇸 English</strong>
</p>

# Inventory Service — Omnichannel Adapter

Bridge between **OSPOS** (local POS) and digital marketplaces (Shopee, Mercado Livre, WooCommerce), with an online store, sales pipeline, and AI-powered onboarding.

**Status:** Production-ready — 4 phases implemented, 75 tests passing.

---

## Overview

This service syncs inventory between a local OSPOS database and multiple e-commerce channels. It provides a REST API for managing products, orders, and inventory across all connected platforms.

### Core Capabilities

- **Inventory Sync** — Real-time stock updates across OSPOS, Shopee, and Mercado Livre
- **Order Management** — Unified order pipeline from all channels
- **Product Mapping** — Match OSPOS products to marketplace listings
- **AI Onboarding** — Intelligent product categorization and enrichment
- **Webhook System** — Event-driven updates for connected services

### Architecture

```
OSPOS (Local POS) ←→ Inventory Service ←→ Marketplaces (Shopee, ML, WooCommerce)
                          ↕
                    PostgreSQL (unified state)
                          ↕
                    REST API ←→ Online Store
```

---

## Tech Stack

- **Python 3.14+** — Core language
- **FastAPI** — REST API framework
- **SQLAlchemy + Alembic** — ORM and migrations
- **PostgreSQL** — Database
- **Docker** — Containerization
- **Pytest** — Testing (75+ tests)

---

## Quick Start

```bash
# Clone
git clone https://github.com/ismaeldouglasdev/inventory-service.git
cd inventory-service

# Configure environment
cp .env.example .env

# Start with Docker
docker compose up -d

# Or run locally
python -m venv .venv
source .venv/bin/activate
pip install -e .
python start.py
```

---

> Full documentation available in [Portuguese](README.md).
