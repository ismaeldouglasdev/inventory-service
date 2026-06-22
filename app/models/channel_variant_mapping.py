"""Maps a variant SKU to its external variant ID on a channel."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelVariantMapping(Base):
    __tablename__ = "channel_variant_mapping"

    variant_sku: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_var_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelVariantMapping variant_sku={self.variant_sku!r} "
            f"channel={self.channel!r}>"
        )
