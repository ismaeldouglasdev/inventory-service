"""Store product — produto sincronizado do OSPOS para exibição na loja online.

Guarda uma cópia local dos produtos que devem aparecer na loja,
com filtros de estoque positivo e presença de imagem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StoreProduct(Base):
    __tablename__ = "store_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ospos_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    store_visible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    last_modified: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<StoreProduct id={self.id} sku={self.sku!r} "
            f"name={self.name!r} stock={self.stock} visible={self.store_visible}>"
        )
