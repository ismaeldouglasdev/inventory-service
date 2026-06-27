"""Testes do Rate Limiter — Token Bucket.

Cobre:
  - acquire/acquire wait/remaining
  - Channel-specific config
  - Rate limit enforcement
  - Refill behavior
"""

from __future__ import annotations

import pytest

from app.services.rate_limiter import TokenBucketRateLimiter


class TestTokenBucketRateLimiter:
    @pytest.fixture
    def limiter(self):
        return TokenBucketRateLimiter()

    async def test_acquire_allows_within_limit(self, limiter):
        assert await limiter.acquire("shopee") is True
        assert await limiter.acquire("shopee") is True

    async def test_acquire_blocks_when_exhausted(self):
        limiter = TokenBucketRateLimiter({
            "shopee": {"tokens": 2, "refill_rate": 2, "refill_interval": 3600},
        })
        assert await limiter.acquire("shopee") is True
        assert await limiter.acquire("shopee") is True
        assert await limiter.acquire("shopee") is False

    async def test_multiple_tokens(self):
        limiter = TokenBucketRateLimiter({
            "shopee": {"tokens": 5, "refill_rate": 5, "refill_interval": 3600},
        })
        assert await limiter.acquire("shopee", 5) is True
        assert await limiter.acquire("shopee", 1) is False

    async def test_remaining(self):
        limiter = TokenBucketRateLimiter({
            "shopee": {"tokens": 3, "refill_rate": 3, "refill_interval": 3600},
        })
        await limiter.acquire("shopee", 2)
        remaining = await limiter.remaining("shopee")
        assert remaining == pytest.approx(1.0)

    async def test_default_config(self, limiter):
        assert await limiter.acquire("unknown-channel") is True

    async def test_wait_acquire_timeout(self):
        limiter = TokenBucketRateLimiter({
            "shopee": {"tokens": 0, "refill_rate": 0, "refill_interval": 3600},
        })
        result = await limiter.wait_acquire("shopee", timeout=0.5)
        assert result is False

    async def test_set_config_runtime(self, limiter):
        limiter.set_config("custom", tokens=5, refill_rate=5, refill_interval=60)
        for _ in range(5):
            assert await limiter.acquire("custom") is True
        assert await limiter.acquire("custom") is False
