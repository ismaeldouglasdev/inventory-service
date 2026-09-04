"""Adopt existing Mercado Livre listings into the local channel mapping.

Listings published directly on the ML panel (never through this service)
have no ``channel_product_mapping`` row and a ``null`` ``seller_custom_field``,
so stock/price sync and order processing silently skip them. This module
reconciles those listings: it scans the seller's items, extracts the GTIN
from each listing's attributes, matches it to an active OSPOS item via
``find_active_item_by_barcode``, writes ``product_mapping`` +
``channel_product_mapping`` and backfills ``seller_custom_field`` so future
lookups hit the fast path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.adapters.implementations.mercadolivre import (
    MercadoLivreAdapter,
    _token_store,
)
from app.database import async_session_factory
from app.models.channel_product_mapping import ChannelProductMapping
from app.models.product_mapping import ProductMapping
from app.services.ospos_client import find_active_item_by_barcode

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 50
# Gentle pause between ML API calls (this PC is weak; ML rate-limits too).
_PAUSE_S = 0.4


def _extract_ean(attributes: list[dict[str, Any]]) -> str | None:
    """Pull the EAN barcode from the listing's attributes.

    Prefers the ``GTIN`` attribute, falls back to ``SELLER_SKU``. Handles both
    ``value_name`` and the ``value_struct.number`` shape ML uses for long
    values.
    """
    if not attributes:
        return None
    for attr in attributes:
        if attr.get("id") not in ("GTIN", "SELLER_SKU"):
            continue
        value = attr.get("value_name")
        if not value:
            vs = attr.get("value_struct") or {}
            value = vs.get("number")
        if value:
            return str(value).strip()
    return None


async def adopt_existing_listings(
    adapter: MercadoLivreAdapter,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register the seller's existing ML listings in the local mapping.

    Args:
        adapter: Authenticated MercadoLivreAdapter instance.
        dry_run: When true, only reports what would be adopted — no DB writes,
            no seller_custom_field PUTs.

    Returns:
        Summary dict with counters per outcome.
    """
    summary: dict[str, Any] = {
        "status": "completed",
        "dry_run": dry_run,
        "total_listings": 0,
        "adopted": 0,
        "would_adopt": 0,
        "no_ean": 0,
        "no_local_match": 0,
        "seller_field_updated": 0,
        "failed": 0,
        "errors": [],
    }

    authed = await adapter.authenticate()
    if not authed:
        summary["status"] = "error"
        summary["errors"].append("ML not authenticated")
        return summary

    user_id = _token_store.user_id
    if not user_id:
        summary["status"] = "error"
        summary["errors"].append("ML user_id not set")
        return summary

    # 1. Collect all listing IDs via GET /users/{id}/items/search (the
    #    scan-mode /sites/MLB/search returns 403).
    listing_ids: list[str] = []
    offset = 0
    while True:
        params = {
            "limit": str(_SEARCH_LIMIT),
            "offset": str(offset),
        }
        try:
            resp = await adapter._request(
                "GET", f"/users/{user_id}/items/search", params=params
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            summary["status"] = "error"
            summary["errors"].append(f"listing search failed: {exc}")
            return summary

        results = data.get("results", [])
        if not results:
            break
        listing_ids.extend(results)
        offset += _SEARCH_LIMIT
        if len(results) < _SEARCH_LIMIT:
            break

    summary["total_listings"] = len(listing_ids)
    logger.info("ML adopt: %s anúncios encontrados no seller %s",
                len(listing_ids), user_id)

    # 2. Process each listing.
    for listing_id in listing_ids:
        try:
            resp = await adapter._request("GET", f"/items/{listing_id}")
            resp.raise_for_status()
            item = resp.json()
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{listing_id}: detail fetch failed: {exc}")
            continue
        await asyncio.sleep(_PAUSE_S)

        title = (item.get("title") or "")[:80]
        status = item.get("status") or "active"
        ean = _extract_ean(item.get("attributes") or [])
        if not ean:
            scf = (item.get("seller_custom_field") or "").strip()
            if scf:
                ean = scf
        if not ean:
            summary["no_ean"] += 1
            logger.info("ML adopt: %s '%s' sem EAN — pulado", listing_id, title)
            continue

        item_id = await find_active_item_by_barcode(ean)
        if item_id is None:
            summary["no_local_match"] += 1
            logger.info("ML adopt: %s EAN=%s sem item OSPOS ativo ('%s')",
                        listing_id, ean, title)
            continue

        if dry_run:
            summary["would_adopt"] += 1
            logger.info("ML adopt (dry): %s EAN=%s → item %s", listing_id, ean, item_id)
            continue

        # 3. Upsert product_mapping + channel_product_mapping (commit per item).
        now = datetime.now(timezone.utc)
        try:
            async with async_session_factory() as session:
                pm = await session.execute(
                    select(ProductMapping).where(ProductMapping.sku == ean)
                )
                pm_row = pm.scalar_one_or_none()
                if pm_row:
                    pm_row.ospos_id = item_id
                    pm_row.last_sync_at = now
                else:
                    session.add(
                        ProductMapping(
                            sku=ean,
                            ospos_id=item_id,
                            has_variants=False,
                            store_id="principal",
                            last_sync_at=now,
                        )
                    )

                cm = await session.execute(
                    select(ChannelProductMapping).where(
                        ChannelProductMapping.sku == ean,
                        ChannelProductMapping.channel == "mercadolivre",
                    )
                )
                cm_row = cm.scalar_one_or_none()
                if cm_row:
                    cm_row.external_id = listing_id
                    cm_row.status = status
                    cm_row.synced_at = now
                    cm_row.external_url = (
                        f"https://www.mercadolivre.com.br/items/{listing_id}"
                    )
                else:
                    session.add(
                        ChannelProductMapping(
                            sku=ean,
                            channel="mercadolivre",
                            external_id=listing_id,
                            external_url=(
                                f"https://www.mercadolivre.com.br/items/{listing_id}"
                            ),
                            status=status,
                            synced_at=now,
                        )
                    )
                await session.commit()
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{listing_id} (EAN={ean}): DB upsert failed: {exc}")
            continue

        summary["adopted"] += 1
        logger.info("ML adopt: %s EAN=%s → item %s (status=%s)",
                    listing_id, ean, item_id, status)

        # 4. Backfill seller_custom_field so get_external_id finds it on the
        #    fast path without scanning.
        current_scf = (item.get("seller_custom_field") or "").strip()
        if current_scf != ean:
            try:
                resp = await adapter._request(
                    "PUT", f"/items/{listing_id}",
                    json={"seller_custom_field": ean},
                )
                resp.raise_for_status()
                summary["seller_field_updated"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(
                    f"{listing_id}: seller_custom_field PUT failed: {exc}"
                )
            await asyncio.sleep(_PAUSE_S)

    logger.info("ML adopt concluído: %s", summary)
    return summary