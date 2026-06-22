"""Product mapping — links OSPOS items to the service."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProductMapping(Base):
    __tablename__ = "product_mapping"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    ospos_id: Mapped[int] = mapped_column(Integer, nullable=False)
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False)
    store_id: Mapped[str] = mapped_column(
        String(32), default="principal", nullable=False
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    # Relationships
    channel_products: Mapped[list["ChannelProductMapping"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ProductMapping sku={self.sku!r} ospos_id={self.ospos_id}>"
