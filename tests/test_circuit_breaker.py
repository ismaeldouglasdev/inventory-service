"""Testes do Circuit Breaker — state machine CLOSED/OPEN/HALF_OPEN.

Cobre:
  - CLOSED → OPEN (threshold de falhas)
  - OPEN → HALF_OPEN (cooldown)
  - HALF_OPEN → CLOSED (sucesso)
  - HALF_OPEN → OPEN (falha)
  - Context guard (sucesso e falha)
  - ProcessedAction registrado
"""

from __future__ import annotations

import pytest


class TestCircuitBreaker:
    """Testa o circuit breaker com SQLite em memória."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from app.database import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture
    def cb(self):
        from app.services.circuit_breaker import CircuitBreaker
        return CircuitBreaker(threshold=3, cooldown=1)  # 1s cooldown para testes

    # ── CLOSED → OPEN ────────────────────────────────────────────

    async def test_closed_allows_requests(self, cb):
        """CLOSED → allow_request retorna True."""
        assert await cb.allow_request("shopee") is True

    async def test_failures_open_circuit(self, cb):
        """N failures consecutivos → OPEN."""
        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        assert await cb.allow_request("shopee") is False

    async def test_success_resets_failure_count(self, cb):
        """Um sucesso reseta o contador de falhas."""
        await cb.on_failure("shopee", "error 1")
        await cb.on_failure("shopee", "error 2")
        await cb.on_success("shopee")  # reseta

        # Ainda permite (CLOSED)
        assert await cb.allow_request("shopee") is True

        # Mais 3 falhas para abrir
        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        assert await cb.allow_request("shopee") is False

    # ── OPEN → HALF_OPEN → CLOSED ───────────────────────────────

    async def test_open_after_cooldown_becomes_half_open(self, cb):
        """Depois do cooldown, OPEN → HALF_OPEN e permite request."""
        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        # OPEN agora
        assert await cb.allow_request("shopee") is False

        # Espera cooldown (1s)
        import asyncio
        await asyncio.sleep(1.1)

        # Deve transicionar para HALF_OPEN e permitir
        assert await cb.allow_request("shopee") is True

    async def test_half_open_success_becomes_closed(self, cb):
        """HALF_OPEN + sucesso → CLOSED."""
        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        import asyncio
        await asyncio.sleep(1.1)

        # HALF_OPEN
        await cb.allow_request("shopee")

        # Um sucesso deve fechar o circuito
        await cb.on_success("shopee")
        assert await cb.allow_request("shopee") is True

    async def test_half_open_failure_becomes_open(self, cb):
        """HALF_OPEN + falha → OPEN novamente."""
        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        import asyncio
        await asyncio.sleep(1.1)

        # HALF_OPEN
        await cb.allow_request("shopee")

        # Falha no HALF_OPEN → volta pra OPEN
        await cb.on_failure("shopee", "half-open failure")
        assert await cb.allow_request("shopee") is False

    # ── Context guard ───────────────────────────────────────────

    async def test_guard_success(self, cb):
        """Context guard com sucesso → registra ProcessedAction."""
        async with cb.guard("shopee", event_id="evt-guard", action_type="update_stock"):
            pass  # sucesso

        assert await cb.allow_request("shopee") is True

        # Verifica ProcessedAction
        from app.database import async_session_factory
        from sqlalchemy import select
        from app.models.processed_action import ProcessedAction

        async with async_session_factory() as s:
            result = await s.execute(
                select(ProcessedAction).where(
                    ProcessedAction.event_id == "evt-guard"
                )
            )
            action = result.scalar_one_or_none()
            assert action is not None
            assert action.status == "ok"
            assert action.target_system == "shopee"
            assert action.action_type == "update_stock"

    async def test_guard_failure(self, cb):
        """Context guard com exceção → registra falha + incrementa contador."""
        class SimulatedError(Exception):
            pass

        with pytest.raises(SimulatedError):
            async with cb.guard("shopee", event_id="evt-fail", action_type="update_stock"):
                raise SimulatedError("API error")

        # Circuito deve ter 1 falha
        from app.database import async_session_factory
        from sqlalchemy import select
        from app.models.channel_state import ChannelState

        async with async_session_factory() as s:
            result = await s.execute(
                select(ChannelState).where(ChannelState.channel == "shopee")
            )
            state = result.scalar_one()
            assert state.failure_count == 1

    async def test_guard_rejects_when_open(self, cb):
        """Context guard levanta CircuitBreakerOpenError quando OPEN."""
        from app.services.circuit_breaker import CircuitBreakerOpenError

        for i in range(3):
            await cb.on_failure("shopee", f"error {i}")

        with pytest.raises(CircuitBreakerOpenError):
            async with cb.guard("shopee"):
                pass  # não deve chegar aqui
