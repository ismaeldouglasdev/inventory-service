"""Inventory State — reservation-to-commitment lifecycle for stock.

Every sale (online or in-store) goes through:

    RESERVED → CONFIRMED → COMMITTED

- RESERVED:   stock is tentatively held (online order received)
- CONFIRMED:  OSPOS confirmed the deduction — commit to channel
- COMMITTED:  channel confirmed the propagation — terminal state
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InventoryState(Base):
    __tablename__ = "inventory_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)

    # Lifecycle
    state: Mapped[str] = mapped_column(
        String(16), default="reserved", nullable=False, index=True
    )  # reserved | confirmed | committed | cancelled

    # Quantities
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    # Financial (snapshot at time of reservation)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    total: Mapped[float] = mapped_column(Float, nullable=False)

    # References
    source_event_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    ospos_sale_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    committed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryState id={self.id} sku={self.sku!r} "
            f"order={self.order_id!r} state={self.state!r}>"
        )
