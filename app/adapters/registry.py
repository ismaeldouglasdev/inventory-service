"""Central registry for marketplace adapters."""

from __future__ import annotations

from typing import NoReturn

from app.adapters.base import MarketplaceAdapter


class AdapterRegistryError(Exception):
    """Raised when an adapter lookup fails."""


class AdapterRegistry:
    """Holds all registered ``MarketplaceAdapter`` instances.

    Usage::

        registry = AdapterRegistry()
        registry.register(woo_adapter)
        wc = registry.get("woocommerce")
    """

    def __init__(self) -> None:
        self._adapters: dict[str, MarketplaceAdapter] = {}

    def register(self, adapter: MarketplaceAdapter) -> None:
        """Register an adapter keyed by its ``channel_name``."""
        name = adapter.channel_name
        if name in self._adapters:
            raise AdapterRegistryError(
                f"Adapter for channel {name!r} is already registered"
            )
        self._adapters[name] = adapter

    def get(self, channel: str) -> MarketplaceAdapter:
        """Retrieve the adapter for a given channel name.

        Raises ``AdapterRegistryError`` when the channel is unknown.
        """
        try:
            return self._adapters[channel]
        except KeyError:
            raise AdapterRegistryError(
                f"No adapter registered for channel {channel!r}. "
                f"Available: {list(self._adapters)}"
            ) from None

    def all(self) -> list[MarketplaceAdapter]:
        """Return every registered adapter."""
        return list(self._adapters.values())

    def channel_names(self) -> list[str]:
        """Return names of all registered channels."""
        return list(self._adapters.keys())

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, channel: str) -> bool:
        return channel in self._adapters
