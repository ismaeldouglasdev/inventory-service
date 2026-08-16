"""Dashboard API endpoints for mobile PWA.

Provides real-time sales KPIs, stock alerts, and goals with WebSocket push.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.services import ospos_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _jsonable(value: Any) -> Any:
    """Recursively convert values not serializable by json.dumps (Decimal from MySQL)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value

# ── WebSocket connection manager ──────────────────────────────────────────


class DashboardNotifier:
    """Manages WebSocket connections for real-time dashboard updates."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)
        logger.info("DashboardNotifier: client connected (%d total)", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._connections:
                self._connections.remove(ws)
        logger.info("DashboardNotifier: client disconnected (%d remaining)", len(self._connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send update to all connected clients."""
        dead: list[WebSocket] = []
        data = _jsonable(data)
        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections.remove(ws)
        if dead:
            logger.info("DashboardNotifier: cleaned %d dead connection(s)", len(dead))


    async def broadcast_stock_alert(self, alert_data: dict[str, Any]) -> None:
        """Broadcast a stock alert change."""
        await self.broadcast({"type": "stock_alert", **alert_data, "ts": datetime.now().isoformat(timespec="seconds")})


_dashboard_notifier = DashboardNotifier()


# ── Background task: poll OSPOS for changes and push ──────────────────────


async def _dashboard_poller(interval: int = 10) -> None:
    """Poll OSPOS for new sales and stock alerts, push via WebSocket.

    - New completed sales are pushed one-by-one as ``{"type": "sale", sale_id, ...}``
      so the app can show a notification per sale.
    - Today's aggregate KPIs are pushed as ``{"type": "summary", ...}`` whenever the
      transaction count grows.
    - A small ring buffer of recent sales is kept for the ``init`` snapshot and the
      ``/sales/recent`` endpoint is served on demand.
    """
    last_sales_count = 0
    last_alerts: dict[int, int] = {}  # item_id -> stock_status
    # Start from the current max id so a service restart doesn't replay history.
    last_sale_id = await ospos_client.fetch_max_sale_id()

    while True:
        try:
            await asyncio.sleep(interval)

            # Fetch current summary (today)
            summary = await ospos_client.fetch_dashboard_summary("today")
            current_sales = summary["transactions"]

            # New individual sales since last poll
            max_id = await ospos_client.fetch_max_sale_id()
            if max_id > last_sale_id:
                new_sales = await ospos_client.fetch_new_sales(last_sale_id, 50)
                for sale in new_sales:
                    await _dashboard_notifier.broadcast({
                        "type": "sale",
                        **sale,
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    })
                last_sale_id = max_id

            # Aggregate KPI update for today
            if current_sales > last_sales_count:
                await _dashboard_notifier.broadcast({
                    "type": "summary",
                    "transactions": current_sales,
                    "sales_total": summary["sales_total"],
                    "items_sold": summary["items_sold"],
                    "avg_ticket": summary["avg_ticket"],
                    "payment_summary": summary["payment_summary"],
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                last_sales_count = current_sales

            # Fetch stock alerts
            alerts = await ospos_client.fetch_stock_alerts(50)
            current_alerts = {a["item_id"]: a["stock_status"] for a in alerts}

            # Detect changes
            for item_id, status in current_alerts.items():
                if item_id not in last_alerts or last_alerts[item_id] != status:
                    alert = next(a for a in alerts if a["item_id"] == item_id)
                    await _dashboard_notifier.broadcast_stock_alert(alert)

            # Detect cleared alerts
            for item_id in list(last_alerts.keys()):
                if item_id not in current_alerts:
                    await _dashboard_notifier.broadcast_stock_alert({
                        "item_id": item_id,
                        "cleared": True,
                    })

            last_alerts = current_alerts

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Dashboard poller error: %s", exc)
            await asyncio.sleep(interval * 2)


# Start poller on module import (will be managed by main.py lifespan)
_poller_task: asyncio.Task | None = None


def start_dashboard_poller() -> None:
    global _poller_task
    if _poller_task is None or _poller_task.done():
        _poller_task = asyncio.create_task(_dashboard_poller())


def stop_dashboard_poller() -> None:
    global _poller_task
    if _poller_task and not _poller_task.done():
        _poller_task.cancel()


# ── Response Schemas ──────────────────────────────────────────────────────


class DashboardSummary(BaseModel):
    period: str
    sales_total: float
    transactions: int
    items_sold: int
    avg_ticket: float
    top_items: list[dict]
    hourly_sales: list[float]
    hourly_max: float
    daily_target: float
    target_pct: int
    pending_receivables: float
    prev_sales_total: float
    change_pct: int
    payment_summary: list[dict] = []


class RecentSale(BaseModel):
    sale_id: int
    sale_time: str
    customer: str
    items_count: int
    total: float


class StockAlert(BaseModel):
    item_id: int
    name: str
    item_number: str
    reorder_level: int
    quantity: int
    stock_status: int  # 1=ZERADO, 2=IRREGULAR
    location_id: int


class DashboardAlerts(BaseModel):
    alerts: list[StockAlert]
    count: int


# ── REST Endpoints ────────────────────────────────────────────────────────


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary(
    period: str = Query("today", pattern=r"^(today|yesterday|week|month|custom)$"),
    start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> Any:
    """Get sales KPIs for the given period.

    - period: today | yesterday | week | month | custom
    - start/end: required when period=custom (YYYY-MM-DD)
    """
    if period == "custom" and not start:
        start = end = datetime.now().strftime("%Y-%m-%d")
    return await ospos_client.fetch_dashboard_summary(period, start, end)


@router.get("/alerts", response_model=DashboardAlerts)
async def get_stock_alerts(
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    """Get items with ZERADO or IRREGULAR stock status."""
    alerts = await ospos_client.fetch_stock_alerts(limit)
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/alert-count")
async def get_stock_alert_count() -> dict[str, int]:
    """Get count of items with stock alerts (for badge)."""
    count = await ospos_client.fetch_stock_alert_count()
    return {"count": count}


@router.get("/sales/recent", response_model=list[RecentSale])
async def get_recent_sales(
    limit: int = Query(10, ge=1, le=50),
) -> Any:
    """Get the most recent completed sales (newest first)."""
    return await ospos_client.fetch_recent_sales(limit)


# ── WebSocket Endpoint ────────────────────────────────────────────────────


@router.websocket("/ws")
async def dashboard_websocket(websocket: WebSocket) -> None:
    """Real-time dashboard updates.

    Sends initial state on connect, then pushes:
    - {"type": "sale", transactions, sales_total, avg_ticket, ts}
    - {"type": "stock_alert", item_id, name, ..., ts} or {"type": "stock_alert", item_id, cleared: true, ts}
    """
    await _dashboard_notifier.connect(websocket)
    try:
        # Send initial state
        summary = await ospos_client.fetch_dashboard_summary("today")
        alerts = await ospos_client.fetch_stock_alerts(20)
        recent_sales = await ospos_client.fetch_recent_sales(10)
        await websocket.send_json(_jsonable({
            "type": "init",
            "summary": summary,
            "alerts": alerts,
            "recent_sales": recent_sales,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }))

        # Keep alive — listen for pings
        while True:
            try:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await _dashboard_notifier.disconnect(websocket)