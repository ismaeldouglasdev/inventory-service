"""Abstract base class for all marketplace adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MarketplaceAdapter(ABC):
    """Interface that every marketplace adapter must implement.

    Each adapter handles auth, stock/price sync, webhook normalisation,
    and product publishing for one channel (Shopee, Mercado Livre,
    WooCommerce, etc.).
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Human-readable channel identifier, e.g. ``'woocommerce'``."""
        ...

    @abstractmethod
    async def authenticate(self) -> bool:
        """Verify that stored credentials are valid.

        Returns ``True`` when the channel API responds successfully.
        """
        ...

    @abstractmethod
    async def update_stock(self, sku: str, quantity: int) -> bool:
        """Push current stock quantity to the channel."""
        ...

    @abstractmethod
    async def update_price(self, sku: str, price: float) -> bool:
        """Push current selling price to the channel."""
        ...

    @abstractmethod
    async def parse_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise a raw channel webhook into the internal event format.

        The returned dict should at minimum contain:
          - ``event_type``
          - ``sku`` (if identifiable)
          - ``channel``
          - ``raw`` (original payload)
        """
        ...

    @abstractmethod
    async def get_external_id(self, sku: str) -> str | None:
        """Resolve an internal SKU to the channel's external product ID."""
        ...

    @abstractmethod
    async def publish_product(self, product: dict[str, Any]) -> str:
        """Create (or fully replace) a product on the channel.

        Returns the external product ID assigned by the channel.
        """
        ...
