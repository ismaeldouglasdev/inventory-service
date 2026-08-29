"""Circuit-breaker / state tracking per channel."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelState(Base):
    __tablename__ = "channel_state"

    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), default="CLOSED", nullable=False
    )  # CLOSED|OPEN|HALF_OPEN
    priority: Mapped[int] = mapped_column(Integer, default=99)
    stock_buffer: Mapped[int] = mapped_column(Integer, default=2)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    open_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── OAuth token persistence (channel = "mercadolivre") ───────────
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ml_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # epoch seconds stored as datetime

    def __repr__(self) -> str:
        return (
            f"<ChannelState channel={self.channel!r} "
            f"status={self.status!r} active={self.active}>"
        )
