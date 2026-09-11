from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.utils.security import rate_limit_store

router = APIRouter(prefix="/shipping", tags=["shipping"])

_FREE_THRESHOLD = 150.0
_BASE_RATE = 15.0
_RATE_PER_ITEM = 2.0


class _ShippingOption:
    __slots__ = ("name", "price", "days_min", "days_max", "free")

    def __init__(
        self,
        name: str,
        price: float,
        days_min: int,
        days_max: int,
        free: bool,
    ) -> None:
        self.name = name
        self.price = price
        self.days_min = days_min
        self.days_max = days_max
        self.free = free

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price": round(self.price, 2),
            "days_min": self.days_min,
            "days_max": self.days_max,
            "free": self.free,
        }


def _calculate(subtotal: float, qty: int) -> list[_ShippingOption]:
    is_free = subtotal >= _FREE_THRESHOLD
    standard_price = 0.0 if is_free else round(_BASE_RATE + _RATE_PER_ITEM * max(qty - 1, 0), 2)
    express_price = round(standard_price * 1.6, 2) if not is_free else 0.0

    return [
        _ShippingOption(
            name="Padrão",
            price=standard_price,
            days_min=5,
            days_max=10,
            free=is_free,
        ),
        _ShippingOption(
            name="Expresso",
            price=express_price,
            days_min=2,
            days_max=4,
            free=is_free,
        ),
    ]


@router.get("/quote", dependencies=[Depends(rate_limit_store)])
async def shipping_quote(
    subtotal: float = Query(default=0.0, ge=0.0, description="Valor dos itens sem frete"),
    qty: int = Query(default=1, ge=1, le=999, description="Quantidade total de itens"),
) -> dict[str, Any]:
    options = _calculate(subtotal, qty)
    return {
        "subtotal": round(subtotal, 2),
        "free_threshold": _FREE_THRESHOLD,
        "free_shipping": subtotal >= _FREE_THRESHOLD,
        "options": [o.to_dict() for o in options],
    }
