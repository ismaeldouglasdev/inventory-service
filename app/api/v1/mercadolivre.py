"""Mercado Livre OAuth and product-publish endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.implementations.mercadolivre import (
    MercadoLivreAdapter,
    _token_store,
)
from app.adapters.registry import AdapterRegistry
from app.config import settings
from app.database import get_session
from app.models.product_mapping import ProductMapping
from app.models.channel_product_mapping import ChannelProductMapping
from app.schemas.product import ChannelPublishRequest
from app.services import ospos_client
from app.services.event_processor import create_event
from app.utils.security import verify_admin_auth, verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mercadolivre", tags=["mercadolivre"])

# Registry reference (injected from main.py)
_registry: AdapterRegistry | None = None


def _set_registry(registry: AdapterRegistry) -> None:
    global _registry
    _registry = registry


def _get_adapter() -> MercadoLivreAdapter:
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    try:
        adapter = _registry.get("mercadolivre")
        if not isinstance(adapter, MercadoLivreAdapter):
            raise HTTPException(status_code=500, detail="Wrong adapter type")
        return adapter
    except Exception:
        raise HTTPException(status_code=503, detail="ML adapter not registered")


# ── OAuth ────────────────────────────────────────────────────────────────


@router.get("/auth-url")
async def auth_url() -> dict[str, str]:
    """Return the URL a seller must visit to authorise the app."""
    url = MercadoLivreAdapter.auth_url()
    return {"auth_url": url}


@router.get("/callback")
async def oauth_callback(
    code: str = Query(..., description="Authorization code from ML"),
) -> dict[str, Any]:
    """Exchange an OAuth code for access/refresh tokens."""
    try:
        data = await MercadoLivreAdapter.exchange_code(code)
        return {
            "status": "ok",
            "access_token": data.get("access_token", "")[:20] + "...[hidden]",
            "refresh_token": data.get("refresh_token", "")[:20] + "...[hidden]",
            "user_id": data.get("user_id"),
            "expires_in": data.get("expires_in"),
        }
    except Exception as exc:
        logger.error("ML OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def ml_status() -> dict[str, Any]:
    """Check whether the ML adapter is authenticated."""
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    return {
        "authenticated": authed,
        "user_id": _token_store.user_id,
        "has_refresh_token": bool(_token_store.refresh_token),
    }


@router.get("/token-debug", dependencies=[Depends(verify_admin_auth)])
async def token_debug() -> dict[str, Any]:
    """Return whether the ML adapter holds tokens (no raw values).

    Security fix (29/ago/2026): this endpoint previously returned the raw
    access_token/refresh_token in plaintext with NO authentication. It is now
    admin-only and returns only booleans — never the token values.
    """
    return {
        "has_access_token": bool(_token_store.access_token),
        "has_refresh_token": bool(_token_store.refresh_token),
        "ml_user_id": _token_store.user_id,
    }


# ── Product publishing ───────────────────────────────────────────────────


@router.post("/publish", status_code=201, dependencies=[Depends(verify_api_key)])
async def publish_product(
    body: ChannelPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Publish a product to Mercado Livre.

    Security fix (29/ago/2026): this endpoint previously had NO authentication
    — anyone could publish items to ML. Now requires a valid X-API-Key.

    Accepts full product data in the request body, publishes directly
    to ML, saves the channel mapping, and creates a tracking event
    in the EventStore.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone

    sku = body.sku

    # 1. Ensure ProductMapping exists (create if not)
    result = await session.execute(
        select(ProductMapping).where(ProductMapping.sku == sku)
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        mapping = ProductMapping(
            sku=sku,
            ospos_id=0,
            has_variants=False,
            store_id="principal",
        )
        session.add(mapping)

    # 2. Check if already published
    result = await session.execute(
        select(ChannelProductMapping).where(
            ChannelProductMapping.sku == sku,
            ChannelProductMapping.channel == "mercadolivre",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return {
            "status": "already_published",
            "channel": "mercadolivre",
            "external_id": existing.external_id,
            "external_url": existing.external_url,
        }

    # 3. Publish via adapter
    adapter = _get_adapter()
    authed = await adapter.authenticate()
    if not authed:
        raise HTTPException(
            status_code=401,
            detail="ML not authenticated. Visit /v1/mercadolivre/auth-url first.",
        )

    product_data = {
        "title": body.title,
        "sku": sku,
        "description": body.description,
        "price": body.price,
        "cost_price": body.cost_price,
        "stock_quantity": body.stock_quantity,
        "condition": body.condition,
        "listing_type_id": body.listing_type_id,
        "category_id": body.category_id or settings.ml_default_category,
        "pictures": body.pictures,
        "attributes": body.attributes,
    }

    try:
        external_id = await adapter.publish_product(product_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ML publish failed: {exc}")

    # 4. Save channel mapping
    channel_mapping = ChannelProductMapping(
        sku=sku,
        channel="mercadolivre",
        external_id=external_id,
        external_url=f"https://www.mercadolivre.com.br/items/{external_id}",
        status="active",
        synced_at=datetime.now(timezone.utc),
    )
    session.add(channel_mapping)

    # 5. Create tracking event in EventStore
    event = create_event(
        event_type="product.created",
        payload=product_data,
        sku=sku,
        channel="mercadolivre",
    )
    session.add(event)

    await session.commit()

    return {
        "status": "published",
        "channel": "mercadolivre",
        "external_id": external_id,
        "external_url": channel_mapping.external_url,
        "event_id": event.id,
    }


# ── Webhook (ML → inventory-service) ────────────────────────────────────


@router.post("/webhook")
async def ml_webhook(
    body: dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Receive a Mercado Livre webhook notification.

    ML posts here on topic changes (``orders_v2``, ``items``, ``questions``).
    The payload is normalised via ``MercadoLivreAdapter.parse_webhook`` and a
    tracking event is persisted in the EventStore so downstream processors
    (stock sync, order pipeline) can react.

    For ``orders_v2`` the order is fetched from ML and written as a real
    sale into OSPOS (``pdv.write_ospos_sale``), deducting stock and then
    pushing the updated remaining stock back to ML. This runs as a
    background task so the endpoint returns 200 within ML's 500ms window —
    ML deactivates topics when the callback is slow.
    """
    adapter = _get_adapter()
    try:
        parsed = await adapter.parse_webhook(body)
    except Exception as exc:
        logger.error("ML webhook parse failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}")

    # Persist a tracking event for downstream processors.
    event = create_event(
        event_type=parsed.get("event_type", "mercadolivre.unknown"),
        payload=parsed.get("raw", body),
        sku=parsed.get("sku"),
        channel="mercadolivre",
    )
    session.add(event)
    await session.commit()

    # ── orders_v2: push the ML sale through the sell pipeline ──────────
    if parsed.get("event_type") == "mercadolivre.orders_v2":
        order_id = _extract_order_id(body.get("resource", ""))
        if order_id:
            background_tasks.add_task(_process_ml_order, adapter, order_id, event.id)

    logger.info(
        "ML webhook received: topic=%s sku=%s event_id=%s",
        parsed.get("event_type"),
        parsed.get("sku"),
        event.id,
    )
    return {"status": "ok", "event_id": event.id}


def _extract_order_id(resource: str) -> str | None:
    """Extract the ML order id from a resource URL like /v1/orders/123456."""
    if "/orders/" not in resource:
        return None
    return resource.split("/orders/")[-1].strip("/") or None


async def _process_ml_order(
    adapter: MercadoLivreAdapter,
    order_id: str,
    source_event_id: str | None,
) -> list[dict[str, Any]]:
    """Fetch an ML order and write it as a REAL sale into OSPOS.

    Maps each ML item to a local OSPOS item (via ``channel_product_mapping``
    → SKU → active ``ospos_items.item_number``), then delegates to
    ``pdv.write_ospos_sale`` so the sale is committed transactionally in the
    OSPOS MySQL (sales + payments + sales_items + stock/inventory decrement),
    with the same idempotency used by the POS app.

    Items whose ML id has no local mapping are skipped (not published through
    this service) and reported as ``skipped``.
    """
    from app.api.v1.pdv import (
        PdvItem,
        PdvPayment,
        PdvSaleRequest,
        write_ospos_sale,
    )

    order = await adapter.fetch_order(order_id)
    if not order or not order.get("items"):
        logger.warning("ML order %s: no items to process", order_id)
        return []

    # Only write a real sale for orders that actually went through.
    order_status = order.get("status") or ""
    if order_status in ("cancelled", "cancelled_by_buyer", "payment_required"):
        logger.info("ML order %s: skipped (status=%s)", order_id, order_status)
        return [
            {
                "external_id": "-",
                "sku": None,
                "status": "skipped",
                "reason": f"order status {order_status}",
            }
        ]

    pdv_items: list[PdvItem] = []
    sold_skus: list[tuple[str, int]] = []  # (sku, ospos item_id) for post-sale ML stock sync
    results: list[dict[str, Any]] = []
    for it in order["items"]:
        sku = await adapter.get_sku_by_external_id(it["external_id"])
        if not sku:
            results.append(
                {
                    "external_id": it["external_id"],
                    "status": "skipped",
                    "reason": "no local mapping",
                }
            )
            continue

        item_id = await ospos_client.find_active_item_by_sku(sku)
        if item_id is None:
            results.append(
                {
                    "external_id": it["external_id"],
                    "sku": sku,
                    "status": "skipped",
                    "reason": "no active OSPOS item for SKU",
                }
            )
            continue

        pdv_items.append(
            PdvItem(
                item_id=item_id,
                line=len(pdv_items),
                quantity=float(it["quantity"]),
                price=float(it["unit_price"]),
            )
        )
        sold_skus.append((sku, item_id))

    if not pdv_items:
        logger.warning("ML order %s: no mappable items (%d skipped)", order_id, len(results))
        return results

    total = round(sum(it.quantity * it.price for it in pdv_items), 2)
    payload = PdvSaleRequest(
        items=pdv_items,
        payments=[PdvPayment(payment_type="Mercado Livre", payment_amount=total)],
        employee_id=1,
        comment=f"ML order {order_id}",
        client_sale_id=f"ml-{order_id}",
    )
    try:
        sale = await write_ospos_sale(payload)
        ml_synced = True
        if not sale.get("duplicate"):
            # A baixa real decrementa ospos_item_quantities, que o CDC não
            # observa (ele diffs ospos_items). Sincroniza o ML agora com o
            # estoque remanescente REAL, lido do MySQL após a venda.
            for sku, item_id in sold_skus:
                remaining = await ospos_client.fetch_item_stock(item_id)
                synced = await adapter.update_stock(sku, int(remaining))
                if not synced:
                    ml_synced = False
                    logger.warning(
                        "ML order %s: ML stock sync failed for SKU %s (remaining=%d)",
                        order_id, sku, int(remaining),
                    )
        results.append(
            {
                "external_id": ",".join(it["external_id"] for it in order["items"]),
                "sku": ",".join(str(p.item_id) for p in pdv_items),
                "quantity": sum(it.quantity for it in pdv_items),
                "status": "sale_written" if not sale.get("duplicate") else "duplicate",
                "sale_id": sale.get("sale_id"),
                "total": total,
                "ml_stock_synced": ml_synced,
            }
        )
        logger.info(
            "ML order %s: OSPOS sale %s (duplicate=%s, R$%.2f)",
            order_id, sale.get("sale_id"), sale.get("duplicate", False), total,
        )
    except Exception as exc:
        logger.error("ML order %s: OSPOS sale write failed: %s", order_id, exc)
        results.append(
            {
                "external_id": ",".join(it["external_id"] for it in order["items"]),
                "sku": ",".join(str(p.item_id) for p in pdv_items),
                "status": "error",
                "reason": str(exc),
            }
        )
    return results


# ── Listings ─────────────────────────────────────────────────────────────


@router.get("/listings")
async def ml_listings(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List products published to Mercado Livre with their status."""
    from sqlalchemy import select

    result = await session.execute(
        select(ChannelProductMapping).where(
            ChannelProductMapping.channel == "mercadolivre"
        )
    )
    rows = result.scalars().all()
    listings = [
        {
            "sku": r.sku,
            "external_id": r.external_id,
            "external_url": r.external_url,
            "status": r.status,
            "synced_at": r.synced_at.isoformat() if r.synced_at else None,
        }
        for r in rows
    ]
    return {"count": len(listings), "listings": listings}


# ── Adoption (reconcile existing ML listings) ────────────────────────────


@router.post("/adopt", dependencies=[Depends(verify_admin_auth)])
async def adopt_listings(
    dry_run: bool = Query(False, description="Simulate without persisting"),
) -> dict[str, Any]:
    """Adopt listings published directly on ML into the local mapping.

    Scans the seller's items, matches each GTIN to an active OSPOS product,
    writes ``product_mapping`` + ``channel_product_mapping`` and backfills
    ``seller_custom_field`` so stock/price sync and order processing work for
    listings that never went through ``/publish``.

    Run with ``dry_run=true`` first to preview the counts — nothing is written.
    """
    from app.services.ml_adopt import adopt_existing_listings

    adapter = _get_adapter()
    try:
        return await adopt_existing_listings(adapter, dry_run=dry_run)
    except Exception as exc:
        logger.error("ML adopt failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"ML adopt failed: {exc}")
