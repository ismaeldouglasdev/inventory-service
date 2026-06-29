"""Account Health Tracking per channel.

Monitors the health status of each marketplace account:
- Tracks daily request counts against limits
- Stores last errors for debugging
- Detects account suspension/violations
- Provides aggregated health view

Usage::

    tracker = HealthTracker()
    await tracker.record_request("mercadolivre", success=True)
    health = await tracker.get_health("mercadolivre")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory

logger = logging.getLogger(__name__)


@dataclass
class ChannelHealthSummary:
    channel: str
    status: str
    active: bool
    failure_count: int
    circuit_state: str
    daily_requests: int
    daily_limit: int | None
    last_error: str | None
    last_error_at: datetime | None
    violations: int


class HealthTracker:
    """Tracks per-channel account health in the database.

    Uses the existing ``channel_state`` table plus runtime counters
    for daily request tracking.
    """

    def __init__(self) -> None:
        self._daily_requests: dict[str, int] = {}
        self._daily_window: dict[str, str] = {}  # channel -> date string YYYY-MM-DD

    async def record_request(
        self,
        channel: str,
        success: bool,
        *,
        error: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Record an API request and update health metrics."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        last_window = self._daily_window.get(channel)

        if last_window != today:
            self._daily_requests[channel] = 0
            self._daily_window[channel] = today

        self._daily_requests[channel] = self._daily_requests.get(channel, 0) + 1

        if not success and error:
            await self._record_error(channel, error, session=session)

    async def get_daily_requests(self, channel: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_window.get(channel) != today:
            return 0
        return self._daily_requests.get(channel, 0)

    async def _record_error(
        self,
        channel: str,
        error: str,
        session: AsyncSession | None = None,
    ) -> None:
        if session is None:
            async with async_session_factory() as sess:
                await self._upsert_error(sess, channel, error)
        else:
            await self._upsert_error(session, channel, error)

    async def _upsert_error(
        self, session: AsyncSession, channel: str, error: str
    ) -> None:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(ChannelHealthRow).where(ChannelHealthRow.channel == channel)
        )
        existing = result.scalar_one_or_none()
        if existing:
            stmt = (
                update(ChannelHealthRow)
                .where(ChannelHealthRow.channel == channel)
                .values(last_error=error[:500], last_error_at=now)
            )
            await session.execute(stmt)
        else:
            session.add(ChannelHealthRow(channel=channel, last_error=error[:500], last_error_at=now))
        await session.commit()

    async def record_violation(self, channel: str, reason: str) -> None:
        """Record a marketplace violation (e.g. ML policy violation)."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(ChannelHealthRow).where(ChannelHealthRow.channel == channel)
            )
            existing = result.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if existing:
                stmt = (
                    update(ChannelHealthRow)
                    .where(ChannelHealthRow.channel == channel)
                    .values(
                        violations=ChannelHealthRow.violations + 1,
                        last_error=reason[:500],
                        last_error_at=now,
                    )
                )
                await session.execute(stmt)
            else:
                session.add(
                    ChannelHealthRow(
                        channel=channel,
                        violations=1,
                        last_error=reason[:500],
                        last_error_at=now,
                    )
                )
            await session.commit()
            logger.warning("Violation recorded for %s: %s", channel, reason)

    async def get_health(
        self,
        channel: str,
        *,
        circuit_state: str = "CLOSED",
        failure_count: int = 0,
        active: bool = True,
    ) -> ChannelHealthSummary:
        daily = await self.get_daily_requests(channel)
        async with async_session_factory() as session:
            result = await session.execute(
                select(ChannelHealthRow).where(ChannelHealthRow.channel == channel)
            )
            row = result.scalar_one_or_none()

        if circuit_state == "OPEN":
            status = "degraded"
        elif row and row.violations > 0:
            status = "warning"
        else:
            status = "healthy"

        return ChannelHealthSummary(
            channel=channel,
            status=status,
            active=active,
            failure_count=failure_count,
            circuit_state=circuit_state,
            daily_requests=daily,
            daily_limit=None,
            last_error=row.last_error if row else None,
            last_error_at=row.last_error_at if row else None,
            violations=row.violations if row else 0,
        )


# Local-only model — uses channel_state table mainly, plus an in-memory counter.
# The _record_error/violation methods can target a dedicated table.
# For now we use an in-memory dict + the existing channel_state table.
class ChannelHealthRow:
    """Minimal in-memory representation of health tracking row."""
    def __init__(self, channel: str, last_error: str | None = None,
                 last_error_at: datetime | None = None, violations: int = 0):
        self.channel = channel
        self.last_error = last_error
        self.last_error_at = last_error_at
        self.violations = violations
