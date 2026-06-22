"""
Testes do EventStore Processor — State Machine + Worker Loop

Cobre:
  - Transições válidas e inválidas da state machine
  - Processamento de evento bem-sucedido
  - Processamento com falha e retry
  - Exaustão de retries → DEAD
  - Múltiplos canais (parcial)
  - Criação de eventos
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.models.event_store import EventStore
from app.services.event_processor import (
    EventStoreProcessor,
    InvalidTransition,
    create_event,
    validate_transition,
)


# ─── Tests: State Machine ─────────────────────────────────────────────────

class TestStateMachine:
    """Valida as regras de transição de estado."""

    def test_valid_pending_to_processing(self):
        """PENDING pode ir para PROCESSING."""
        validate_transition("evt-1", "pending", "processing")  # não levanta

    def test_valid_processing_to_completed(self):
        validate_transition("evt-1", "processing", "completed")

    def test_valid_processing_to_failed(self):
        validate_transition("evt-1", "processing", "failed")

    def test_valid_processing_to_partial(self):
        validate_transition("evt-1", "processing", "partial")

    def test_valid_failed_to_processing_retry(self):
        validate_transition("evt-1", "failed", "processing")

    def test_valid_failed_to_dead(self):
        validate_transition("evt-1", "failed", "dead")

    def test_valid_partial_to_processing(self):
        validate_transition("evt-1", "partial", "processing")

    def test_valid_dead_to_pending_manual(self):
        """DEAD pode voltar pra PENDING só por ação manual."""
        validate_transition("evt-1", "dead", "pending")

    # --- Transições inválidas ---

    def test_invalid_completed_to_anything(self):
        """COMPLETED é terminal — não pode transicionar."""
        for target in ("processing", "failed", "dead"):
            with pytest.raises(InvalidTransition, match="não é permitido"):
                validate_transition("evt-1", "completed", target)

    def test_invalid_pending_to_completed(self):
        """PENDING não pode pular direto pra COMPLETED."""
        with pytest.raises(InvalidTransition):
            validate_transition("evt-1", "pending", "completed")

    def test_invalid_pending_to_dead(self):
        with pytest.raises(InvalidTransition):
            validate_transition("evt-1", "pending", "dead")


# ─── Tests: Criação de Eventos ────────────────────────────────────────────

class TestCreateEvent:
    """Testa a função auxiliar create_event."""

    def test_create_stock_event(self):
        ev = create_event(
            "stock.updated",
            {"sku": "ABC-123", "quantity": 5},
            sku="ABC-123",
            channel="woocommerce",
        )

        assert ev.event_type == "stock.updated"
        assert ev.state == "pending"
        assert ev.sku == "ABC-123"
        assert ev.channel == "woocommerce"
        assert ev.retry_count == 0
        assert ev.max_retries == 5
        assert isinstance(ev.id, str) and len(ev.id) > 0
        assert json.loads(ev.payload) == {"sku": "ABC-123", "quantity": 5}

    def test_create_event_defaults(self):
        ev = create_event("product.created", {"name": "Produto X"})
        assert ev.channel is None
        assert ev.sku is None


# ─── Tests: Processor ─────────────────────────────────────────────────────

class TestEventStoreProcessor:
    """Testa o processador com banco SQLite real (em memória)."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        """Recria tabelas antes de cada teste."""
        from app.database import Base, engine

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture
    async def session(self):
        from app.database import async_session_factory

        async with async_session_factory() as s:
            yield s

    @pytest.fixture
    def processor(self, registry):
        return EventStoreProcessor(registry, batch_size=10)

    async def _insert_event(self, session, **overrides) -> EventStore:
        """Helper para inserir evento no banco."""
        ev = create_event(
            event_type=overrides.get("event_type", "stock.updated"),
            payload=overrides.get(
                "payload", {"sku": "ABC-123", "quantity": 10}
            ),
            sku=overrides.get("sku", "ABC-123"),
            channel=overrides.get("channel", "woocommerce"),
        )
        # Aplica overrides no estado, retry_count, etc
        for k, v in overrides.items():
            if k in ("event_type", "payload", "sku", "channel"):
                continue
            setattr(ev, k, v)
        session.add(ev)
        await session.commit()
        return ev

    # ── Fluxo feliz ─────────────────────────────────────────────────

    async def test_process_pending_event_success(self, processor):
        """Evento PENDING é processado e vai pra COMPLETED."""
        from app.database import async_session_factory

        async with async_session_factory() as session:
            await self._insert_event(session)

        results = await processor.run_once()

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].new_state == "completed"

        # Verifica no banco
        async with async_session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(EventStore))
            ev = result.scalar_one()
            assert ev.state == "completed"

    async def test_multiple_events_batch(self, processor):
        """Vários eventos são processados em lote."""
        from app.database import async_session_factory

        async with async_session_factory() as session:
            for i in range(3):
                await self._insert_event(
                    session,
                    payload={"sku": f"SKU-{i}", "quantity": i * 10},
                )

        results = await processor.run_once()

        assert len(results) == 3
        assert all(r.success for r in results)
        assert all(r.new_state == "completed" for r in results)

    # ── Falhas e retries ────────────────────────────────────────────

    async def test_failed_event_goes_to_failed(self, failing_registry):
        """Adapter falha → evento vai pra FAILED."""
        processor = EventStoreProcessor(failing_registry, batch_size=10)

        from app.database import async_session_factory

        async with async_session_factory() as session:
            await self._insert_event(session)

        results = await processor.run_once()

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].new_state == "failed"

    async def test_retry_exhaustion_goes_to_dead(self, failing_registry):
        """Depois de max_retries falhas → evento vai pra DEAD."""
        processor = EventStoreProcessor(failing_registry, batch_size=10)

        from app.database import async_session_factory

        async with async_session_factory() as session:
            ev = await self._insert_event(
                session,
                retry_count=4,  # 4 falhas já, max=5 → próxima falha exaure
                max_retries=5,
                state="failed",
                updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )

        results = await processor.run_once()

        # Deve pegar o FAILED (backoff respeitado) e processar
        # Como o adapter falha, incrementa retry para 5 = max → DEAD
        assert len(results) >= 1
        assert results[0].new_state == "dead"

    async def test_partial_state_with_two_channels(
        self, registry, failing_registry
    ):
        """Um canal funciona, outro falha → PARTIAL."""
        from app.adapters.registry import AdapterRegistry

        # Registry com 2 canais: um bom, um ruim
        mixed_registry = AdapterRegistry()
        from tests.conftest import FakeAdapter

        mixed_registry.register(FakeAdapter("woocommerce", should_fail=False))
        mixed_registry.register(FakeAdapter("shopee", should_fail=True))

        processor = EventStoreProcessor(mixed_registry, batch_size=10)

        from app.database import async_session_factory

        async with async_session_factory() as session:
            await self._insert_event(session, channel=None)

        results = await processor.run_once()

        assert len(results) == 1
        assert results[0].success is True  # parcial ainda é "sucesso" parcial
        assert results[0].new_state == "partial"
        assert results[0].channels_ok == ["woocommerce"]
        assert results[0].channels_fail == ["shopee"]

    # ── Evento desconhecido ─────────────────────────────────────────

    async def test_unknown_event_type_goes_to_dead(self, processor):
        """Tipo de evento não mapeado → DEAD."""
        from app.database import async_session_factory

        async with async_session_factory() as session:
            await self._insert_event(
                session, event_type="unknown.type", payload={}
            )

        results = await processor.run_once()

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].new_state == "dead"
        assert "desconhecido" in (results[0].error or "")

    # ── Sem eventos ─────────────────────────────────────────────────

    async def test_no_pending_events(self, processor):
        """Sem eventos pendentes → lista vazia."""
        results = await processor.run_once()
        assert results == []

    # ── Fetch de FAILED com backoff ─────────────────────────────────

    async def test_failed_event_respects_backoff(self, failing_registry):
        """Evento FAILED só é pego depois do backoff."""
        processor = EventStoreProcessor(failing_registry, batch_size=10)

        from app.database import async_session_factory

        async with async_session_factory() as session:
            ev = await self._insert_event(
                session,
                state="failed",
                retry_count=1,
                updated_at=datetime.now(timezone.utc),  # agora → backoff não passou
            )

        results = await processor.run_once()

        # Não deve pegar o evento — backoff de 2^1*10 = 20s não passou
        assert len(results) == 0
