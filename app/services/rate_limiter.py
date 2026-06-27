"""Rate Limiter — token bucket, desacoplado para Redis no futuro.

Cada canal tem um token bucket: um número fixo de requisições
por intervalo de tempo. Quando os tokens acabam, a requisição
é bloqueada até o próximo refill.

Interface:

    limiter = TokenBucketRateLimiter()
    await limiter.acquire("shopee")       # → True/False
    await limiter.acquire("shopee", 3)    # tenta com 3 tokens
    await limiter.wait_acquire("shopee")  # bloqueia até conseguir

A implementação atual é in-memory. Para produção com múltiplas
instâncias, trocar o backend para Redis mantendo a mesma interface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Configurações default ───────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, dict[str, float]] = {
    "woocommerce":   {"tokens": 60,  "refill_rate": 60.0,   "refill_interval": 60.0},
    "mercadolivre":  {"tokens": 30,  "refill_rate": 30.0,   "refill_interval": 60.0},
    "shopee":        {"tokens": 20,  "refill_rate": 20.0,   "refill_interval": 60.0},
    "default":       {"tokens": 10,  "refill_rate": 10.0,   "refill_interval": 60.0},
}


# ── Interface ───────────────────────────────────────────────────────────

class RateLimiter(ABC):
    """Abstract rate limiter — troque o backend sem mudar o código."""

    @abstractmethod
    async def acquire(self, channel: str, tokens: int = 1) -> bool:
        """Try to acquire *tokens*. Returns True if allowed."""
        ...

    @abstractmethod
    async def wait_acquire(self, channel: str, tokens: int = 1, timeout: float = 30.0) -> bool:
        """Block until tokens are available or *timeout* expires."""
        ...

    @abstractmethod
    async def remaining(self, channel: str) -> float:
        """Return the number of tokens currently available."""
        ...


# ── Token Bucket (in-memory) ───────────────────────────────────────────

@dataclass
class _Bucket:
    tokens: float
    max_tokens: float
    refill_rate: float
    refill_interval: float
    last_refill: float = field(default_factory=time.time)


class TokenBucketRateLimiter(RateLimiter):
    """In-memory token bucket rate limiter.

    Cada canal tem seu próprio bucket com capacidade e taxa de refill
    configuráveis.

    Uso::

        limiter = TokenBucketRateLimiter()
        if await limiter.acquire("shopee"):
            await call_api()
    """

    def __init__(self, config: dict[str, dict[str, float]] | None = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, channel: str, tokens: int = 1) -> bool:
        cfg = self._get_config(channel)
        async with self._lock:
            bucket = self._get_bucket(channel, cfg)
            self._refill(bucket)
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False

    async def wait_acquire(self, channel: str, tokens: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self.acquire(channel, tokens):
                return True
            await asyncio.sleep(0.1)
        return False

    async def remaining(self, channel: str) -> float:
        cfg = self._get_config(channel)
        async with self._lock:
            bucket = self._get_bucket(channel, cfg)
            self._refill(bucket)
            return bucket.tokens

    def set_config(self, channel: str, tokens: float, refill_rate: float, refill_interval: float) -> None:
        """Update rate config for a channel at runtime."""
        self._config[channel] = {
            "tokens": tokens,
            "refill_rate": refill_rate,
            "refill_interval": refill_interval,
        }

    # ── Internals ───────────────────────────────────────────────────

    def _get_config(self, channel: str) -> dict[str, float]:
        """Get config for a channel, falling back to default or first available."""
        if channel in self._config:
            return self._config[channel]
        if "default" in self._config:
            return self._config["default"]
        return next(iter(self._config.values()), {"tokens": 10, "refill_rate": 10, "refill_interval": 60})

    def _get_bucket(self, channel: str, cfg: dict[str, float]) -> _Bucket:
        if channel not in self._buckets:
            self._buckets[channel] = _Bucket(
                tokens=cfg["tokens"],
                max_tokens=cfg["tokens"],
                refill_rate=cfg["refill_rate"],
                refill_interval=cfg["refill_interval"],
            )
        return self._buckets[channel]

    def _refill(self, bucket: _Bucket) -> None:
        now = time.time()
        elapsed = now - bucket.last_refill
        if elapsed >= bucket.refill_interval:
            bucket.tokens = min(bucket.max_tokens, bucket.tokens + bucket.refill_rate)
            bucket.last_refill = now
