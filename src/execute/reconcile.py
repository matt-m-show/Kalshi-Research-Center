"""Reconciliation loop: syncs local order/fill state against Kalshi API.

Runs every M seconds (configured in runtime.yaml). Detects:
  - Orders filled since last sync → record fills, update order status
  - Orders expired/cancelled by Kalshi → update local status
  - Fill anomalies (fills without matching local orders) → log + alert
  - Circuit-breaker conditions → trigger HALT
"""

from __future__ import annotations

import asyncio
import os

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import Fill, KalshiEnv, Order, OrderStatus, Side
from src.kalshi_client.client import KalshiClient
from src.kalshi_client.models import KalshiOrder

log = get_logger("reconcile")

_ENV = os.getenv("KALSHI_ENV", "demo").lower()

# Status mapping from Kalshi API to our OrderStatus
_STATUS_MAP = {
    "resting": OrderStatus.SUBMITTED,
    "executed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "pending": OrderStatus.SUBMITTED,
}

# Circuit-breaker: halt if API error rate per minute exceeds this
_MAX_API_ERRORS_PER_MIN = int(os.getenv("MAX_API_ERRORS_PER_MIN", "10"))


class Reconciler:
    def __init__(self, client: KalshiClient, ledger: Ledger, interval_sec: int = 30) -> None:
        self._client = client
        self._ledger = ledger
        self._interval = interval_sec
        self._running = False
        self._error_count = 0
        self._error_window_start = 0.0

    async def run_once(self) -> dict:
        """Sync a single reconciliation pass. Returns summary dict."""
        import time
        summary = {"updated": 0, "new_fills": 0, "anomalies": 0, "errors": 0}

        # Fetch open orders from Kalshi
        try:
            kalshi_orders = await self._client.get_orders()
            kalshi_fills = await self._client.get_fills()
        except Exception as e:
            log.error("reconcile_fetch_failed", error=str(e))
            summary["errors"] += 1
            self._check_circuit_breaker(summary["errors"])
            return summary

        # Build lookup maps
        kalshi_order_map = {o.order_id: o for o in kalshi_orders if o.order_id}
        kalshi_fill_ids = {f.trade_id for f in kalshi_fills}

        # Update local orders
        local_orders = self._ledger.get_orders_by_status(
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        )

        for local in local_orders:
            if not local.kalshi_order_id:
                continue
            remote = kalshi_order_map.get(local.kalshi_order_id)
            if remote is None:
                # Not in current open orders — may be fully filled or cancelled
                remote_status = OrderStatus.FILLED  # assume filled; fills sync below
            else:
                remote_status = _STATUS_MAP.get(remote.status, local.status)
                if remote and remote.filled_count:
                    local.filled_contracts = remote.filled_count
                    if remote.filled_count < remote.count:
                        remote_status = OrderStatus.PARTIALLY_FILLED

            if remote_status != local.status:
                local.status = remote_status
                from datetime import datetime
                local.last_updated_at = datetime.utcnow()
                self._ledger.save_order(local)
                summary["updated"] += 1
                log.info("order_status_updated",
                         order_id=local.order_id, new_status=remote_status.value)

        # Record new fills
        for kf in kalshi_fills:
            if not kf.trade_id:
                continue
            # Check if already recorded
            local_order = next(
                (o for o in local_orders if o.kalshi_order_id == kf.order_id), None
            )
            if local_order is None:
                # Fill for an order not in current window — look up by kalshi_order_id
                all_submitted = self._ledger.get_orders_by_status(
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                    OrderStatus.FILLED.value,
                )
                local_order = next(
                    (o for o in all_submitted if o.kalshi_order_id == kf.order_id), None
                )
            if local_order is None:
                log.warning("fill_no_local_order", kalshi_trade_id=kf.trade_id,
                            kalshi_order_id=kf.order_id)
                summary["anomalies"] += 1
                continue

            existing_fills = self._ledger.get_fills_for_order(local_order.order_id)
            if any(f.kalshi_trade_id == kf.trade_id for f in existing_fills):
                continue  # already recorded

            side = Side.YES if kf.side == "yes" else Side.NO
            fill = Fill(
                kalshi_trade_id=kf.trade_id,
                order_id=local_order.order_id,
                rec_id=local_order.rec_id,
                market_ticker=kf.ticker,
                side=side,
                price_cents=kf.yes_price if side == Side.YES else kf.no_price,
                count=kf.count,
                is_taker=kf.is_taker,
                env=KalshiEnv(_ENV),
            )
            self._ledger.save_fill(fill)
            summary["new_fills"] += 1
            log.info("fill_recorded", fill_id=fill.fill_id, ticker=kf.ticker)

        return summary

    def _check_circuit_breaker(self, errors_this_run: int) -> None:
        import time
        now = time.monotonic()
        if now - self._error_window_start > 60:
            self._error_count = 0
            self._error_window_start = now
        self._error_count += errors_this_run
        if self._error_count >= _MAX_API_ERRORS_PER_MIN:
            self._ledger.halt(
                f"Circuit breaker: {self._error_count} API errors in 60s",
                actor="reconcile",
            )
            log.critical("circuit_breaker_triggered", error_count=self._error_count)

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            if not self._ledger.is_halted():
                try:
                    summary = await self.run_once()
                    log.debug("reconcile_pass", **summary)
                except Exception as e:
                    log.error("reconcile_loop_error", error=str(e))
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
