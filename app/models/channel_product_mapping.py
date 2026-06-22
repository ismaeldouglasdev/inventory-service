"""Maps a product (SKU) to its external representation on a channel."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ChannelProductMapping(Base):
    __tablename__ = "channel_product_mapping"

    sku: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("product_mapping.sku", ondelete="CASCADE"),
        primary_key=True,
    )
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_url: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    product: Mapped["ProductMapping"] = relationship(
        back_populates="channel_products"
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelProductMapping sku={self.sku!r} "
            f"channel={self.channel!r} external_id={self.external_id!r}>"
        )
