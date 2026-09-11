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


def create_customer_token(customer_id: int) -> str:
    """Issue an HS256 customer token (sub=<customer_id>, scope=customer, 24h)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(customer_id),
        "scope": "customer",
        "iat": now,
        "exp": now + timedelta(seconds=JWT_EXPIRES_SECONDS),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


async def get_optional_customer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_bearer),
) -> Optional[int]:
    """Resolve a customer id IF a valid customer token is present, else None.

    Guest checkout support for POST /orders: no credentials → None (guest
    order allowed); a present-but-invalid token still raises 401/403 so a
    broken session never silently downgrades the caller to guest.
    """
    if credentials is None or not credentials.credentials:
        return None
    return await verify_customer_auth(credentials)


async def resolve_customer_or_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_bearer),
) -> tuple[str, Optional[int]]:
    """Resolve the viewer of an order: ("customer", id) or ("admin", None).

    Used by GET /orders/{id} where both an authenticated owner (customer
    scope) and an admin (sub=admin) may read. Any other/missing/invalid
    token is rejected with 401 (missing/invalid) or 403 (wrong scope).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autorizado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # PyJWT 2.x emite DeprecationWarning p/ algoritmo inseguro
            payload = jwt.decode(credentials.credentials, get_jwt_secret(), algorithms=["HS256"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada ou inválida",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("scope") == "customer":
        try:
            return ("customer", int(payload["sub"]))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido",
                headers={"WWW-Authenticate": "Bearer"},
            )
    if payload.get("sub") == "admin":
        return ("admin", None)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Token sem escopo de acesso a pedidos",
    )


async def verify_customer_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_bearer),
) -> int:
    """Require a valid customer Bearer JWT; return the authenticated customer id.

    Rejects admin tokens (sub=admin) and tokens without scope=customer, so
    the customer endpoints can never be reached with admin credentials.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autorizado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # PyJWT 2.x emite DeprecationWarning p/ algoritmo inseguro
            payload = jwt.decode(credentials.credentials, get_jwt_secret(), algorithms=["HS256"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada ou inválida",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("scope") != "customer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token sem escopo de cliente",
        )
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
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


# ── Password Hashing (PBKDF2-SHA256, stdlib only) ─────────────
_PBKDF2_ITERATIONS = 100_000
_PBKDF2_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a plaintext password with PBKDF2-SHA256 (no passlib/bcrypt dep).

    Stored format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<digest_hex>``.
    """
    import hashlib
    import secrets as _secrets

    salt = _secrets.token_bytes(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2 hash (constant-time)."""
    import hashlib
    import hmac as _hmac

    try:
        scheme, iterations_str, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return _hmac.compare_digest(actual, expected)


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
