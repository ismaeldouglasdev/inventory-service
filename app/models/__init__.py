"""SQLAlchemy declarative models for the Inventory Service.

Re-export every model so Alembic's ``autogenerate`` and external
imports can consume them via ``from app.models import …``.
"""

from app.models.product_mapping import ProductMapping
from app.models.channel_product_mapping import ChannelProductMapping
from app.models.channel_variant_mapping import ChannelVariantMapping
from app.models.channel_fee_config import ChannelFeeConfig
from app.models.channel_state import ChannelState
from app.models.event_store import EventStore
from app.models.onboarding import OnboardingSession, OnboardingImage
from app.models.store_product import StoreProduct

__all__ = [
    "ProductMapping",
    "ChannelProductMapping",
    "ChannelVariantMapping",
    "ChannelFeeConfig",
    "ChannelState",
    "EventStore",
    "OnboardingSession",
    "OnboardingImage",
    "StoreProduct",
]
