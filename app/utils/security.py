from __future__ import annotations

import logging
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

# ── API Key Auth ─────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── Admin JWT Auth ───────────────────────────────────────────
_admin_bearer = HTTPBearer(auto_error=False)
JWT_EXPIRES_SECONDS = 86400  # 24h

_ephemeral_jwt_secret: str = ""


def get_jwt_secret() -> str:
    """Resolve the admin JWT signing secret (env-configured or ephemeral).

    A single ephemeral secret is generated per process so that tokens
    issued by login keep validating until restart. Production MUST set
    JWT_SECRET — a warning is logged once per process otherwise.
    """
    global _ephemeral_jwt_secret
    if settings.jwt_secret:
        return settings.jwt_secret
    if not _ephemeral_jwt_secret:
        import secrets as _secrets

        _ephemeral_jwt_secret = _secrets.token_urlsafe(48)
        logger.warning(
            "JWT_SECRET not configured; using EPHEMERAL per-process secret "
            "(tokens invalidam a cada restart). Configure JWT_SECRET em produção."
        )
    return _ephemeral_jwt_secret


def create_admin_token() -> str:
    """Issue an HS256 admin token (sub=admin, 24h expiry)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "admin",
        "iat": now,
        "exp": now + timedelta(seconds=JWT_EXPIRES_SECONDS),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


async def verify_admin_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_bearer),
) -> None:
    """Require a valid admin Bearer JWT on protected endpoints."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autorizado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # PyJWT 2.x emite DeprecationWarning p/ algoritmo inseguro
            jwt.decode(credentials.credentials, get_jwt_secret(), algorithms=["HS256"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada ou inválida",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def verify_api_key(request: Request, api_key: Optional[str] = Depends(api_key_header)) -> None:
    """Protect sensitive endpoints. REQUIRES a valid API key.

    Security fix (29/ago/2026): previously, a missing API_KEY env var or a
    missing/invalid key was silently ALLOWED (open access). Now:
    - If API_KEY is not configured, the endpoint is unavailable (503).
    - A missing or invalid key is rejected with 401.
    - The hardcoded "dummy-key" backdoor was removed.
    """
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key not configured on server",
        )
    if not api_key:
        # Also check query param for GET requests
        api_key = request.query_params.get("api_key")
    if not api_key or api_key != settings.api_key:
        logger.warning(
            "API key auth failed: received key=%r, header present=%s",
            api_key,
            "X-API-Key" in request.headers,
        )
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
