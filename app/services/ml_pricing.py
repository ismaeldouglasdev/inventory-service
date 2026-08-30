"""ML pricing: compute the Mercado Livre sale price from the store price.

Regra do usuário: o preço no ML SEMPRE fica acima do preço da loja (PDV),
porque as taxas do ML (comissão + frete) comem o lucro. Publicar no preço
do PDV = prejuízo. A regra é um multiplicador sobre a base configurada.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class MLPrice:
    price: float
    listing_type_id: str
    shipping_mode: str


def _round_price(value: float, mode: str) -> float:
    if mode == "99":
        return float(int(value)) + 0.99
    if mode == "90":
        return float(int(value)) + 0.90
    return round(value, 2)


def compute_ml_price(
    unit_price: float,
    cost_price: float | None = None,
    *,
    base: str | None = None,
    markup: float | None = None,
    round_mode: str | None = None,
    premium_min: float | None = None,
    shipping_mode: str | None = None,
) -> MLPrice:
    base = base or settings.ml_price_base
    markup = markup if markup is not None else settings.ml_price_markup
    round_mode = round_mode or settings.ml_price_round
    premium_min = premium_min if premium_min is not None else settings.ml_premium_min_price
    shipping_mode = shipping_mode or settings.ml_shipping_mode

    if base == "cost" and cost_price is not None:
        raw = cost_price * markup
    else:
        raw = unit_price * markup

    price = _round_price(raw, round_mode)
    listing_type_id = "gold_pro" if price >= premium_min else "gold_special"
    return MLPrice(
        price=price,
        listing_type_id=listing_type_id,
        shipping_mode=shipping_mode,
    )
