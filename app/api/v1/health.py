"""Health-check endpoint for probes and monitoring."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.schemas.health import HealthResponse

logger = logging.getLogger(__name__)

# We inject the registry from the app state (set in main.py).
# This router dependency avoids a circular import.
_registry: AdapterRegistry | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


router = APIRouter(tags=["health"])


async def _check_db(session: AsyncSession) -> str:
    """Return ``'connected'`` or ``'disconnected'``."""
    try:
        await session.execute(text("SELECT 1"))
        return "connected"
    except Exception as exc:
        logger.warning("Health-check DB probe failed: %s", exc)
        return "disconnected"


@router.get("/health", response_model=HealthResponse)
async def health_check(
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    """Return service health information."""
    db_status = await _check_db(session)
    adapter_names = list(_registry.channel_names()) if _registry else []

    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        version="0.1.0",
        database=db_status,
        adapters=adapter_names,
    )
