"""Shopee Open Platform v2 adapter.

Autenticação:
1. GET /api/v2/auth/url -> redirect seller to Shopee
2. Shopee redirects to callback with ?code=...
3. POST /api/v2/auth/token/get -> access_token + refresh_token

Documentação: https://open.shopee.com/documents
"""

from __future__ import annotations

import hashlib 
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.adapters.base import MarketplaceAdapter
from app.config import settings

# -- Assinatura HMAC 
# ---------------------------- 

def _sign_request(
        partner_id: int,
        api_key: str,
        path: str,
        timestamp: int,
        access_token: str = "",
        body: dict[str, Any] | None = None,
) -> str:
    """Gera a assinatura HMAC-SHA256 exigida pela Shopee."""
    raw = f"{partner_id}{path}{timestamp}{access_token}"
    if body:
        import json
        raw += json.dumps(body, separators=(",",":"))
    return hmac.new(
        api_key.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
logger = logging.getLogger(__name__)

# -- Constants

_API_BASE = "https://partner.shopeemobile.com"
_API_BASE_SANDBOX = "https://partner.test-stable.shopeemobile.com"
_API_TIMEOUT = 30.0
_PARTNER_ID = 0 # Será preenchido via settings
_API_KEY = ""  # Será preenchido via settings 

# -- Token Store
# ---------------------------
class ShopeeTokenStore:
    """Armazena tokens da Shopee em memória.
    
    Na produção isso vai pro banco (tabela ChannelState).
    """

    def __init__(self) -> None:
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.expires_at: float = 0.0 # timestamp epoch
        self.shop_id: int = 0

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at

    def update(self, data: dict[str, Any]) -> None:
        self.access_token = data.get("access_token", self.access_token)
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        expires_in = data.get("expires_in", 0)
        self.expires_at = time.time() + expires_in - 60 # 1 min buffer
        self.shop_id = data.get("shop_id", self.shop_id)

_token_store = ShopeeTokenStore()

# -- Adapter 
class ShopeeAdapter(MarketplaceAdapter):
    """Adapter for Shopee marketplace via Open Platform v2 API."""

    @property
    def channel_name(self) -> str:
        return "shopee"

# --------------------------------- 
# HTTP helpers 
# ---------------------------------
    @staticmethod 
    def _base_url() -> str:
        """Retorna a URL base de acordo com o ambiente."""
        return _API_BASE_SANDBOX if settings.shopee_sandbox else _API_BASE
    @staticmethod
    def _common_params() -> dict 
