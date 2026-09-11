"""Testes dos endpoints de pedidos da loja (B3 Loja Online Overhaul).

Cobre:
  - Criação guest (sucesso, campos obrigatórios, validação de itens)
  - Criação com cliente logado (match email/phone, mismatch → 400)
  - Consulta: dono, outro cliente (404), admin, pedido de guest
  - Admin: listagem + filtro por status
  - Confirmação (reserva estoque via sell_pipeline, canal "loja")
  - Estoque insuficiente → CANCELLED + cancelled_reason
  - Double-confirm 409, body PENDING 400, cancel com/sem reason
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.v1 import orders as orders_api
from app.api.v1.orders import (
    OrderCreate,
    OrderItemIn,
    OrderStatusUpdate,
    create_order,
    get_order,
    list_admin_orders,
    update_order_status,
)
from app.models.inventory_state import InventoryState
from app.models.order import OrderStatus
from app.models.store_product import StoreProduct
from app.services.circuit_breaker import CircuitBreaker
from app.utils.security import create_customer_token


class TestOrders:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from app.database import Base, engine

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture(autouse=True)
    def inject_deps(self):
        """Registrar registry/CB no módulo — _get_pipeline() exige registry."""
        from app.adapters.registry import AdapterRegistry

        orders_api._set_registry(AdapterRegistry())
        orders_api._set_circuit_breaker(CircuitBreaker())

    async def _seed_product(
        self, sku: str = "ABC-123", stock: int = 10, price: float = 29.90,
        store_visible: bool = True, ospos_id: int | None = None,
    ) -> StoreProduct:
        async with async_session_factory() as session:
            p = StoreProduct(
                ospos_id=ospos_id if ospos_id is not None else abs(hash(sku)) % 10**6,
                sku=sku,
                name=f"Produto {sku}",
                description="",
                price=price,
                category="teste",
                stock=stock,
                store_visible=store_visible,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(p)
            await session.commit()
            return p

    async def _register_customer(self, email: str = "cliente@example.com") -> tuple[int, str]:
        """Cria customer via endpoint de registro; retorna (customer_id, token)."""
        from app.api.v1.customer_auth import RegisterRequest, register

        result = await register(
            RegisterRequest(
                name="Cliente Teste",
                email=email,
                phone="11999990000",
                password="senha-segura-1",
            )
        )
        return result.customer.id, result.token

    # ── Criação (guest) ───────────────────────────────────────────

    async def test_create_order_guest_success(self):
        await self._seed_product(stock=10, price=29.90)

        order = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=2)],
                guest_name="Comprador Anônimo",
                guest_email="comprador@example.com",
                guest_phone="11988887777",
                whatsapp="11988887777",
                freight=10.0,
            ),
            customer_id=None,
        )

        assert order.customer_id is None
        assert order.status == OrderStatus.PENDING.value
        assert order.payment_method == "whatsapp"
        assert order.subtotal == 59.80
        assert order.total == 69.80
        assert len(order.items) == 1
        assert order.items[0].name == "Produto ABC-123"
        assert order.guest_email == "comprador@example.com"

    async def test_create_order_guest_missing_fields_400(self):
        await self._seed_product(stock=10)
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(items=[OrderItemIn(sku="ABC-123", qty=1)]),
                customer_id=None,
            )
        assert exc.value.status_code == 400
        detail = exc.value.detail
        assert "guest_name" in detail
        assert "guest_email" in detail
        assert "guest_phone" in detail
        assert "whatsapp" in detail

    async def test_create_order_invalid_sku_400(self):
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="NAOEXISTE", qty=1)],
                    guest_name="Ana",
                    guest_email="ana@example.com",
                    guest_phone="11999990000",
                    whatsapp="11999990000",
                ),
                customer_id=None,
            )
        assert exc.value.status_code == 400
        assert "NAOEXISTE" in str(exc.value.detail)

    async def test_create_order_hidden_product_400(self):
        await self._seed_product(stock=10, store_visible=False)
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="ABC-123", qty=1)],
                    guest_name="Ana",
                    guest_email="ana@example.com",
                    guest_phone="11999990000",
                    whatsapp="11999990000",
                ),
                customer_id=None,
            )
        assert exc.value.status_code == 400
        assert "indisponível" in str(exc.value.detail)

    async def test_create_order_insufficient_stock_400(self):
        await self._seed_product(stock=2)
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="ABC-123", qty=5)],
                    guest_name="Ana",
                    guest_email="ana@example.com",
                    guest_phone="11999990000",
                    whatsapp="11999990000",
                ),
                customer_id=None,
            )
        assert exc.value.status_code == 400
        assert "Estoque insuficiente" in str(exc.value.detail)

    # ── Criação (cliente logado) ──────────────────────────────────

    async def test_create_order_customer_match(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()

        order = await create_order(
            OrderCreate(items=[OrderItemIn(sku="ABC-123", qty=1)]),
            customer_id=customer_id,
        )

        assert order.customer_id == customer_id
        assert order.guest_email == "cliente@example.com"
        assert order.whatsapp == "11999990000"

    async def test_create_order_customer_email_mismatch_400(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="ABC-123", qty=1)],
                    guest_email="outro@example.com",
                ),
                customer_id=customer_id,
            )
        assert exc.value.status_code == 400
        assert "Email n" in str(exc.value.detail)

    async def test_create_order_customer_phone_mismatch_400(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()
        with pytest.raises(HTTPException) as exc:
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="ABC-123", qty=1)],
                    guest_phone="11900000000",
                ),
                customer_id=customer_id,
            )
        assert exc.value.status_code == 400
        assert "Telefone n" in str(exc.value.detail)

    # ── Consulta ──────────────────────────────────────────────────

    async def test_get_order_owner_sees(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()
        created = await create_order(
            OrderCreate(items=[OrderItemIn(sku="ABC-123", qty=1)]),
            customer_id=customer_id,
        )

        fetched = await get_order(
            created.id,
            viewer=("customer", customer_id),
        )
        assert fetched.id == created.id

    async def test_get_order_other_customer_404(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()
        created = await create_order(
            OrderCreate(items=[OrderItemIn(sku="ABC-123", qty=1)]),
            customer_id=customer_id,
        )

        with pytest.raises(HTTPException) as exc:
            await get_order(created.id, viewer=("customer", 99999))
        assert exc.value.status_code == 404

    async def test_get_order_admin_sees(self):
        await self._seed_product(stock=10)
        customer_id, _ = await self._register_customer()
        created = await create_order(
            OrderCreate(items=[OrderItemIn(sku="ABC-123", qty=1)]),
            customer_id=customer_id,
        )

        fetched = await get_order(created.id, viewer=("admin", None))
        assert fetched.id == created.id

    async def test_get_guest_order_admin_only(self):
        await self._seed_product(stock=10)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        with pytest.raises(HTTPException) as exc:
            await get_order(created.id, viewer=("customer", 123))
        assert exc.value.status_code == 404

        fetched = await get_order(created.id, viewer=("admin", None))
        assert fetched.id == created.id

    # ── Listagem admin ────────────────────────────────────────────

    async def test_list_admin_orders(self):
        await self._seed_product(stock=100)
        for i in range(3):
            await create_order(
                OrderCreate(
                    items=[OrderItemIn(sku="ABC-123", qty=1)],
                    guest_name=f"Comprador {i}",
                    guest_email=f"comprador{i}@example.com",
                    guest_phone="11999990000",
                    whatsapp="11999990000",
                ),
                customer_id=None,
            )

        orders = await list_admin_orders(status_filter=None, limit=50)
        assert len(orders) == 3

    async def test_list_admin_orders_filter_status(self):
        await self._seed_product(stock=100)
        await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        pending = await list_admin_orders(status_filter=OrderStatus.PENDING, limit=50)
        assert len(pending) == 1
        confirmed = await list_admin_orders(status_filter=OrderStatus.CONFIRMED, limit=50)
        assert len(confirmed) == 0

    # ── Atualização de status ─────────────────────────────────────

    async def test_confirm_order_reserves_stock(self):
        await self._seed_product(stock=10, price=29.90)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=2)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )

        updated = await update_order_status(
            created.id, OrderStatusUpdate(status=OrderStatus.CONFIRMED)
        )
        assert updated.status == OrderStatus.CONFIRMED.value

        # Estoque debitado
        async with async_session_factory() as s:
            from sqlalchemy import select

            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            assert p.stock == 8

            res = (await s.execute(
                select(InventoryState).where(InventoryState.order_id == str(created.id))
            )).scalars().all()
            assert len(res) == 1
            assert res[0].channel == "loja"
            assert res[0].state == "reserved"

    async def test_confirm_insufficient_stock_cancels(self):
        await self._seed_product(stock=5)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=5)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )

        # Esgota o estoque DEPOIS do pedido (simula venda paralela no OSPOS)
        async with async_session_factory() as s:
            from sqlalchemy import select

            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            p.stock = 0
            await s.commit()

        updated = await update_order_status(
            created.id, OrderStatusUpdate(status=OrderStatus.CONFIRMED)
        )
        assert updated.status == OrderStatus.CANCELLED.value
        assert "Insufficient stock" in (updated.cancelled_reason or "")

    async def test_double_confirm_409(self):
        await self._seed_product(stock=10)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        await update_order_status(created.id, OrderStatusUpdate(status=OrderStatus.CONFIRMED))
        with pytest.raises(HTTPException) as exc:
            await update_order_status(created.id, OrderStatusUpdate(status=OrderStatus.CONFIRMED))
        assert exc.value.status_code == 409

    async def test_body_pending_400(self):
        await self._seed_product(stock=10)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        with pytest.raises(HTTPException) as exc:
            await update_order_status(created.id, OrderStatusUpdate(status=OrderStatus.PENDING))
        assert exc.value.status_code == 400

    async def test_cancel_with_reason(self):
        await self._seed_product(stock=10)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        updated = await update_order_status(
            created.id, OrderStatusUpdate(status=OrderStatus.CANCELLED, reason="Cliente desistiu")
        )
        assert updated.status == OrderStatus.CANCELLED.value
        assert updated.cancelled_reason == "Cliente desistiu"

    async def test_cancel_default_reason(self):
        await self._seed_product(stock=10)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="ABC-123", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )
        updated = await update_order_status(
            created.id, OrderStatusUpdate(status=OrderStatus.CANCELLED)
        )
        assert updated.status == OrderStatus.CANCELLED.value
        assert updated.cancelled_reason == "Cancelado pelo lojista"

    async def test_confirm_rollback_partial_multi_item(self):
        await self._seed_product(sku="AAA-1", stock=10)
        await self._seed_product(sku="BBB-2", stock=5)
        created = await create_order(
            OrderCreate(
                items=[OrderItemIn(sku="AAA-1", qty=1), OrderItemIn(sku="BBB-2", qty=1)],
                guest_name="Ana",
                guest_email="ana@example.com",
                guest_phone="11999990000",
                whatsapp="11999990000",
            ),
            customer_id=None,
        )

        # Esgota o 2º item DEPOIS do pedido → reserva do 1º deve ser revertida
        async with async_session_factory() as s:
            from sqlalchemy import select

            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "BBB-2"))).scalar_one()
            p.stock = 0
            await s.commit()

        updated = await update_order_status(
            created.id, OrderStatusUpdate(status=OrderStatus.CONFIRMED)
        )
        assert updated.status == OrderStatus.CANCELLED.value

        # Reserva do primeiro item foi revertida (soft-cancel) → estoque restaurado
        async with async_session_factory() as s:
            from sqlalchemy import select

            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "AAA-1"))).scalar_one()
            assert p.stock == 10
            res = (await s.execute(
                select(InventoryState).where(InventoryState.order_id == str(created.id))
            )).scalars().all()
            assert len(res) == 1
            assert res[0].state == "cancelled"


from app.database import async_session_factory  # noqa: E402