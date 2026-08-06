"""FastAPI application entry-point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.utils.metrics import (
    requests_total,
    request_duration,
    requests_in_flight,
)

from app.adapters.implementations.woocommerce import WooCommerceAdapter
from app.adapters.implementations.mercadolivre import MercadoLivreAdapter
from app.adapters.implementations.shopee import ShopeeAdapter
from app.adapters.registry import AdapterRegistry
from app.api.v1.health import (
    _set_registry as _set_health_registry,
    _set_circuit_breaker as _set_health_cb,
    _set_start_time,
)
from app.api.v1.health import router as health_router
from app.api.v1.products import _set_cdc_agent
from app.api.v1.products import router as products_router
from app.api.v1.mercadolivre import _set_registry as _set_ml_registry
from app.api.v1.mercadolivre import router as mercadolivre_router
from app.api.v1.woocommerce import _set_registry as _set_wc_registry
from app.api.v1.woocommerce import router as woocommerce_router
from app.api.v1.shopee import _set_registry as _set_shopee_registry
from app.api.v1.shopee import router as shopee_router
from app.api.v1.store import _set_store_sync as _set_store_sync_ref
from app.api.v1.store import router as store_router
from app.api.v1.sell import _set_registry as _set_sell_registry
from app.api.v1.sell import _set_circuit_breaker as _set_sell_cb
from app.api.v1.sell import router as sell_router
from app.api.v1.admin import (
    _set_registry as _set_admin_registry,
    _set_circuit_breaker as _set_admin_cb,
)
from app.api.v1.admin import router as admin_router
from app.api.v1.onboarding import router as onboarding_router
from app.api.v1.agent_bridge import router as agent_bridge_router
from app.config import settings
from app.services.cdc_agent import CDCAgent
# from app.services.event_processor import EventStoreProcessor
from app.services.store_sync import StoreSync
from app.services.circuit_breaker import CircuitBreaker
from app.utils.logging import setup_logging
from app.utils.metrics import generate_metrics

# ── Logging ────────────────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger(__name__)


# ── Globals ──────────────────────────────────────────────────────────────
registry = AdapterRegistry()
cdc_agent = CDCAgent(poll_interval=settings.cdc_poll_interval)
# event_processor = EventStoreProcessor(registry, poll_interval=5.0)
circuit_breaker = CircuitBreaker()


# ── Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting Inventory Service v0.1.0")

    _set_start_time()

    # ── Adapter registry ────────────────────────────────────────────
    if settings.woocommerce_url and settings.woocommerce_consumer_key:
        wc_adapter = WooCommerceAdapter()
        registry.register(wc_adapter)
        logger.info("WooCommerce adapter registered")
    else:
        logger.info("WooCommerce adapter skipped — WOOCOMMERCE_URL or WOOCOMMERCE_CONSUMER_KEY not set")

    if settings.shopee_partner_id and settings.shopee_api_key:
        shopee_adapter = ShopeeAdapter()
        registry.register(shopee_adapter)
        logger.info("Shopee adapter registered")
    else:
        logger.info("Shopee adapter skipped — SHOPEE_PARTNER_ID or SHOPEE_API_KEY not set")

    if settings.ml_client_id and settings.ml_client_secret:
        ml_adapter = MercadoLivreAdapter()
        registry.register(ml_adapter)
        logger.info("Mercado Livre adapter registered")
    else:
        logger.info("Mercado Livre adapter skipped — ML_CLIENT_ID or ML_CLIENT_SECRET not set")

    # ── Inject registry + CB into route modules ─────────────────────
    _set_health_registry(registry)
    _set_health_cb(circuit_breaker)
    _set_admin_registry(registry)
    _set_admin_cb(circuit_breaker)
    _set_ml_registry(registry)
    _set_wc_registry(registry)
    _set_shopee_registry(registry)
    _set_sell_registry(registry)
    _set_sell_cb(circuit_breaker)

    # ── Start CDC Agent ─────────────────────────────────────────────
    _set_cdc_agent(cdc_agent)
    if settings.cdc_enabled:
        cdc_task = asyncio.create_task(cdc_agent.run_forever())
        logger.info("CDC Agent started (poll every %ds)", settings.cdc_poll_interval)
    else:
        cdc_task = None
        logger.info("CDC Agent disabled (CDC_ENABLED=false)")

    # ── Store Sync ─────────────────────────────────────────────────
    store_sync = StoreSync()
    _set_store_sync_ref(store_sync)
    logger.info("StoreSync service registered")

    processor_task = None
    logger.info("EventStore Processor disabled")

    yield

    # ── Shutdown ────────────────────────────────────────────────────
    logger.info("Shutting down Inventory Service")

    if cdc_task is not None:
        cdc_agent.stop()
        cdc_task.cancel()
        try:
            await cdc_task
        except asyncio.CancelledError:
            pass

    if processor_task is not None:
        processor_task.cancel()
        try:
            await processor_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutdown complete")


# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Inventory Service",
    description="Omnichannel adapter bridging OSPOS with WooCommerce, Shopee, ML. With observability, rate limiting, and health tracking.",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Path normalization ─────────────────────────────────────────────────
_PATH_PATTERNS = [
    (re.compile(r"^/v1/store/images/product_\d+\.[a-z]+$"), "/v1/store/images/{id}"),
    (re.compile(r"^/v1/store/products/\d+$"), "/v1/store/products/{id}"),
    (re.compile(r"^/v1/admin/products/\d+$"), "/v1/admin/products/{id}"),
]


def _normalize_path(path: str) -> str:
    for pattern, replacement in _PATH_PATTERNS:
        if pattern.match(path):
            return replacement
    return path


# ── ASGI Metrics middleware ─────────────────────────────────────────────
from starlette.types import ASGIApp, Scope, Receive, Send


class MetricsASGIMiddleware:
    """ASGI middleware that records request metrics (count, duration, in-flight)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = _normalize_path(scope.get("path", "/unknown"))
        start = time.time()
        status = [500]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.time() - start
            requests_total.labels(method=method, endpoint=path, status=str(status[0])).inc()
            request_duration.labels(method=method, endpoint=path).observe(duration)


app.add_middleware(MetricsASGIMiddleware)

# ── Metrics endpoint ────────────────────────────────────────────────────
@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=True)
async def metrics_endpoint() -> PlainTextResponse:
    """Prometheus metrics endpoint — scraped by Prometheus or checked manually."""
    return PlainTextResponse(
        content=generate_metrics().decode("utf-8"),
        media_type="text/plain; charset=utf-8",
    )


# ── Routers ─────────────────────────────────────────────────────────────
app.include_router(health_router, prefix="/v1")
app.include_router(admin_router, prefix="/v1")
app.include_router(products_router, prefix="/v1")
app.include_router(mercadolivre_router, prefix="/v1")
app.include_router(woocommerce_router, prefix="/v1")
app.include_router(shopee_router, prefix="/v1")
app.include_router(store_router, prefix="/v1")
app.include_router(sell_router, prefix="/v1")
app.include_router(onboarding_router, prefix="/v1")
app.include_router(agent_bridge_router, prefix="/v1")

# ── APK download ──────────────────────────────────────────────────────────
APK_PATH = Path(__file__).resolve().parent.parent / "static" / "app-debug.apk"


@app.get("/app-debug.apk", include_in_schema=False)
async def download_apk():
    if APK_PATH.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(APK_PATH), media_type="application/vnd.android.package-archive", filename="app-debug.apk")
    return PlainTextResponse("APK not found", status_code=404)


# ── Static files (mobile status page, etc.) ───────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
