"""Circuit Breaker per channel — prevents cascading failures.

State machine for each channel:

    CLOSED ──(failures >= threshold)──► OPEN
    OPEN   ──(timeout elapsed)───────► HALF_OPEN
    HALF_OPEN ──(success)──────────────► CLOSED
    HALF_OPEN ──(failure)──────────────► OPEN

The breaker also tracks the channel health status as
``ACTIVE | DEGRADED | DISABLED`` at the adapter level.

Usage::

    cb = CircuitBreaker()
    async with cb.guard("shopee"):
        # call external API — exceptions are caught automatically
        result = await adapter.update_stock(sku, qty)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update, insert

from app.database import async_session_factory
from app.models.channel_state import ChannelState
from app.models.processed_action import ProcessedAction
from app.utils.metrics import (
    adapter_failures,
    circuit_breaker_failures,
    circuit_breaker_state,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 5
DEFAULT_TIMEOUT_SECONDS = 60
# How long OPEN stays before transitioning to HALF_OPEN
OPEN_COOLDOWN_SECONDS = 30


# ── Exceptions ──────────────────────────────────────────────────────────

class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""


class CircuitBreaker:
    """Per-channel circuit breaker backed by the ``channel_state`` table.

    Thread-safe (async) — each channel has its own row in the DB so
    multiple service instances share the same breaker state.
    """

    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown: int = OPEN_COOLDOWN_SECONDS,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._local_failures: dict[str, int] = {}  # ephemeral counter

    # ── Public API ──────────────────────────────────────────────────────

    async def allow_request(self, channel: str) -> bool:
        """Check whether a request to *channel* is allowed.

        Returns ``True`` when the circuit is CLOSED or HALF_OPEN.
        Side-effect: transitions OPEN → HALF_OPEN if cooldown passed.
        """
        state = await self._get_state(channel)
        if state is None:
            return True  # no row = not yet tracked = allowed

        if state.status == "CLOSED":
            return True

        if state.status == "OPEN":
            # Check cooldown
            now = datetime.now(timezone.utc)
            open_until = state.open_until
            # SQLite stores naive UTC — make aware for comparison
            if open_until and open_until.tzinfo is None:
                open_until = open_until.replace(tzinfo=timezone.utc)
            if open_until and now < open_until:
                logger.info("Circuit OPEN for %s — request rejected", channel)
                return False
            # Cooldown expired → transition to HALF_OPEN
            await self._set_status(channel, "HALF_OPEN")
            logger.info("Circuit %s: OPEN → HALF_OPEN (cooldown expired)", channel)
            return True

        if state.status == "HALF_OPEN":
            return True  # allow one probe request

        return True

    async def on_success(self, channel: str) -> None:
        """Report a successful call.

        Resets failure counters. If HALF_OPEN → CLOSED.
        """
        state = await self._get_state(channel)
        await self._set_status(channel, "CLOSED", failure_count=0)
        circuit_breaker_state.labels(channel=channel).set(0)
        circuit_breaker_failures.labels(channel=channel).set(0)

        if state and state.status != "CLOSED":
            logger.info("Circuit %s: %s → CLOSED (success)", channel, state.status)

    async def on_failure(self, channel: str, error: str = "") -> None:
        """Report a failed call.

        Increments failure counter. If threshold reached → OPEN.
        """
        state = await self._get_state(channel)
        failures = (state.failure_count if state else 0) + 1
        now = datetime.now(timezone.utc)

        adapter_failures.labels(channel=channel, operation="api_call").inc()
        circuit_breaker_failures.labels(channel=channel).set(failures)

        if failures >= self.threshold:
            open_until = now + timedelta(seconds=self.cooldown)
            await self._set_status(
                channel,
                "OPEN",
                failure_count=failures,
                last_failure=now,
                open_until=open_until,
            )
            circuit_breaker_state.labels(channel=channel).set(2)
            logger.warning(
                "Circuit %s → OPEN (%d failures, cooldown=%ds)",
                channel, failures, self.cooldown,
            )
        else:
            new_status = state.status if state else "CLOSED"
            await self._set_status(
                channel,
                new_status,
                failure_count=failures,
                last_failure=now,
            )
            cb_val = 0 if new_status == "CLOSED" else (1 if new_status == "HALF_OPEN" else 2)
            circuit_breaker_state.labels(channel=channel).set(cb_val)
            logger.info(
                "Circuit %s: failure %d/%d",
                channel, failures, self.threshold,
            )

    # ── Context guard ──────────────────────────────────────────────────

    @asynccontextmanager
    async def guard(
        self,
        channel: str,
        *,
        event_id: str | None = None,
        action_type: str = "",
    ) -> AsyncGenerator[None, None]:
        """Async context manager — wraps an external call with CB logic.

        Usage::

            async with cb.guard("shopee", event_id=ev.id, action_type="update_stock"):
                result = await adapter.update_stock(sku, qty)

        On success: records a ProcessedAction row.
        On failure: records failure + optional ProcessedAction row.
        """
        if not await self.allow_request(channel):
            raise CircuitBreakerOpenError(
                f"Circuit breaker OPEN for channel {channel!r}"
            )

        start = datetime.now(timezone.utc)
        try:
            yield
        except Exception as exc:
            duration = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            await self.on_failure(channel, str(exc))
            if event_id:
                await self._record_action(
                    event_id=event_id,
                    target=channel,
                    status="failed",
                    action_type=action_type,
                    response=str(exc)[:500],
                    duration_ms=duration,
                )
            raise
        else:
            duration = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            await self.on_success(channel)
            if event_id:
                await self._record_action(
                    event_id=event_id,
                    target=channel,
                    status="ok",
                    action_type=action_type,
                    duration_ms=duration,
                )

    # ── Helpers ────────────────────────────────────────────────────────

    async def _get_state(self, channel: str) -> ChannelState | None:
        async with async_session_factory() as session:
            result = await session.execute(
                select(ChannelState).where(ChannelState.channel == channel)
            )
            return result.scalar_one_or_none()

    async def _set_status(
        self,
        channel: str,
        status: str,
        *,
        failure_count: int | None = None,
        last_failure: datetime | None = None,
        open_until: datetime | None = None,
    ) -> None:
        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)

            # Check if row exists
            result = await session.execute(
                select(ChannelState).where(ChannelState.channel == channel)
            )
            existing = result.scalar_one_or_none()

            values: dict[str, Any] = {
                "status": status,
                "active": status != "OPEN",
                "updated_at": now,
            }
            if failure_count is not None:
                values["failure_count"] = failure_count
            if last_failure is not None:
                values["last_failure_at"] = last_failure
            if open_until is not None:
                values["open_until"] = open_until

            if existing:
                stmt = (
                    update(ChannelState)
                    .where(ChannelState.channel == channel)
                    .values(**values)
                )
                await session.execute(stmt)
            else:
                session.add(ChannelState(channel=channel, **values))

            await session.commit()

    async def _record_action(
        self,
        event_id: str,
        target: str,
        status: str,
        action_type: str,
        response: str = "",
        duration_ms: int = 0,
    ) -> None:
        async with async_session_factory() as session:
            action = ProcessedAction(
                event_id=event_id,
                target_system=target,
                status=status,
                action_type=action_type,
                response_summary=response or None,
                duration_ms=duration_ms or None,
                created_at=datetime.now(timezone.utc),
            )
            session.add(action)
            await session.commit()
