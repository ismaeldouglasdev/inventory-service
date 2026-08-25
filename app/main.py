"""FastAPI application entry-point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
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
from app.api.v1.estoque import router as estoque_router
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
from app.api.v1.dashboard import router as dashboard_router, start_dashboard_poller, stop_dashboard_poller
from app.api.v1.swarm import router as swarm_router
from app.api.v1.observability import router as observability_router
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

    # ── Dashboard Poller ────────────────────────────────────────────
    start_dashboard_poller()
    logger.info("Dashboard poller started")

    processor_task: asyncio.Task | None = None
    logger.info("EventStore Processor disabled")

    yield

    # ── Shutdown ────────────────────────────────────────────────────
    logger.info("Shutting down Inventory Service")

    stop_dashboard_poller()

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
app.include_router(estoque_router, prefix="/v1")
app.include_router(sell_router, prefix="/v1")
app.include_router(onboarding_router, prefix="/v1")
app.include_router(agent_bridge_router, prefix="/v1")
app.include_router(dashboard_router, prefix="/v1")
app.include_router(swarm_router, prefix="/v1")
app.include_router(observability_router, prefix="/v1")

# ── APK download ──────────────────────────────────────────────────────────
APK_PATH = Path(__file__).resolve().parent.parent / "static" / "app-debug.apk"


@app.get("/app-debug.apk", include_in_schema=False)
async def download_apk():
    if APK_PATH.exists():
        return FileResponse(str(APK_PATH), media_type="application/vnd.android.package-archive", filename="app-debug.apk")
    return PlainTextResponse("APK not found", status_code=404)


# ── SEO (robots.txt + sitemap.xml) ───────────────────────────────────────
# Registrados ANTES do catch-all da SPA para não serem sombreados.
SITE_URL = "https://loja-online-82t7.onrender.com"
SITEMAP_MAX_URLS = 500


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nDisallow: /admin\nSitemap: {SITE_URL}/sitemap.xml\n",
        media_type="text/plain; charset=utf-8",
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml() -> Response:
    """Sitemap com páginas estáticas + produtos visíveis (cap 500)."""
    from datetime import datetime

    from app.database import get_session
    from app.models.store_product import StoreProduct
    from sqlalchemy import select as sa_select

    urls: list[str] = ["/", "/search"]
    product_entries: list[tuple[int, object]] = []

    try:
        async for session in get_session():
            result = await session.execute(
                sa_select(
                    StoreProduct.id,
                    StoreProduct.updated_at,
                )
                .where(StoreProduct.store_visible == True)  # noqa: E712
                .order_by(StoreProduct.updated_at.desc())
                .limit(SITEMAP_MAX_URLS - len(urls))
            )
            product_entries = [(row[0], row[1]) for row in result.fetchall()]
            break
    except Exception as exc:
        logger.warning("sitemap: DB query failed (%s); emitting static-only sitemap", exc)

    today = time.strftime("%Y-%m-%d")
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in urls:
        body.append(f"  <url><loc>{SITE_URL}{path}</loc><lastmod>{today}</lastmod></url>")
    for pid, updated_at in product_entries:
        lastmod = today
        if isinstance(updated_at, datetime):
            lastmod = updated_at.strftime("%Y-%m-%d")
        body.append(f"  <url><loc>{SITE_URL}/produto/{pid}</loc><lastmod>{lastmod}</lastmod></url>")
    body.append("</urlset>")

    return Response(content="\n".join(body), media_type="application/xml")


# ── Static frontend (loja SPA) ──────────────────────────────────────────
# Serves the built loja frontend (static/) as a SPA. API routes under
# /v1 are registered first (above), so they take precedence. Anything else
# falls back to index.html so client-side routing works.

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """Serve static assets and SPA fallback for the loja frontend."""
    # Never shadow API routes with HTML
    if full_path.startswith("v1/") or full_path == "v1":
        raise HTTPException(status_code=404, detail="Not Found")

    requested = (STATIC_DIR / full_path).resolve()
    # Path traversal guard
    if not str(requested).startswith(str(STATIC_DIR.resolve())):
        raise HTTPException(status_code=404, detail="Not Found")

    if requested.is_file():
        return FileResponse(str(requested))

    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    raise HTTPException(status_code=404, detail="Not Found")
