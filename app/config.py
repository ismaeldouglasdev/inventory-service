"""Application configuration via Pydantic Settings."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────
    # Default: SQLite for local dev. Swap to postgres+asyncpg for prod.
    database_url: str = "sqlite+aiosqlite:///./data/inventory.db"

    # ── FastAPI ────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = '["https://loja-online-kmg8.onrender.com","https://lojaonline-murex.vercel.app","http://localhost:5173","http://localhost:3000","http://localhost:8080","http://localhost","http://127.0.0.1","http://192.168.15.6"]'
    log_level: str = "INFO"
    log_format: str = "text"  # "json" or "text"

    # ── Observability ──────────────────────────────────────────────────
    metrics_enabled: bool = True
    metrics_host: str = ""  # separate metrics server host (optional)
    metrics_port: int = 8001  # separate metrics server port
    sentry_dsn: str = ""

    # ── WooCommerce ────────────────────────────────────────────────────
    woocommerce_url: str = ""
    woocommerce_consumer_key: str = ""
    woocommerce_consumer_secret: str = ""

    # ── Mercado Livre ─────────────────────────────────────────────────
    ml_client_id: str = ""
    ml_client_secret: str = ""
    ml_redirect_uri: str = "http://localhost:8000/v1/mercadolivre/callback"
    ml_access_token: str = ""
    ml_refresh_token: str = ""
    ml_user_id: int = 0
    ml_default_category: str = "MLB271793"  # Ferramentas (fallback)

    # ML price = base × markup. Base "pdv" (preço loja) ou "cost" (custo).
    # Regra do usuário: SEMPRE acima do PDV, senão as taxas do ML comem o lucro.
    ml_price_base: str = "pdv"
    ml_price_markup: float = 1.4
    ml_price_round: str = "99"  # "99"|"90"|"none"
    ml_premium_min_price: float = 30.0  # >= este valor → gold_pro (Premium)
    ml_shipping_mode: str = "normal"  # "normal"|"free"

    # ── Shopee ──────────────────────────────────────────────────────────
    shopee_partner_id: int = 0
    shopee_api_key: str = ""
    shopee_redirect_uri: str = "http://localhost:8000/v1/shopee/callback"
    shopee_sandbox: bool = True
    shopee_access_token: str = ""
    shopee_refresh_token: str = ""
    shopee_shop_id: int = 0

    # ── OSPOS ──────────────────────────────────────────────────────────
    ospos_db_host: str = "localhost"
    ospos_db_port: int = 3306
    ospos_db_name: str = "ospos"
    ospos_db_user: str = "root"
    ospos_db_pass: str = ""
    ospos_api_url: str = ""  # REST fallback (optional)
    ospos_uploads_dir: str = "/var/www/html/pos/public/uploads/item_pics"

    # ── LaMa (inpainting local via ONNX — leve) ───────────────────────
    lama_model_path: str = "/home/ismael/lama-onnx/lama_fp32.onnx"
    lama_threads: int = 2          # threads de inferência (i3-3220T 2C/4T)

    # ── Cloudflare R2 (image storage) ────────────────────────────────
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_public_url: str = ""  # auto-generated if empty
    r2_s3_endpoint: str = ""  # e.g. https://<account_id>.r2.cloudflarestorage.com

    # ── R2 Free-Tier Hard Guardrails ────────────────────────────────
    # Free tier: 10 GB storage, 1M Class A ops, 10M Class B ops / month
    # Hard caps at ~90% to prevent accidental overage
    r2_max_storage_bytes: int = 9_663_676_416  # 9.0 GiB in bytes
    r2_max_class_a_ops: int = 900_000  # writes/deletes per month
    r2_max_class_b_ops: int = 9_000_000  # reads per month

    # ── AI / LLM ────────────────────────────────────────────────────────
    ai_api_url: str = ""
    ai_api_key: str = ""
    ai_model: str = "gpt-4o"
    ai_max_images: int = 4

    # ── Gemini (leitura de notas fiscais via visão) ─────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_max_images: int = 6

    # ── Security ───────────────────────────────────────────────────────
    api_key: str = ""  # If set, requires X-API-Key header on sensitive endpoints
    admin_password: str = ""  # Painel admin (/v1/admin/auth/login) — DEVE ser definida via env
    jwt_secret: str = ""  # Segredo HS256 dos tokens do admin (obrigatório em produção)
    rate_limit_store: int = 60   # requests/min for store endpoints
    rate_limit_write: int = 10   # requests/min for write endpoints
    rate_limit_admin: int = 20   # requests/min for admin endpoints

    # ── Push Sync (push local data to remote Render instance) ────────
    push_sync_url: str = ""  # e.g. https://loja-online-kmg8.onrender.com
    push_sync_api_key: str = ""  # falls back to api_key if empty

    # ── CDC Agent ─────────────────────────────────────────────────────
    cdc_enabled: bool = True
    cdc_poll_interval: int = 30  # seconds

    # ── Convenience properties ─────────────────────────────────────────
    @property
    def cors_origins_list(self) -> list[str]:
        return json.loads(self.cors_origins)

    @property
    def database_url_async(self) -> str:
        """Return the DATABASE_URL suitable for async drivers.

        SQLAlchemy async drivers need ``sqlite+aiosqlite://`` or
        ``postgresql+asyncpg://``.  If the user-provided URL already
        carries a ``+`` suffix it is returned as-is.
        """
        url = self.database_url
        if "+" in url.split("://")[0]:
            return url
        # Auto-pick a default async driver
        if url.startswith("sqlite"):
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if url.startswith("postgresql"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def database_url_sync(self) -> str:
        """Return a sync-compatible URL for Alembic and scripts."""
        return self.database_url


# Module-level singleton
settings = Settings()

# Ensure data directory exists (for SQLite)
_data_dir = Path(__file__).resolve().parent.parent / "data"
_data_dir.mkdir(parents=True, exist_ok=True)
