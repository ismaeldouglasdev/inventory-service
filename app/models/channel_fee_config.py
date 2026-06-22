"""Fee configuration per channel / fee type."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelFeeConfig(Base):
    __tablename__ = "channel_fee_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    fee_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # commission|shipping_subsidy|ads_minimum|payment_gateway
    value: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # manual|auto_detected|import
    notes: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelFeeConfig id={self.id} channel={self.channel!r} "
            f"fee_type={self.fee_type!r} value={self.value}>"
        )
