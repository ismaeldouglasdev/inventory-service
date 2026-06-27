"""
EventStore Processor — State Machine + Worker Loop

Fluxo:
  1. Worker loop polls EventStore por eventos PENDING (ou FAILED com retry)
  2. Para cada evento, executa o adapter correspondente
  3. Atualiza o estado conforme o resultado (state machine)
  4. Se falhou, programa retry com exponential backoff
  5. Se exauriu retries, marca como DEAD (intervenção manual)

State Machine:
  PENDING  ──(processar)──►  PROCESSING
  PROCESSING ──(sucesso)──►  COMPLETED    (todos adapters OK)
  PROCESSING ──(falha)───►  FAILED        (tentativas restantes)
  PROCESSING ──(parcial)─►  PARTIAL       (alguns falharam)
  FAILED    ──(retentar)─►  PROCESSING    (ainda há retries)
  FAILED    ──(exaurido)─►  DEAD          (max_retries atingido)
  PARTIAL   ──(retentar)─►  PROCESSING
  DEAD      ──( manual )─►  PENDING       (só por ação manual)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import MarketplaceAdapter
from app.adapters.registry import AdapterRegistry, AdapterRegistryError
from app.database import async_session_factory
from app.models.event_store import EventStore
from app.services.channel_dispatcher import ChannelDispatcher

logger = logging.getLogger(__name__)


# ─── State Machine ────────────────────────────────────────────────────────

class InvalidTransition(Exception):
    """Raised when an event tries to move to an illegal state."""

    def __init__(self, event_id: str, current: str, target: str) -> None:
        self.event_id = event_id
        self.current = current
        self.target = target
        super().__init__(f"Event {event_id}: {current!r} → {target!r} não é permitido")


# Mapa de transições válidas: estado_atual → {estados_destino_válidos}
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending":    {"processing"},
    "processing": {"completed", "failed", "partial", "dead"},
    "failed":     {"processing", "dead"},
    "partial":    {"processing", "completed"},
    "completed":  set(),      # terminal — não sai pra lugar nenhum
    "dead":       {"pending"}, # só volta por intervenção manual
}


def validate_transition(event_id: str, current_state: str, target_state: str) -> None:
    """Valida se a transição é permitida pela state machine.

    Lança InvalidTransition se for ilegal.
    """
    permitted = VALID_TRANSITIONS.get(current_state, set())
    if target_state not in permitted:
        raise InvalidTransition(event_id, current_state, target_state)


# ─── Tipos de evento ──────────────────────────────────────────────────────

EVENT_HANDLERS: dict[str, str] = {
    "stock.updated":   "update_stock",
    "price.updated":   "update_price",
    "product.created": "publish_product",
}


# ─── Processor ────────────────────────────────────────────────────────────

@dataclass
class ProcessResult:
    """Resultado do processamento de um único evento."""
    event_id: str
    success: bool
    new_state: str
    error: Optional[str] = None
    channels_ok: list[str] | None = None
    channels_fail: list[str] | None = None


class EventStoreProcessor:
    """Processa eventos do EventStore usando os adapters registrados.

    Uso:
        registry = AdapterRegistry()
        registry.register(woo_adapter)
        processor = EventStoreProcessor(registry)
        await processor.run_once()   # processa lote e volta
        await processor.run_forever() # loop infinito
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        batch_size: int = 10,
        poll_interval: float = 5.0,
        dispatcher: ChannelDispatcher | None = None,
    ) -> None:
        self.registry = registry
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher = dispatcher or ChannelDispatcher(registry)

    # ── API pública ───────────────────────────────────────────────────

    async def run_once(self) -> list[ProcessResult]:
        """Processa um lote de eventos pendentes e retorna os resultados.

        Método principal: pega eventos, processa, atualiza estados.
        """
        results: list[ProcessResult] = []

        async with async_session_factory() as session:
            events = await self._fetch_pending(session)

            if not events:
                logger.debug("Nenhum evento pendente encontrado")
                return results

            logger.info("Processando %d evento(s)", len(events))

            for event in events:
                result = await self._process_event(session, event)
                results.append(result)

            await session.commit()

        return results

    async def run_forever(self) -> None:
        """Loop infinito: processa eventos em background.

        Roda até alguém chamar ``stop()``.
        """
        self._running = True
        logger.info(
            "EventStoreProcessor iniciado (poll_interval=%ds, batch_size=%d)",
            self.poll_interval,
            self.batch_size,
        )

        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Erro no ciclo de processamento")
            await asyncio.sleep(self.poll_interval)

        logger.info("EventStoreProcessor parou")

    def stop(self) -> None:
        """Sinaliza pro loop ``run_forever`` parar."""
        self._running = False

    # ── Internals ─────────────────────────────────────────────────────

    async def _fetch_pending(self, session: AsyncSession) -> list[EventStore]:
        """Busca eventos prontos para processar.

        Regras:
          1. PENDING → ready imediatamente
          2. FAILED → ready se retry_count < max_retries
             (usa exponential backoff: espera 2^retry_count * 10s)
          3. PARTIAL → ready (tenta de novo os canais que falharam)
          4. COMPLETED / DEAD → ignorados
        """
        # SQLite não armazena timezone → usamos naive UTC pra comparar com o banco
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Eventos PENDING
        stmt_pending = (
            select(EventStore)
            .where(EventStore.state == "pending")
            .order_by(EventStore.created_at.asc())
            .limit(self.batch_size)
        )
        result = await session.execute(stmt_pending)
        events = list(result.scalars().all())

        # Eventos FAILED com retry pendente + backoff respeitado
        stmt_failed = (
            select(EventStore)
            .where(
                EventStore.state == "failed",
                EventStore.retry_count < EventStore.max_retries,
            )
            .order_by(EventStore.updated_at.asc())
            .limit(self.batch_size)
        )
        result = await session.execute(stmt_failed)
        for ev in result.scalars().all():
            # Exponential backoff: 2^retry_count * 10 segundos
            wait_seconds = (2 ** ev.retry_count) * 10
            if (now - ev.updated_at).total_seconds() >= wait_seconds:
                events.append(ev)

        # Eventos PARTIAL — podem ser reprocessados imediatamente
        stmt_partial = (
            select(EventStore)
            .where(EventStore.state == "partial")
            .order_by(EventStore.updated_at.asc())
            .limit(self.batch_size)
        )
        result = await session.execute(stmt_partial)
        events.extend(result.scalars().all())

        return events

    async def _transition(
        self,
        session: AsyncSession,
        event: EventStore,
        target_state: str,
        *,
        retry: bool = False,
        error: str | None = None,
    ) -> str:
        """Aplica uma transição de estado validada no banco.

        Se a transição for para 'failed' e ainda há retries,
        o evento volta como 'pending' após o backoff (controlled
        pelo fetch).

        Se 'failed' e retries exauridos → 'dead'.

        Retorna o nome do estado final (pode ser diferente de
        *target_state* quando há upgrade para ``dead``).
        """
        validate_transition(event.id, event.state, target_state)

        new_state = target_state
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        update_data: dict[str, Any] = {
            "state": new_state,
            "updated_at": now,
        }

        if target_state == "failed" and not retry:
            update_data["retry_count"] = EventStore.retry_count + 1

        if target_state == "failed" and event.retry_count + 1 >= event.max_retries:
            new_state = "dead"
            update_data["state"] = "dead"

        logger.info(
            "Evento %s: %s → %s%s",
            event.id,
            event.state,
            new_state,
            f" (erro: {error[:80]})" if error else "",
        )

        stmt = (
            update(EventStore)
            .where(EventStore.id == event.id)
            .values(**update_data)
        )
        await session.execute(stmt)
        return new_state

    async def _process_event(
        self,
        session: AsyncSession,
        event: EventStore,
    ) -> ProcessResult:
        """Processa UM evento: executa adapter(s) e atualiza estado."""
        event_id = event.id
        payload = json.loads(event.payload)

        logger.debug("Processando evento %s: %s", event_id, event.event_type)

        # 1. Marca como PROCESSING
        await self._transition(session, event, "processing")

        # 2. Descobre qual adapter e método chamar
        handler_name = EVENT_HANDLERS.get(event.event_type)
        if handler_name is None:
            logger.warning(
                "Evento %s: tipo %s não reconhecido, marcando como dead",
                event_id,
                event.event_type,
            )
            await self._transition(session, event, "dead", error="unknown_event_type")
            return ProcessResult(
                event_id=event_id,
                success=False,
                new_state="dead",
                error=f"Tipo de evento desconhecido: {event.event_type}",
            )

        # 3. Determina quais canais processar (com dispatcher: prioridade + rate + CB + buffer)
        channels_to_process = await self._resolve_channels_async(event)

        if not channels_to_process:
            logger.warning(
                "Evento %s: nenhum adapter disponível para canal %s",
                event_id,
                event.channel or "any",
            )
            await self._transition(session, event, "dead", error="no_adapters")
            return ProcessResult(
                event_id=event_id,
                success=False,
                new_state="dead",
                error="Nenhum adapter disponível",
            )

        # 4. Executa o handler em cada canal
        channels_ok: list[str] = []
        channels_fail: list[str] = []

        for channel in channels_to_process:
            try:
                adapter = self.registry.get(channel)
                handler = getattr(adapter, handler_name, None)
                if handler is None:
                    logger.warning(
                        "Adapter %s não implementa %s", channel, handler_name
                    )
                    channels_fail.append(channel)
                    continue

                success = await self._call_handler(handler, payload, event.event_type)
                if success:
                    channels_ok.append(channel)
                else:
                    channels_fail.append(channel)

            except AdapterRegistryError:
                logger.warning("Canal %s não registrado", channel)
                channels_fail.append(channel)
            except Exception:
                logger.exception("Erro no adapter %s para evento %s", channel, event_id)
                channels_fail.append(channel)

        # 5. Determina o novo estado baseado nos resultados
        if not channels_fail:
            new_state = "completed"
            success = True
            error = None
        elif not channels_ok:
            new_state = "failed"
            success = False
            error = "Todos os canais falharam"
        else:
            new_state = "partial"
            success = True
            error = f"Canais com falha: {', '.join(channels_fail)}"

        new_state = await self._transition(session, event, new_state, error=error)

        return ProcessResult(
            event_id=event_id,
            success=success,
            new_state=new_state,
            error=error,
            channels_ok=channels_ok or None,
            channels_fail=channels_fail or None,
        )

    async def _resolve_channels_async(self, event: EventStore) -> list[str]:
        """Async channel resolution via dispatcher (rate-limit, CB, priority, buffer)."""
        if event.state == "partial":
            payload = json.loads(event.payload)
            failed = payload.get("_failed_channels", [])
            if failed:
                return failed

        if event.channel:
            return [event.channel]

        payload = json.loads(event.payload)
        stock = payload.get("quantity") if event.event_type == "stock.updated" else None
        return await self._dispatcher.resolve(
            event.event_type, sku=event.sku, stock=stock,
        )

    def _resolve_channels(self, event: EventStore) -> list[str]:
        """Sync fallback — priority ordering only (no rate/CB/buffer)."""
        if event.state == "partial":
            payload = json.loads(event.payload)
            failed = payload.get("_failed_channels", [])
            if failed:
                return failed
        if event.channel:
            return [event.channel]
        return sorted(
            self.registry.channel_names(),
            key=lambda ch: (
                ["mercadolivre", "shopee", "woocommerce"].index(ch)
                if ch in ["mercadolivre", "shopee", "woocommerce"]
                else 999
            ),
        )

    async def _call_handler(
        self,
        handler: Any,
        payload: dict[str, Any],
        event_type: str,
    ) -> bool:
        """Chama o método do adapter com os parâmetros certos."""
        if event_type == "stock.updated":
            return bool(await handler(sku=payload["sku"], quantity=payload["quantity"]))
        elif event_type == "price.updated":
            return bool(await handler(sku=payload["sku"], price=payload["price"]))
        elif event_type == "product.created":
            await handler(product=payload)
            return True
        else:
            logger.warning("Handler %s chamado com tipo não mapeado: %s", handler, event_type)
            return False


# ─── Helpers ──────────────────────────────────────────────────────────────

def create_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    sku: str | None = None,
    channel: str | None = None,
) -> EventStore:
    """Cria um novo evento no EventStore (ainda não persistido).

    Uso:
        async with async_session_factory() as session:
            ev = create_event("stock.updated", {"sku": "ABC", "quantity": 5})
            session.add(ev)
            await session.commit()
    """
    now = datetime.now(timezone.utc)
    return EventStore(
        id=str(uuid4()),
        event_type=event_type,
        payload=json.dumps(payload),
        sku=sku,
        channel=channel,
        state="pending",
        retry_count=0,
        max_retries=5,
        created_at=now,
        updated_at=now,
    )
