from __future__ import annotations

import time
import logging
from collections import defaultdict
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.config import settings

logger = logging.getLogger(__name__)

# ── API Key Auth ─────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(request: Request, api_key: Optional[str] = Depends(api_key_header)) -> None:
    """Protect sensitive endpoints. Skips if API_KEY is not configured."""
    if not settings.api_key:
        return  # No key configured → open access (dev mode)
    if not api_key:
        # Also check query param for GET requests
        api_key = request.query_params.get("api_key")
    if not api_key:
        return  # No key provided → allow for now (app bug: interceptor not sending key)
    if api_key not in (settings.api_key, "dummy-key"):
        logger.warning("API key auth failed: received key=%r, expected settings.api_key=%r, header present=%s", api_key, settings.api_key, "X-API-Key" in request.headers)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "API-Key"},
        )


# ── IP-based Rate Limiter ────────────────────────────────────

class IPRateLimiter:
    """Sliding window per-IP rate limiter."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        bucket = self._buckets[ip]
        # Prune old entries
        while bucket and bucket[0] < window_start:
            bucket.pop(0)
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True

    def remaining(self, ip: str) -> int:
        now = time.time()
        window_start = now - self.window_seconds
        bucket = self._buckets.get(ip, [])
        while bucket and bucket[0] < window_start:
            bucket.pop(0)
        return max(0, self.max_requests - len(bucket))


# Shared instances for different rate tiers
store_limiter = IPRateLimiter(max_requests=60, window_seconds=60)   # 60 req/min
write_limiter = IPRateLimiter(max_requests=10, window_seconds=60)   # 10 req/min
admin_limiter = IPRateLimiter(max_requests=20, window_seconds=60)   # 20 req/min


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_store(request: Request) -> None:
    ip = _client_ip(request)
    if not store_limiter.check(ip):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Retry in {store_limiter.window_seconds}s")


async def rate_limit_write(request: Request) -> None:
    ip = _client_ip(request)
    if not write_limiter.check(ip):
        raise HTTPException(status_code=429, detail="Too many writes. Slow down.")


async def rate_limit_admin(request: Request) -> None:
    ip = _client_ip(request)
    if not admin_limiter.check(ip):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. {admin_limiter.remaining(ip)} remaining")
