"""Orders API — pedidos da loja online (B3 Loja Online Overhaul).

Fluxo:
  POST /v1/orders            → cliente (logado ou convidado) cria pedido PENDING
  GET  /v1/orders/{id}       → dono (via token) ou admin consulta pedido
  GET  /v1/admin/orders      → admin lista pedidos
  PUT  /v1/admin/orders/{id}/status → admin confirma (reserva estoque via
       sell_pipeline) ou cancela um pedido PENDING

O canal usado nas reservas é ``"loja"`` — identifica pedidos originados na
loja online, distintos de vendas OSPOS/WooCommerce.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.registry import AdapterRegistry
from app.database import get_session
from app.models.customer import Customer
from app.models.order import Order, OrderItem, OrderStatus
from app.models.store_product import StoreProduct
from app.services.circuit_breaker import CircuitBreaker
from app.services.sell_pipeline import SellPipeline
from app.utils.security import (
    get_optional_customer,
    rate_limit_admin,
    rate_limit_store,
    rate_limit_write,
    resolve_customer_or_admin,
    verify_admin_auth,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])
admin_orders_router = APIRouter(prefix="/admin/orders", tags=["orders-admin"])

# ── Global refs (injected at startup, mesmo padrão do sell.py) ─────────
_registry: AdapterRegistry | None = None
_circuit_breaker: CircuitBreaker | None = None


def _set_registry(r: AdapterRegistry) -> None:
    global _registry
    _registry = r


def _set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _circuit_breaker
    _circuit_breaker = cb


def _get_pipeline() -> SellPipeline:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Adapter registry not initialised")
    return SellPipeline(_registry, _circuit_breaker or CircuitBreaker())


# ── Schemas ────────────────────────────────────────────────────────────

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class OrderItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=999)


class OrderCreate(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1, max_length=100)
    guest_name: Optional[str] = Field(default=None, max_length=128)
    guest_email: Optional[str] = Field(default=None, pattern=_EMAIL_PATTERN, max_length=255)
    guest_phone: Optional[str] = Field(default=None, max_length=32)
    whatsapp: Optional[str] = Field(default=None, max_length=32)
    cep: Optional[str] = Field(default=None, max_length=9)
    address: Optional[str] = Field(default=None, max_length=512)
    freight: float = Field(default=0.0, ge=0.0)


class OrderItemOut(BaseModel):
    id: int
    sku: str
    name: str
    price: float
    qty: int

    model_config = {"from_attributes": True}


class OrderOut(BaseModel):
    id: int
    customer_id: Optional[int]
    guest_name: str
    guest_email: str
    guest_phone: str
    whatsapp: str
    status: str
    payment_method: str
    cancelled_reason: Optional[str]
    subtotal: float
    freight: float
    total: float
    cep: Optional[str]
    address: str
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut]

    model_config = {"from_attributes": True}


class OrderStatusUpdate(BaseModel):
    status: OrderStatus
    reason: Optional[str] = Field(default=None, max_length=512)


# ── Helpers ────────────────────────────────────────────────────────────

async def _load_order_with_items(order_id: int) -> Optional[Order]:
    """Busca pedido com items carregados (evita MissingGreenlet com lazy load)."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
        return result.scalar_one_or_none()


# ── Endpoints públicos ─────────────────────────────────────────────────

@router.post(
    "",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_write)],
)
async def create_order(
    body: OrderCreate,
    customer_id: Optional[int] = Depends(get_optional_customer),
) -> OrderOut:
    """Cria pedido PENDING — cliente logado (token) ou convidado.

    Logado: email/telefone (se enviados) precisam bater com o cadastro e o
    whatsapp cai no telefone do cliente quando omitido.
    Convidado: guest_name/guest_email/guest_phone/whatsapp são obrigatórios.
    """
    async for session in get_session():
        # ── Identidade ──────────────────────────────────────────────
        customer: Optional[Customer] = None
        guest_name = body.guest_name
        guest_email = body.guest_email
        guest_phone = body.guest_phone
        whatsapp = body.whatsapp

        if customer_id is not None:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conta de cliente não encontrada",
                )
            if guest_email is not None and guest_email.strip().lower() != customer.email:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email não corresponde ao cadastro",
                )
            if guest_phone is not None and guest_phone != customer.phone:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Telefone não corresponde ao cadastro",
                )
            guest_name = guest_name or customer.name
            guest_email = customer.email
            whatsapp = whatsapp or customer.phone
            if guest_phone is None:
                guest_phone = customer.phone or ""
            if not whatsapp:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="WhatsApp é obrigatório para receber o pedido",
                )
        else:
            missing = [
                field
                for field, value in (
                    ("guest_name", guest_name),
                    ("guest_email", guest_email),
                    ("guest_phone", guest_phone),
                    ("whatsapp", whatsapp),
                )
                if not value
            ]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cadastro incompleto — campos obrigatórios: {', '.join(missing)}",
                )

        # ── Itens (valida contra StoreProduct visível na loja) ─────
        order_items: list[OrderItem] = []
        subtotal = 0.0
        for item in body.items:
            product = (
                await session.execute(
                    select(StoreProduct).where(StoreProduct.sku == item.sku)
                )
            ).scalar_one_or_none()
            if product is None or not product.store_visible:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Produto SKU {item.sku!r} indisponível na loja",
                )
            if product.stock < item.qty:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Estoque insuficiente para SKU {item.sku!r}",
                )
            line_total = product.price * item.qty
            subtotal += line_total
            order_items.append(
                OrderItem(
                    product_id=product.id,
                    sku=product.sku,
                    name=product.name,
                    price=product.price,
                    qty=item.qty,
                )
            )

        freight = round(body.freight, 2)
        order = Order(
            customer_id=customer.id if customer else None,
            guest_name=(guest_name or "").strip(),
            guest_email=(guest_email or "").strip().lower(),
            guest_phone=(guest_phone or "").strip(),
            whatsapp=(whatsapp or "").strip(),
            status=OrderStatus.PENDING.value,
            payment_method="whatsapp",
            subtotal=round(subtotal, 2),
            freight=freight,
            total=round(subtotal + freight, 2),
            cep=body.cep,
            address=(body.address or "").strip(),
            items=order_items,
        )
        session.add(order)
        await session.commit()

        loaded = await _load_order_with_items(order.id)
        if loaded is None:  # pragma: no cover - acabamos de commitar
            raise HTTPException(status_code=500, detail="Falha ao recarregar pedido")
        return OrderOut.model_validate(loaded)

    raise HTTPException(status_code=500, detail="Falha interna ao criar pedido")  # pragma: no cover


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    dependencies=[Depends(rate_limit_store)],
)
async def get_order(
    order_id: int,
    viewer: tuple[str, Optional[int]] = Depends(resolve_customer_or_admin),
) -> OrderOut:
    """Retorna pedido — apenas o dono (token do cliente) ou admin.

    Para um cliente, pedidos de outra conta (ou de convidados) são
    indistinguíveis de inexistência → 404, sem vazar dados.
    """
    role, viewer_id = viewer
    order = await _load_order_with_items(order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pedido não encontrado",
        )
    if role == "customer" and order.customer_id != viewer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pedido não encontrado",
        )
    return OrderOut.model_validate(order)


# ── Endpoints admin ────────────────────────────────────────────────────

@admin_orders_router.get(
    "",
    response_model=list[OrderOut],
    dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)],
)
async def list_admin_orders(
    status_filter: Optional[OrderStatus] = Query(
        default=None, alias="status", description="Filtra por status do pedido"
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OrderOut]:
    """Lista pedidos (mais recentes primeiro), opcionalmente por status."""
    async for session in get_session():
        query = select(Order).options(selectinload(Order.items))
        if status_filter is not None:
            query = query.where(Order.status == status_filter.value)
        query = query.order_by(Order.created_at.desc()).limit(limit)
        result = await session.execute(query)
        orders = result.scalars().all()
        return [OrderOut.model_validate(order) for order in orders]
    return []  # pragma: no cover


@admin_orders_router.put(
    "/{order_id}/status",
    response_model=OrderOut,
    dependencies=[Depends(verify_admin_auth), Depends(rate_limit_admin)],
)
async def update_order_status(
    order_id: int,
    body: OrderStatusUpdate,
) -> OrderOut:
    """Confirma ou cancela um pedido PENDING.

    CONFIRMED → reserva o estoque item a item via sell_pipeline (canal
    ``"loja"``). Se alguma reserva falhar (ex.: estoque insuficiente), as
    reservas já feitas são desfeitas e o pedido vira CANCELLED com o motivo.
    CANCELLED → registra o motivo (padrão: "Cancelado pelo lojista").
    Pedidos PENDING não voltam para outro estado e não podem ser reabertos.
    """
    async for session in get_session():
        result = await session.execute(
            select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pedido não encontrado",
            )
        if order.status != OrderStatus.PENDING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pedido já está {order.status}",
            )
        if body.status == OrderStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pedido PENDING não pode voltar para PENDING",
            )

        if body.status == OrderStatus.CONFIRMED:
            pipeline = _get_pipeline()
            reserved_ids: list[int] = []
            try:
                for item in order.items:
                    res = await pipeline.reserve(
                        sku=item.sku,
                        quantity=item.qty,
                        unit_price=item.price,
                        channel="loja",
                        order_id=str(order.id),
                        notes=f"Pedido #{order.id}",
                    )
                    reserved_ids.append(res["id"])
            except ValueError as exc:
                for rid in reserved_ids:
                    try:
                        await pipeline.cancel(rid, reason="Pedido cancelado por falha na reserva")
                    except ValueError:
                        logger.warning(
                            "Falha ao desfazer reserva %s do pedido %s (não existe mais)",
                            rid,
                            order.id,
                        )
                order.status = OrderStatus.CANCELLED.value
                order.cancelled_reason = str(exc)
                await session.commit()
                loaded = await _load_order_with_items(order.id)
                if loaded is None:  # pragma: no cover
                    raise HTTPException(status_code=500, detail="Falha ao recarregar pedido")
                return OrderOut.model_validate(loaded)
            order.status = OrderStatus.CONFIRMED.value
        else:  # CANCELLED
            order.status = OrderStatus.CANCELLED.value
            order.cancelled_reason = body.reason or "Cancelado pelo lojista"

        await session.commit()
        loaded = await _load_order_with_items(order.id)
        if loaded is None:  # pragma: no cover
            raise HTTPException(status_code=500, detail="Falha ao recarregar pedido")
        return OrderOut.model_validate(loaded)

    raise HTTPException(status_code=500, detail="Falha interna ao atualizar pedido")  # pragma: no cover