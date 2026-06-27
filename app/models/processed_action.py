"""Idempotency tracker — prevents duplicate side-effects.

Every time the system dispatches an event to an external channel
(WooCommerce, Mercado Livre, Shopee), it records a row here.  The
composite primary key (event_id, target_system) guarantees that the
same action is never applied twice.

    if exists(event_id, "woocommerce"):
        skip  # already applied
    else:
        dispatch()
        insert(event_id, "woocommerce", "ok")
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProcessedAction(Base):
    __tablename__ = "processed_actions"

    event_id: Mapped[str] = mapped_column(
        String(36), primary_key=True
    )
    target_system: Mapped[str] = mapped_column(
        String(32), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="ok", nullable=False
    )  # ok | failed | skipped
    sku: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    action_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # update_stock | update_price | publish_product | delete_product
    request_summary: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    response_summary: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ProcessedAction event={self.event_id!r} "
            f"target={self.target_system!r} status={self.status!r}>"
        )
