"""Idempotent order placement and pre-flight checks.

Flow:
  1. Pre-flight: re-query orderbook within T seconds; abort if price moved > slippage limit.
  2. Submit: POST /portfolio/orders with client_order_id for idempotency.
  3. Record: write Order artifact to ledger.
  4. Return the KalshiOrder response.

The Execute Agent calls place_order(). It never sees raw credentials or
private key material; auth.sign_request() handles that transparently.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import (
    ApprovedOrder,
    Fill,
    KalshiEnv,
    Order,
    OrderStatus,
    Side,
)
from src.kalshi_client.client import KalshiClient
from src.kalshi_client.models import CreateOrderRequest, KalshiOrder

log = get_logger("execute_agent")

_ENV = os.getenv("KALSHI_ENV", "demo").lower()

# Max allowed price drift (bps) between QA approval and actual submission.
# Read from risk.yaml at call time so dashboard edits take effect immediately.
# Falls back to env var, then 500bps if neither is set.
def _load_slippage_bps() -> int:
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(
            (Path(__file__).parents[2] / "config" / "risk.yaml").read_text(encoding="utf-8")
        )
        return int(cfg.get("preflight", {}).get("max_slippage_bps", 500))
    except Exception:
        return int(os.getenv("PREFLIGHT_SLIPPAGE_BPS", "500"))


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


async def preflight_check(
    client: KalshiClient,
    approved: ApprovedOrder,
    slippage_bps: int = _PREFLIGHT_SLIPPAGE_BPS,
) -> tuple[bool, str]:
    """Re-query the orderbook and abort if price has drifted too much.

    Returns (ok, reason).
    """
    ob = await client.get_orderbook(approved.market_ticker)
    mid = ob.mid_price()
    if mid is None:
        return False, "No orderbook mid available at submission time"

    drift = abs(approved.limit_price_cents - mid) / max(mid, 1) * 10000
    if drift > slippage_bps:
        return False, (
            f"Price moved {drift:.0f}bps from approved {approved.limit_price_cents}¢ "
            f"to current mid {mid:.1f}¢ (limit: {slippage_bps}bps)"
        )
    return True, "ok"


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


async def place_order(
    approved: ApprovedOrder,
    rec_id: str,
    client: KalshiClient,
    ledger: Ledger,
    skip_preflight: bool = False,
) -> Order:
    """Submit an approved order to Kalshi.  Idempotent via client_order_id.

    Returns the local Order record (including its status after submission).
    """
    if ledger.is_halted():
        raise RuntimeError("System is HALTED — refusing to place order")

    # Pre-flight
    if not skip_preflight:
        ok, reason = await preflight_check(client, approved, slippage_bps=_load_slippage_bps())
        if not ok:
            log.warning("preflight_failed", rec_id=rec_id, reason=reason)
            ledger.append_audit("PREFLIGHT_ABORT", "execute_agent",
                                {"rec_id": rec_id, "reason": reason})
            raise ValueError(f"Pre-flight check failed: {reason}")

    # Map our schema to the API request
    side_str = approved.side.value.lower()   # "yes" | "no"
    price_field = "yes_price" if approved.side == Side.YES else "no_price"

    expiry_ts: int | None = None
    if approved.expiry_ts:
        expiry_ts = int(approved.expiry_ts.timestamp())

    req = CreateOrderRequest(
        ticker=approved.market_ticker,
        action="buy",
        side=side_str,
        type="limit",
        count=approved.size_contracts,
        **{price_field: approved.limit_price_cents},
        expiration_ts=expiry_ts,
        client_order_id=approved.client_order_id,
    )

    # Create local record first so we have an audit trail even if API call fails
    local_order = Order(
        client_order_id=approved.client_order_id,
        rec_id=rec_id,
        market_ticker=approved.market_ticker,
        side=approved.side,
        order_type=approved.order_type,
        limit_price_cents=approved.limit_price_cents,
        size_contracts=approved.size_contracts,
        status=OrderStatus.APPROVED,
        env=KalshiEnv(_ENV),
    )
    ledger.save_order(local_order)

    try:
        kalshi_order: KalshiOrder = await client.create_order(req)
        local_order.kalshi_order_id = kalshi_order.order_id
        local_order.submitted_at = datetime.utcnow()
        local_order.status = OrderStatus.SUBMITTED
        local_order.last_updated_at = datetime.utcnow()
        ledger.save_order(local_order)
        ledger.append_audit("ORDER_SUBMITTED", "execute_agent", {
            "rec_id": rec_id,
            "order_id": local_order.order_id,
            "kalshi_order_id": kalshi_order.order_id,
            "ticker": approved.market_ticker,
            "side": side_str,
            "price": approved.limit_price_cents,
            "size": approved.size_contracts,
        })
        log.info("order_submitted", rec_id=rec_id, kalshi_order_id=kalshi_order.order_id)
    except Exception as e:
        local_order.status = OrderStatus.FAILED
        local_order.last_updated_at = datetime.utcnow()
        ledger.save_order(local_order)
        ledger.append_audit("ORDER_FAILED", "execute_agent",
                            {"rec_id": rec_id, "error": str(e)})
        log.error("order_failed", rec_id=rec_id, error=str(e))
        raise

    return local_order


# ---------------------------------------------------------------------------
# Flatten: cancel all open orders and market-limit close all positions
# ---------------------------------------------------------------------------


async def flatten_all(client: KalshiClient, ledger: Ledger, actor: str = "user") -> dict:
    """Cancel all open orders and attempt to close all positions."""
    cancelled = await client.cancel_all_orders()
    ledger.append_audit("FLATTEN_CANCEL_ORDERS", actor, {"cancelled": cancelled})

    positions = await client.get_positions()
    closed: list[str] = []
    errors: list[str] = []

    for pos in positions:
        if pos.position == 0:
            continue
        side = Side.YES if pos.position > 0 else Side.NO
        size = abs(pos.position)
        # Use market-limit: price 1¢ for YES (want to sell), 99¢ for NO
        price = 1 if side == Side.YES else 99
        req = CreateOrderRequest(
            ticker=pos.market_ticker,
            action="sell",
            side=side.value.lower(),
            type="limit",
            count=size,
            yes_price=price if side == Side.YES else None,
            no_price=price if side == Side.NO else None,
        )
        try:
            await client.create_order(req)
            closed.append(pos.market_ticker)
        except Exception as e:
            errors.append(f"{pos.market_ticker}: {e}")
            log.error("flatten_close_failed", ticker=pos.market_ticker, error=str(e))

    ledger.append_audit("FLATTEN_CLOSE_POSITIONS", actor, {"closed": closed, "errors": errors})
    return {"cancelled_orders": cancelled, "closed_positions": closed, "errors": errors}
