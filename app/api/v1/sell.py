"""Sell Pipeline API — reserve / confirm / commit / cancel endpoints.

These endpoints let external callers (webhooks, manual admin, internal
services) drive the inventory state machine.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.registry import AdapterRegistry
from app.services.circuit_breaker import CircuitBreaker
from app.services.sell_pipeline import SellPipeline
from app.utils.security import verify_api_key, rate_limit_write

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sell", tags=["sell"])

# ── Global refs (injected at startup) ──────────────────────────────────
_registry: AdapterRegistry | None = None
_circuit_breaker: CircuitBreaker | None = None


def _set_registry(r: AdapterRegistry) -> None:
    global _registry
    _registry = r


def _set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _circuit_breaker
    _circuit_breaker = cb


def _get_pipeline() -> SellPipeline:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Adapter registry not initialised")
    return SellPipeline(_registry, _circuit_breaker or CircuitBreaker())


# ── Schemas ────────────────────────────────────────────────────────────

from pydantic import BaseModel


class ReserveRequest(BaseModel):
    sku: str
    quantity: int
    unit_price: float
    channel: str
    order_id: str
    source_event_id: str | None = None
    notes: str | None = None


class ConfirmRequest(BaseModel):
    reservation_id: int
    ospos_sale_id: str | None = None


class CancelRequest(BaseModel):
    reservation_id: int
    reason: str = ""


# ── Endpoints ──────────────────────────────────────────────────────────


@router.post("/reserve", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def reserve(body: ReserveRequest) -> dict[str, Any]:
    """Reserve stock for an order."""
    pipeline = _get_pipeline()
    try:
        return await pipeline.reserve(
            sku=body.sku,
            quantity=body.quantity,
            unit_price=body.unit_price,
            channel=body.channel,
            order_id=body.order_id,
            source_event_id=body.source_event_id,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/confirm", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def confirm(body: ConfirmRequest) -> dict[str, Any]:
    """Confirm a reservation (OSPOS processed the sale)."""
    pipeline = _get_pipeline()
    try:
        return await pipeline.confirm(
            reservation_id=body.reservation_id,
            ospos_sale_id=body.ospos_sale_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/commit", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def commit(reservation_id: int = Query(..., ge=1)) -> dict[str, Any]:
    """Mark a reservation as fully committed (channel propagated)."""
    pipeline = _get_pipeline()
    try:
        return await pipeline.commit(reservation_id=reservation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/cancel", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def cancel(body: CancelRequest) -> dict[str, Any]:
    """Cancel a reservation and restore stock."""
    pipeline = _get_pipeline()
    try:
        return await pipeline.cancel(
            reservation_id=body.reservation_id,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/sell", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def sell(body: ReserveRequest) -> dict[str, Any]:
    """Full sell flow: reserve → confirm → propagate → commit."""
    pipeline = _get_pipeline()
    try:
        return await pipeline.sell(
            sku=body.sku,
            quantity=body.quantity,
            unit_price=body.unit_price,
            channel=body.channel,
            order_id=body.order_id,
            source_event_id=body.source_event_id,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/reservations/{reservation_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def get_reservation(reservation_id: int) -> dict[str, Any]:
    """Get a single reservation."""
    pipeline = _get_pipeline()
    result = await pipeline.get_reservation(reservation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return result


@router.get("/reservations", dependencies=[Depends(verify_api_key), Depends(rate_limit_write)])
async def list_reservations(
    sku: str | None = Query(None),
    state: str | None = Query(None, pattern=r"^(reserved|confirmed|committed|cancelled)$"),
    order_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List reservations with optional filters."""
    pipeline = _get_pipeline()
    return await pipeline.list_reservations(
        sku=sku, state=state, order_id=order_id, limit=limit,
        )
