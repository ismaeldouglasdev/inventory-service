"""Duplicate product resolution — picks the "best" product when multiple
store products share the same SKU (barcode).

Rule (by priority):
1. Most recently modified (``last_modified`` / ``updated_at``) wins.
2. Higher stock wins (real available quantity).
3. Uppercase name wins (more concrete / catalog-style entry).
4. Existing image wins (photo follows the best record).
5. Higher price wins as a tie-breaker.

The whole point is that the photo, visibility and scan resolution follow
the record with the most concrete, up-to-date data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _sortable_dt(value) -> datetime:
    return value if value is not None else datetime.min.replace(tzinfo=timezone.utc)


def product_score(p) -> tuple:
    """Compute a comparable score for one StoreProduct.

    Tuples are compared element-wise; higher is better.
    """
    last_modified = _sortable_dt(getattr(p, "last_modified", None) or getattr(p, "updated_at", None))

    name = (p.name or "").strip()
    is_uppercase = bool(name) and name == name.upper()

    return (
        last_modified,          # 1. most recently modified
        p.stock if p.stock else 0,  # 2. more stock
        int(is_uppercase),      # 3. uppercase / catalog name
        int(bool(getattr(p, "image_url", None))),  # 4. has image
        float(p.price or 0),    # 5. higher price
    )


def pick_best_duplicate(products: list) -> object:
    """Return the best product among a list sharing the same SKU."""
    if not products:
        return None
    if len(products) == 1:
        return products[0]
    best = max(products, key=product_score)
    logger.info(
        "Duplicate resolution: %d candidates for SKU %s → picked %s (id=%s)",
        len(products),
        getattr(best, "sku", "?"),
        best.name,
        best.id,
    )
    return best


def group_duplicates(products: list) -> dict[str, list]:
    """Group products by SKU, returning only SKUs with more than one entry."""
    groups: dict[str, list] = {}
    for p in products:
        groups.setdefault(p.sku, []).append(p)
    return {sku: items for sku, items in groups.items() if len(items) > 1}
