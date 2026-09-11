"""Archive table for completed/old events from the event store."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventStoreArchive(Base):
    """Archived events from the main event_store table.

    Used to keep the active event_store lean while preserving
    historical data for auditing and debugging.
    """

    __tablename__ = "event_store_archive"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Any] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ospos_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_archive_sku", "sku"),
        Index("ix_archive_state", "state"),
        Index("ix_archive_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<EventStoreArchive id={self.id!r} event_type={self.event_type!r} "
            f"state={self.state!r}>"
        )
