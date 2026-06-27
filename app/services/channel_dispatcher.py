"""Channel Dispatcher — resolve a ordem e regras de propagação para canais.

Prioridade (definida no plano v3.1 §11):
  1. Loja física (OSPOS)
  2. Mercado Livre
  3. Shopee
  4. WooCommerce direto

Stock buffer: cada canal pode ter um buffer de segurança (ex: nunca
deixar o estoque virtual chegar a zero para aquele canal).

Uso::

    dispatcher = ChannelDispatcher(registry, limiter)
    channels = await dispatcher.resolve("stock.updated", sku="ABC-123")
    for ch in channels:
        await dispatch_to(ch)
"""

from __future__ import annotations

import logging
from typing import Any

from app.adapters.registry import AdapterRegistry
from app.models.channel_state import ChannelState
from app.services.circuit_breaker import CircuitBreaker
from app.services.rate_limiter import RateLimiter, TokenBucketRateLimiter

logger = logging.getLogger(__name__)

# Ordem de prioridade dos canais
CHANNEL_PRIORITY = ["mercadolivre", "shopee", "woocommerce"]


class ChannelDispatcher:
    """Resolves which channels should receive an event, in priority order.

    Applays:
    - Priority ordering (ML → Shopee → WooCommerce)
    - Circuit breaker status (skip OPEN channels)
    - Rate limiting
    - Stock buffer (only propagate if stock > buffer for that channel)
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.registry = registry
        self.limiter = rate_limiter or TokenBucketRateLimiter()
        self.cb = circuit_breaker or CircuitBreaker()

    async def resolve(
        self,
        event_type: str,
        *,
        sku: str | None = None,
        stock: int | None = None,
    ) -> list[str]:
        """Return the list of channels that should receive this event,
        ordered by priority and filtered by availability.

        Args:
            event_type: Type of event (stock.updated, price.updated, etc.)
            sku: Product SKU (for stock buffer checks)
            stock: Current stock quantity (for buffer checks)

        Returns:
            Ordered list of channel names to dispatch to.
        """
        channels = self._get_available_channels()
        if not channels:
            return []

        result: list[str] = []

        for ch in channels:
            # Circuit breaker check
            if not await self.cb.allow_request(ch):
                logger.info("Dispatcher: skipping %s — circuit OPEN", ch)
                continue

            # Rate limiter check
            if not await self.limiter.acquire(ch):
                logger.info("Dispatcher: skipping %s — rate limited", ch)
                continue

            # Stock buffer check (only for stock events)
            if event_type in ("stock.updated",) and stock is not None:
                buffer = await self._get_stock_buffer(ch)
                if stock <= buffer:
                    logger.info(
                        "Dispatcher: skipping %s — stock %d <= buffer %d",
                        ch, stock, buffer,
                    )
                    continue

            result.append(ch)

        return result

    async def should_buffer(self, channel: str, stock: int) -> bool:
        """Check if a channel's stock buffer prevents propagation."""
        buffer = await self._get_stock_buffer(channel)
        return stock <= buffer

    # ── Internal helpers ──────────────────────────────────────────────

    def _get_available_channels(self) -> list[str]:
        """Return registered channels sorted by priority."""
        registered = self.registry.channel_names()
        # Sort by priority order, putting unknown channels at the end
        return sorted(
            registered,
            key=lambda ch: CHANNEL_PRIORITY.index(ch) if ch in CHANNEL_PRIORITY else 999,
        )

    async def _get_stock_buffer(self, channel: str) -> int:
        """Get the stock buffer for a channel from ChannelState."""
        from sqlalchemy import select
        from app.database import async_session_factory

        async with async_session_factory() as session:
            result = await session.execute(
                select(ChannelState.stock_buffer).where(
                    ChannelState.channel == channel
                )
            )
            val = result.scalar_one_or_none()
            return val or 0
