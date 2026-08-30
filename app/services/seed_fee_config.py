"""Seed the channel_fee_config table with Mercado Livre fee rates.

Rates confirmed via the ML fee simulator (29/ago/2026):
  - Premium (gold_pro)   = 19% commission
  - Clássico (gold_special) = 13% commission
  - Frete grátis: ML subsidia 50% do custo de envio (shipping_subsidy)
  - Frete normal: comprador paga o frete (sem subsídio)
Run: python3 -m app.services.seed_fee_config
"""

from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import select

from app.database import async_session_factory
from app.models.channel_fee_config import ChannelFeeConfig

FEES = [
    ("commission", "0.1900", "gold_pro", "Premium"),
    ("commission", "0.1300", "gold_special", "Clássico"),
    ("shipping_subsidy", "0.5000", "free", "ML subsidia 50% do frete"),
    ("shipping_subsidy", "0.0000", "normal", "Comprador paga o frete"),
    ("payment_gateway", "0.0000", "all", "Sem taxa extra de gateway"),
]


async def seed() -> int:
    today = date.today()
    created = 0
    async with async_session_factory() as session:
        for fee_type, value, source, notes in FEES:
            result = await session.execute(
                select(ChannelFeeConfig).where(
                    ChannelFeeConfig.channel == "mercadolivre",
                    ChannelFeeConfig.fee_type == fee_type,
                    ChannelFeeConfig.source == source,
                )
            )
            if result.scalar_one_or_none():
                continue
            session.add(
                ChannelFeeConfig(
                    channel="mercadolivre",
                    fee_type=fee_type,
                    value=value,
                    valid_from=today,
                    source=source,
                    notes=notes,
                )
            )
            created += 1
        await session.commit()
    return created


if __name__ == "__main__":
    n = asyncio.run(seed())
    print(f"channel_fee_config: {n} linhas criadas")
