"""Order + OrderItem — pedidos da loja online (B3).

Fluxo: cliente submete pedido → ``PENDING`` → admin confirma →
``CONFIRMED`` (dispara reserva de estoque no sell_pipeline) ou cancela
→ ``CANCELLED`` (ex.: estoque insuficiente). Pagamento v1 é WhatsApp-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrderStatus(str, Enum):
    """Status do pedido, persistido como string (PENDING/CONFIRMED/CANCELLED)."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class Order(Base):
    """Pedido da loja — pode ser de cliente logado (customer_id) ou convidado."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    guest_name: Mapped[str] = mapped_column(String(128), nullable=False)
    guest_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    guest_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    whatsapp: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=OrderStatus.PENDING.value, nullable=False, index=True
    )
    payment_method: Mapped[str] = mapped_column(
        String(20), default="whatsapp", nullable=False
    )
    cancelled_reason: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    subtotal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    freight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cep: Mapped[Optional[str]] = mapped_column(String(9), nullable=True, index=True)
    address: Mapped[str] = mapped_column(String(512), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
    )

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.id",
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} status={self.status!r} "
            f"total={self.total} customer_id={self.customer_id}>"
        )


class OrderItem(Base):
    """Linha de pedido — SKU congelado no momento da compra."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("store_products.id", ondelete="SET NULL"), nullable=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=datetime.now(timezone.utc),
    )

    order: Mapped[Optional[Order]] = relationship(back_populates="items")

    def __repr__(self) -> str:
        return (
            f"<OrderItem id={self.id} sku={self.sku!r} "
            f"qty={self.qty} price={self.price}>"
        )