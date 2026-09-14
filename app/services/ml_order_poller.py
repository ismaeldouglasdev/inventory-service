"""Mercado Livre order poller — fallback de captura de vendas.

The ML webhook (``POST /v1/mercadolivre/webhook``) is the fast path for new
sales, but it depends on a **public inbound URL** (tunnel) configured in the ML
developer portal — and ML only pushes a notification once. If the tunnel goes
down, the webhook is missed and the sale is lost forever.

This poller queries the ML ``/orders/search`` API every ``ml_poll_interval``
seconds and pushes any *new* order through the same pipeline the webhook uses
(``_process_ml_order`` → ``write_ospos_sale`` → ML stock sync back). It is
idempotent because ``write_ospos_sale`` dedupes on ``client_sale_id = ml-{id}``.

It is the poller (not the webhook) that actually implements "detect ML sales
and keep stock in sync" reliably, since it does not depend on inbound
connectivity.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Cursor state file — survives service restarts so we never reprocess the
# whole history after a reboot (only look back at the last N minutes).
# File lives at <project>/data/ml_poller_state.json (three parents up from
# app/services/ml_order_poller.py).
_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ml_poller_state.json"

# Look-back window on first run / after cursor loss. Orders older than this
# are considered already handled (or stale), and skipping them keeps the
# initial poll light.
_LOOKBACK_ON_RESET = timedelta(hours=24)


def _default_state() -> dict[str, Any]:
    """State used when the cursor file is missing or corrupt."""
    return {"last_order_created": (datetime.now(timezone.utc) - _LOOKBACK_ON_RESET).isoformat()}


def _load_state() -> dict[str, Any]:
    try:
        raw = _STATE_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return _default_state()
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("last_order_created"):
            return _default_state()
        return data
    except (OSError, json.JSONDecodeError):
        return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("ML poller: could not persist cursor state to %s", _STATE_PATH)


class MLOrderPoller:
    """Periodically fetch new ML orders and push them into OSPOS."""

    def __init__(self, poll_interval: float | None = None) -> None:
        self.poll_interval = poll_interval or settings.ml_poll_interval
        self._running = False

    # ── Public API ───────────────────────────────────────────────────

    async def run_once(self) -> int:
        """Poll ML for new orders and process them — return processed count."""
        from app.adapters.implementations.mercadolivre import _token_store
        from app.api.v1.mercadolivre import _get_adapter, _process_ml_order

        user_id = _token_store.user_id or settings.ml_user_id
        if not user_id:
            logger.warning("ML poller: no seller id known, skipping cycle")
            return 0

        state = _load_state()
        since = state.get("last_order_created")
        if not since:
            since = _default_state()["last_order_created"]

        try:
            adapter = _get_adapter()
        except Exception as exc:
            logger.warning("ML poller: adapter unavailable (%s), skipping cycle", exc)
            return 0

        orders = await self._fetch_new_orders(adapter, user_id, since)
        if not orders:
            # Still advance the cursor to "now" so a long stretch without
            # sales does not accumulate a huge window on the next cycle.
            _save_state({"last_order_created": datetime.now(timezone.utc).isoformat()})
            return 0

        newest = since
        processed = 0
        for order in orders:
            order_id = order.get("id")
            if not order_id:
                continue
            created = order.get("date_created") or ""
            if created and created > newest:
                newest = created
            try:
                results = await _process_ml_order(adapter, str(order_id), None)
                for r in results:
                    if r.get("status") == "sale_written":
                        processed += 1
            except Exception:
                logger.exception("ML poller: failed processing order %s", order_id)

        # Always advance cursor; a failed order will be re-fetched by webhook
        # or manually, not eternally retried by the poller.
        _save_state({"last_order_created": newest or datetime.now(timezone.utc).isoformat()})

        logger.info(
            "ML poller: %d order(s) seen, %d new sale(s) written to OSPOS (since=%s)",
            len(orders), processed, since,
        )
        return processed

    async def run_forever(self) -> None:
        """Loop infinito — runs until stop() is called."""
        self._running = True
        logger.info(
            "ML order poller started (poll_interval=%ss; user_id=%s)",
            self.poll_interval, settings.ml_user_id,
        )
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("ML poller: error in poll cycle")
            await asyncio.sleep(self.poll_interval)
        logger.info("ML order poller stopped")

    def stop(self) -> None:
        self._running = False

    # ── ML access ────────────────────────────────────────────────────

    async def _fetch_new_orders(
        self, adapter: Any, user_id: int, since: str
    ) -> list[dict[str, Any]]:
        """Query ML /orders/search for orders created after ``since``.

        Returns the raw order dicts (each carrying ``id``, ``date_created``
        and ``status``). We rely on ``_process_ml_order`` for the heavy lifting
        (mapping, idempotent sale write, stock sync), so here we only paginate
        the search results.
        """
        params = {
            "seller_id": str(user_id),
            "order.date_created.from": since,
            "sort": "date_asc",
            "limit": "50",
        }
        seen: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = await adapter._request("GET", "/orders/search", params={**params, "offset": str(offset)})
            if resp.status_code >= 400:
                # Do NOT advance the cursor on API failure — orders created
                # during an outage would be skipped forever. Propagate so
                # run_forever logs and retries on the next cycle.
                raise RuntimeError(
                    f"/orders/search failed ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            results = data.get("results") or []
            seen.extend(results)
            total = int((data.get("paging") or {}).get("total", 0))
            offset += len(results)
            # Stop when page exhausted or paging says we have them all.
            if not results or offset >= total:
                break

        # ML returns newest-last with date_asc; drop anything without a real id.
        return [o for o in seen if o.get("id")]