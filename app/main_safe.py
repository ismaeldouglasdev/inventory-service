"""Entry-point seguro com apenas os roteadores essenciais:
   - health (ping para frontend)
   - store (HTTP para loja-online)
   - agent_bridge (opcional para WebSocket scanner)
   - metrics (scrape)

Desabilitar adaptadores externos e circuit-breaker que não precisam para testes rápidos.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router, _set_start_time
from app.api.v1.store import router as store_router, _set_store_sync
from app.api.v1.agent_bridge import router as agent_bridge_router
from app.services.store_sync import StoreSync
from app.config import settings
from app.utils.logging import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# --- Autodescricao da loja ---
_sync = StoreSync()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Modo seguro do Inventory Service em execu\xe7\xe3o")
    _set_start_time()
    _set_store_sync(_sync)
    yield

app = FastAPI(title="Inventory Service Seguro", lifespan=lifespan, version="0.0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/v1")
app.include_router(store_router, prefix="/v1")
app.include_router(agent_bridge_router, prefix="/v1")
logger.info("Roteadores minimizados adicionados")
