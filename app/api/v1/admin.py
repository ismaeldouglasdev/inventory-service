"""Admin endpoints for monitoring and operations.

Includes:
- Dead Letter Queue recovery (re-process DEAD events)
- Event store inspection
- Channel health overview
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.models.event_store import EventStore
from app.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

_registry: AdapterRegistry | None = None
_circuit_breaker: CircuitBreaker | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


def _set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _circuit_breaker
    _circuit_breaker = cb


router = APIRouter(tags=["admin"])


# ── Dead Letter Queue ──────────────────────────────────────────────────


@router.get("/admin/events/dead")
async def list_dead_events(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List events in DEAD state for manual review."""
    stmt = (
        select(EventStore)
        .where(EventStore.state == "dead")
        .order_by(EventStore.updated_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    return {
        "total": len(events),
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "state": ev.state,
                "sku": ev.sku,
                "channel": ev.channel,
                "retry_count": ev.retry_count,
                "max_retries": ev.max_retries,
                "error": None,  # error info is in payload
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
                "updated_at": ev.updated_at.isoformat() if ev.updated_at else None,
            }
            for ev in events
        ],
    }


@router.post("/admin/events/dead/{event_id}/reprocess")
async def reprocess_dead_event(
    event_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move a DEAD event back to PENDING for reprocessing."""
    result = await session.execute(
        select(EventStore).where(
            EventStore.id == event_id,
            EventStore.state == "dead",
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found or not in DEAD state")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        update(EventStore)
        .where(EventStore.id == event_id)
        .values(
            state="pending",
            retry_count=0,
            updated_at=now,
        )
    )
    await session.execute(stmt)
    await session.commit()

    logger.info("DEAD event %s moved to PENDING for reprocess", event_id)
    return {"status": "ok", "event_id": event_id, "new_state": "pending"}


@router.post("/admin/events/dead/reprocess-all")
async def reprocess_all_dead_events(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move ALL DEAD events back to PENDING."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        update(EventStore)
        .where(EventStore.state == "dead")
        .values(state="pending", retry_count=0, updated_at=now)
    )
    result = await session.execute(stmt)
    await session.commit()

    count = result.rowcount
    logger.info("Reprocessed %d DEAD events → PENDING", count)
    return {"status": "ok", "reprocessed": count}


@router.delete("/admin/events/dead/{event_id}")
async def delete_dead_event(
    event_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a DEAD event (acknowledge and discard)."""
    result = await session.execute(
        select(EventStore).where(
            EventStore.id == event_id,
            EventStore.state == "dead",
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found or not in DEAD state")

    stmt = sa_delete(EventStore).where(EventStore.id == event_id)
    await session.execute(stmt)
    await session.commit()

    logger.info("DEAD event %s deleted (acknowledged)", event_id)
    return {"status": "ok", "deleted": event_id}


# ── Event Store Inspection ────────────────────────────────────────────


@router.get("/admin/events/stats")
async def event_stats(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get event store statistics grouped by state."""
    from sqlalchemy import func

    stmt = (
        select(EventStore.state, func.count(EventStore.id))
        .group_by(EventStore.state)
    )
    result = await session.execute(stmt)
    rows = result.all()

    stats = {state: count for state, count in rows}
    total = sum(stats.values())

    return {
        "total": total,
        "by_state": stats,
        "by_type": {},
    }


@router.get("/admin/events")
async def list_events(
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List events with optional state filter."""
    stmt = select(EventStore)
    if state:
        stmt = stmt.where(EventStore.state == state)
    stmt = stmt.order_by(EventStore.created_at.desc()).limit(limit).offset(offset)

    result = await session.execute(stmt)
    events = result.scalars().all()

    return {
        "total": len(events),
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "state": ev.state,
                "sku": ev.sku,
                "channel": ev.channel,
                "retry_count": ev.retry_count,
                "max_retries": ev.max_retries,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
                "updated_at": ev.updated_at.isoformat() if ev.updated_at else None,
            }
            for ev in events
        ],
    }
