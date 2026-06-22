"""Event-sourcing table for change-data-capture and command events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventStore(Base):
    __tablename__ = "event_store"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True
    )  # UUID string
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Any] = mapped_column(Text, nullable=False)  # JSON
    state: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending|processing|completed|failed|partial|dead
    sku: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    channel: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    ospos_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_event_store_state", "state"),
        Index("ix_event_store_sku", "sku"),
        Index("ix_event_store_channel", "channel"),
    )

    def __repr__(self) -> str:
        return (
            f"<EventStore id={self.id!r} event_type={self.event_type!r} "
            f"state={self.state!r}>"
        )
