from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.schemas.health import ChannelHealthDetail, HealthDetailResponse, HealthResponse
from app.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

_registry: AdapterRegistry | None = None
_circuit_breaker: CircuitBreaker | None = None
_start_time: datetime | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


def _set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _circuit_breaker
    _circuit_breaker = cb


router = APIRouter(tags=["health"])


async def _check_db(session: AsyncSession) -> tuple[str, float | None]:
    start = time.monotonic()
    try:
        await session.execute(text("SELECT 1"))
        latency = (time.monotonic() - start) * 1000
        return "connected", round(latency, 2)
    except Exception as exc:
        logger.warning("Health-check DB probe failed: %s", exc)
        return "disconnected", None


def _set_start_time() -> None:
    global _start_time
    _start_time = datetime.now(timezone.utc)


@router.get("/health", response_model=HealthResponse)
async def health_check(
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    db_status, _ = await _check_db(session)
    adapter_names = list(_registry.channel_names()) if _registry else []
    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        version="0.1.0",
        database=db_status,
        adapters=adapter_names,
    )


@router.get("/health/detail", response_model=HealthDetailResponse)
async def health_detail(
    session: AsyncSession = Depends(get_session),
) -> HealthDetailResponse:
    db_status, db_latency = await _check_db(session)
    adapter_names = list(_registry.channel_names()) if _registry else []
    uptime = None
    if _start_time:
        uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()

    channels: list[ChannelHealthDetail] = []
    if _circuit_breaker and adapter_names:
        for ch in adapter_names:
            try:
                state = await _circuit_breaker._get_state(ch)
                if state is None:
                    ch_status = "healthy"
                    ch_active = True
                    ch_failures = 0
                    ch_circuit = "CLOSED"
                elif state.status == "OPEN":
                    ch_status = "degraded"
                    ch_active = state.active
                    ch_failures = state.failure_count
                    ch_circuit = state.status
                else:
                    ch_status = "healthy" if state.active else "degraded"
                    ch_active = state.active
                    ch_failures = state.failure_count
                    ch_circuit = state.status
                channels.append(
                    ChannelHealthDetail(
                        channel=ch,
                        status=ch_status,
                        active=ch_active,
                        failure_count=ch_failures,
                        circuit_state=ch_circuit,
                        last_error=None,
                    )
                )
            except Exception:
                channels.append(
                    ChannelHealthDetail(
                        channel=ch,
                        status="unknown",
                        active=True,
                        failure_count=0,
                        circuit_state="UNKNOWN",
                    )
                )

    return HealthDetailResponse(
        status="ok" if db_status == "connected" else "degraded",
        version="0.1.0",
        database=db_status,
        database_latency_ms=db_latency,
        uptime_seconds=uptime,
        channels=channels,
    )


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def health_ready(
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    db_status, _ = await _check_db(session)
    if db_status != "connected":
        return {"status": "not_ready", "reason": "database_disconnected"}
    return {"status": "ready"}
