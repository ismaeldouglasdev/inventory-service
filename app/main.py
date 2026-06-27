"""FastAPI application entry-point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.implementations.woocommerce import WooCommerceAdapter
from app.adapters.implementations.mercadolivre import MercadoLivreAdapter
from app.adapters.registry import AdapterRegistry
from app.api.v1.health import _set_registry as _set_health_registry
from app.api.v1.health import router as health_router
from app.api.v1.products import _set_cdc_agent
from app.api.v1.products import router as products_router
from app.api.v1.mercadolivre import _set_registry as _set_ml_registry
from app.api.v1.mercadolivre import router as mercadolivre_router
from app.api.v1.woocommerce import _set_registry as _set_wc_registry
from app.api.v1.woocommerce import router as woocommerce_router
from app.api.v1.store import _set_store_sync as _set_store_sync_ref
from app.api.v1.store import router as store_router
from app.api.v1.sell import _set_registry as _set_sell_registry
from app.api.v1.sell import _set_circuit_breaker as _set_sell_cb
from app.api.v1.sell import router as sell_router
from app.config import settings
from app.services.cdc_agent import CDCAgent
from app.services.event_processor import EventStoreProcessor
from app.services.store_sync import StoreSync
from app.services.circuit_breaker import CircuitBreaker

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Globals ──────────────────────────────────────────────────────────────
registry = AdapterRegistry()
cdc_agent = CDCAgent(poll_interval=settings.cdc_poll_interval)
event_processor = EventStoreProcessor(registry, poll_interval=5.0)


# ── Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle."""
    logger.info("Starting Inventory Service v0.1.0")

    # ── Adapter registry ────────────────────────────────────────────
    # WooCommerce
    if settings.wood_commerce_url and settings.wood_commerce_consumer_key:
        wc_adapter = WooCommerceAdapter()
        registry.register(wc_adapter)
        logger.info("WooCommerce adapter registered")
    else:
        logger.info(
            "WooCommerce adapter skipped — WOOD_COMMERCE_URL or "
            "WOOD_COMMERCE_CONSUMER_KEY not set"
        )

    # Mercado Livre
    if settings.ml_client_id and settings.ml_client_secret:
        ml_adapter = MercadoLivreAdapter()
        registry.register(ml_adapter)
        logger.info("Mercado Livre adapter registered")
    else:
        logger.info(
            "Mercado Livre adapter skipped — ML_CLIENT_ID or "
            "ML_CLIENT_SECRET not set"
        )

    # ── Inject registry into route modules ───────────────────────────
    _set_health_registry(registry)
    _set_ml_registry(registry)
    _set_wc_registry(registry)
    _set_sell_registry(registry)
    _set_sell_cb(cb := CircuitBreaker())

    # ── Start CDC Agent ──────────────────────────────────────────────
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

    # ── Start EventStore Processor ───────────────────────────────────
    processor_task = asyncio.create_task(event_processor.run_forever())
    logger.info("EventStore Processor started (poll every %.1fs)", event_processor.poll_interval)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down Inventory Service")

    # Stop CDC Agent
    if cdc_task is not None:
        cdc_agent.stop()
        cdc_task.cancel()
        try:
            await cdc_task
        except asyncio.CancelledError:
            pass

    # Stop EventStore Processor
    event_processor.stop()
    processor_task.cancel()
    try:
        await processor_task
    except asyncio.CancelledError:
        pass


# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Inventory Service",
    description="Omnichannel adapter bridging OSPOS with WooCommerce, Shopee, ML",
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

# ── Routers ─────────────────────────────────────────────────────────────
app.include_router(health_router, prefix="/v1")
app.include_router(products_router, prefix="/v1")
app.include_router(mercadolivre_router, prefix="/v1")
app.include_router(woocommerce_router, prefix="/v1")
app.include_router(store_router, prefix="/v1")
app.include_router(sell_router, prefix="/v1")
