"""Testes do Sell Pipeline — Inventory State lifecycle + idempotência.

Cobre:
  - Reserve (sucesso, duplicata, estoque insuficiente)
  - Confirm (reserved → confirmed)
  - Commit (confirmed → committed)
  - Cancel (restaura estoque)
  - Full sell flow (reserve → confirm → propagate → commit)
  - Idempotência via ProcessedAction
"""

from __future__ import annotations

import pytest

from app.models.inventory_state import InventoryState
from app.models.store_product import StoreProduct
from app.models.processed_action import ProcessedAction
from app.services.sell_pipeline import SellPipeline


class TestSellPipeline:
    """Testa o pipeline de venda com SQLite em memória."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        """Recria tabelas antes de cada teste."""
        from app.database import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture
    def pipeline(self, registry):
        return SellPipeline(registry)

    async def _seed_product(
        self, sku: str = "ABC-123", stock: int = 10, price: float = 29.90
    ) -> StoreProduct:
        """Helper: insere um produto na tabela store_products."""
        from app.database import async_session_factory
        from datetime import datetime, timezone

        async with async_session_factory() as session:
            p = StoreProduct(
                ospos_id=1,
                sku=sku,
                name="Produto Teste",
                description="",
                price=price,
                category="teste",
                stock=stock,
                store_visible=True,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(p)
            await session.commit()
            return p

    # ── Reserve ────────────────────────────────────────────────────

    async def test_reserve_success(self, pipeline):
        """Reserva de estoque com dados válidos."""
        await self._seed_product(stock=10)

        result = await pipeline.reserve(
            sku="ABC-123",
            quantity=2,
            unit_price=29.90,
            channel="woocommerce",
            order_id="order-001",
        )

        assert result["state"] == "reserved"
        assert result["sku"] == "ABC-123"
        assert result["quantity"] == 2
        assert result["total"] == 59.80
        assert result["order_id"] == "order-001"
        assert result["channel"] == "woocommerce"
        assert result["id"] > 0

        # Estoque foi decrementado
        from app.database import async_session_factory
        from sqlalchemy import select

        async with async_session_factory() as s:
            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            assert p.stock == 8  # 10 - 2

    async def test_reserve_duplicate(self, pipeline):
        """Mesmo order_id + SKU não cria segunda reserva."""
        await self._seed_product(stock=10)

        r1 = await pipeline.reserve(
            sku="ABC-123", quantity=2, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )
        r2 = await pipeline.reserve(
            sku="ABC-123", quantity=2, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )

        assert r1["id"] == r2["id"]  # mesma reserva

        # Estoque debitado só uma vez
        from app.database import async_session_factory
        from sqlalchemy import select

        async with async_session_factory() as s:
            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            assert p.stock == 8

    async def test_reserve_insufficient_stock(self, pipeline):
        """Estoque insuficiente levanta erro."""
        await self._seed_product(stock=2)

        with pytest.raises(ValueError, match="Insufficient stock"):
            await pipeline.reserve(
                sku="ABC-123", quantity=5, unit_price=29.90,
                channel="woocommerce", order_id="order-001",
            )

    async def test_reserve_product_not_found(self, pipeline):
        """SKU inexistente levanta erro."""
        with pytest.raises(ValueError, match="not found"):
            await pipeline.reserve(
                sku="NONEXIST", quantity=1, unit_price=10.0,
                channel="woocommerce", order_id="order-001",
            )

    # ── Confirm ───────────────────────────────────────────────────

    async def test_confirm_success(self, pipeline):
        """Reserva confirmada → state = confirmed."""
        await self._seed_product(stock=10)
        r = await pipeline.reserve(
            sku="ABC-123", quantity=2, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )

        result = await pipeline.confirm(r["id"], ospos_sale_id="ospos-123")
        assert result["state"] == "confirmed"
        assert result["ospos_sale_id"] == "ospos-123"

    async def test_confirm_not_found(self, pipeline):
        """Confirmar reserva inexistente levanta erro."""
        with pytest.raises(ValueError, match="No RESERVED"):
            await pipeline.confirm(9999)

    # ── Commit ────────────────────────────────────────────────────

    async def test_commit_success(self, pipeline):
        """Reserva confirmada → commit → state = committed."""
        await self._seed_product(stock=10)
        r = await pipeline.reserve(
            sku="ABC-123", quantity=1, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )
        await pipeline.confirm(r["id"])

        result = await pipeline.commit(r["id"])
        assert result["state"] == "committed"

    async def test_commit_not_confirmed(self, pipeline):
        """Reserva em reserved não pode ir pra committed."""
        await self._seed_product(stock=10)
        r = await pipeline.reserve(
            sku="ABC-123", quantity=1, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )

        with pytest.raises(ValueError, match="No CONFIRMED"):
            await pipeline.commit(r["id"])

    # ── Cancel ────────────────────────────────────────────────────

    async def test_cancel_restores_stock(self, pipeline):
        """Cancelar reserva restaura o estoque."""
        await self._seed_product(stock=10)
        r = await pipeline.reserve(
            sku="ABC-123", quantity=3, unit_price=29.90,
            channel="woocommerce", order_id="order-001",
        )

        await pipeline.cancel(r["id"], reason="teste cancelamento")

        # Estoque restaurado
        from app.database import async_session_factory
        from sqlalchemy import select

        async with async_session_factory() as s:
            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            assert p.stock == 10  # voltou ao original

    # ── Full sell ─────────────────────────────────────────────────

    async def test_sell_full_flow(self, pipeline):
        """Fluxo completo: reserve → confirm → propagate → committed."""
        await self._seed_product(stock=10)

        result = await pipeline.sell(
            sku="ABC-123", quantity=1, unit_price=29.90,
            channel="woocommerce", order_id="order-sell-001",
        )

        assert result["state"] == "committed"

        # Estoque debitado
        from app.database import async_session_factory
        from sqlalchemy import select

        async with async_session_factory() as s:
            p = (await s.execute(select(StoreProduct).where(StoreProduct.sku == "ABC-123"))).scalar_one()
            assert p.stock == 9

    async def test_sell_propagation_failure_stays_confirmed(self, pipeline, failing_registry):
        """Se a propagação falha, fica como CONFIRMED (não COMMITTED)."""
        await self._seed_product(stock=10)
        pipeline = SellPipeline(failing_registry)

        result = await pipeline.sell(
            sku="ABC-123", quantity=1, unit_price=29.90,
            channel="woocommerce", order_id="order-fail-001",
        )

        assert result["state"] == "confirmed"

    # ── Idempotency ──────────────────────────────────────────────

    async def test_is_already_processed_false(self, pipeline):
        """Evento não processado → False."""
        result = await pipeline.is_already_processed("evt-001", "woocommerce")
        assert result is False

    async def test_is_already_processed_true(self, pipeline):
        """Evento já processado → True."""
        from app.database import async_session_factory
        from datetime import datetime, timezone

        async with async_session_factory() as s:
            s.add(ProcessedAction(
                event_id="evt-001",
                target_system="woocommerce",
                status="ok",
                action_type="update_stock",
                created_at=datetime.now(timezone.utc),
            ))
            await s.commit()

        result = await pipeline.is_already_processed("evt-001", "woocommerce")
        assert result is True

    # ── Query helpers ─────────────────────────────────────────────

    async def test_get_reservation(self, pipeline):
        """Buscar reserva por ID."""
        await self._seed_product(stock=5)
        r = await pipeline.reserve(
            sku="ABC-123", quantity=1, unit_price=10.0,
            channel="woocommerce", order_id="order-get-001",
        )

        fetched = await pipeline.get_reservation(r["id"])
        assert fetched is not None
        assert fetched["id"] == r["id"]

    async def test_get_reservation_not_found(self, pipeline):
        """Reserva inexistente → None."""
        result = await pipeline.get_reservation(9999)
        assert result is None

    async def test_list_reservations(self, pipeline):
        """Listar reservas com filtros."""
        await self._seed_product(stock=20)
        await pipeline.reserve(
            sku="ABC-123", quantity=1, unit_price=10.0,
            channel="woocommerce", order_id="order-list-001",
        )

        results = await pipeline.list_reservations(sku="ABC-123")
        assert len(results) == 1
        assert results[0]["sku"] == "ABC-123"

    async def test_list_reservations_empty(self, pipeline):
        """Listar sem reservas → lista vazia."""
        results = await pipeline.list_reservations()
        assert results == []
