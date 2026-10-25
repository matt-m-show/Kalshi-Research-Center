"""Order placement, cancellation and repricing against the Kalshi V2 order API.

Flow for an entry:
  1. Pre-flight: re-read the orderbook for the correct side and re-quote the
     post-only price, never above the approved max price. Check cash.
  2. Submit: POST /portfolio/events/orders (idempotent via client_order_id).
  3. Record: the local Order moves APPROVED → SUBMITTED (or FILLED if it
     matched immediately), or FAILED with the API's error.

This module never sees key material; auth.sign_request() signs each request.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import (
    ApprovedOrder,
    KalshiEnv,
    Order,
    OrderPurpose,
    OrderStatus,
    Side,
    new_id,
    utcnow,
)
from src.kalshi_client.client import KalshiClient
from src.kalshi_client.models import CreateOrderV2Request
from src.qa.pricing import FeeModel, passive_price

log = get_logger("execute_agent")


class PreflightError(ValueError):
    """The order should not be sent right now (price moved, no cash, halted)."""


def _env() -> KalshiEnv:
    return KalshiEnv(os.getenv("KALSHI_ENV", "demo").lower())


def _unix(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


def _side_price_from_yes(yes_cents: int | None, side: Side) -> int | None:
    if yes_cents is None:
        return None
    return yes_cents if side == Side.YES else 100 - yes_cents


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


async def preflight(
    client: KalshiClient,
    approved: ApprovedOrder,
    fees: FeeModel | None = None,
) -> tuple[int, int, str]:
    """Return (price_cents, contracts, note) to send now, or raise PreflightError."""
    fees = fees or FeeModel()
    ob = await client.get_orderbook(approved.market_ticker)
    side = approved.side
    max_price = approved.max_price_cents or approved.limit_price_cents

    if approved.post_only:
        price = passive_price(ob, side, max_price)
        if price is None:
            raise PreflightError(
                f"{side.value} best bid {ob.bid(side)}¢ is above our max {max_price}¢"
            )
    else:
        ask = ob.ask(side)
        if ask is None or ask > max_price:
            raise PreflightError(f"{side.value} ask {ask}¢ is above our max {max_price}¢")
        price = ask

    count = approved.size_contracts
    balance = (await client.get_balance()).balance
    per_contract = price + fees.per_contract_cents(price, maker=approved.post_only)
    affordable = int(balance // max(per_contract, 1))
    note = ""
    if affordable < count:
        if affordable < 1:
            raise PreflightError(f"Cash ${balance / 100:.2f} can't cover 1 contract at {price}¢")
        note = f"size cut {count}→{affordable} to fit cash ${balance / 100:.2f}"
        count = affordable
    if price != approved.limit_price_cents:
        note = (note + "; " if note else "") + f"re-quoted {approved.limit_price_cents}¢→{price}¢"
    return price, count, note


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


async def _submit(order: Order, client: KalshiClient, ledger: Ledger, post_only: bool,
                  tif: str = "good_till_canceled", reduce_only: bool | None = None) -> Order:
    req = CreateOrderV2Request.for_side(
        ticker=order.market_ticker,
        side=order.side,
        action=order.action,
        price_cents=order.limit_price_cents,
        count=order.size_contracts,
        time_in_force=tif,
        client_order_id=order.client_order_id,
        expiration_time=_unix(order.expires_at) if tif == "good_till_canceled" else None,
        post_only=post_only or None,
        reduce_only=reduce_only,
    )
    try:
        resp = await client.create_order(req)
    except Exception as e:
        order.status = OrderStatus.FAILED
        order.status_reason = str(e)[:500]
        order.last_updated_at = utcnow()
        ledger.save_order(order)
        ledger.append_audit("ORDER_FAILED", "execute_agent",
                            {"order_id": order.order_id, "rec_id": order.rec_id, "error": str(e)[:500]})
        log.error("order_failed", order_id=order.order_id, ticker=order.market_ticker, error=str(e))
        raise

    order.kalshi_order_id = resp.order_id
    order.submitted_at = order.last_updated_at = utcnow()
    order.filled_contracts = resp.fill_count
    if resp.fill_count and resp.average_fill_price is not None:
        order.avg_fill_price_cents = _side_price_from_yes(resp.average_fill_price, order.side)
    if resp.fill_count >= order.size_contracts:
        order.status = OrderStatus.FILLED
    elif resp.fill_count > 0:
        order.status = OrderStatus.PARTIALLY_FILLED
    elif tif == "immediate_or_cancel":
        order.status = OrderStatus.CANCELLED
        order.status_reason = "IOC: nothing matched"
    else:
        order.status = OrderStatus.SUBMITTED
    ledger.save_order(order)
    ledger.append_audit("ORDER_SUBMITTED", "execute_agent", {
        "order_id": order.order_id, "rec_id": order.rec_id, "kalshi_order_id": resp.order_id,
        "ticker": order.market_ticker, "side": order.side.value, "action": order.action,
        "price": order.limit_price_cents, "size": order.size_contracts,
        "filled": resp.fill_count, "purpose": order.purpose.value,
    })
    log.info("order_submitted", ticker=order.market_ticker, side=order.side.value,
             action=order.action, price=order.limit_price_cents, size=order.size_contracts,
             filled=resp.fill_count, kalshi_order_id=resp.order_id)
    return order


async def place_order(
    approved: ApprovedOrder,
    rec_id: str,
    client: KalshiClient,
    ledger: Ledger,
    *,
    existing: Order | None = None,
    fees: FeeModel | None = None,
    market_close_time: datetime | None = None,
) -> Order:
    """Pre-flight and submit an approved entry order.

    ``existing`` is the PENDING_APPROVAL / APPROVED record from the HITL flow;
    it is updated in place rather than duplicated.
    Raises PreflightError when the order should not go out now.
    """
    if ledger.is_halted():
        raise PreflightError("System is HALTED — refusing to place order")

    try:
        price, count, note = await preflight(client, approved, fees)
    except PreflightError as e:
        ledger.append_audit("PREFLIGHT_ABORT", "execute_agent", {"rec_id": rec_id, "reason": str(e)})
        log.warning("preflight_blocked", rec_id=rec_id, ticker=approved.market_ticker, reason=str(e))
        if existing is not None:
            existing.status = OrderStatus.CANCELLED
            existing.status_reason = f"Pre-flight: {e}"
            existing.last_updated_at = utcnow()
            ledger.save_order(existing)
        raise

    order = existing or Order(
        client_order_id=approved.client_order_id,
        rec_id=rec_id,
        market_ticker=approved.market_ticker,
        side=approved.side,
        env=_env(),
        limit_price_cents=price,
        size_contracts=count,
    )
    order.limit_price_cents = price
    order.max_price_cents = approved.max_price_cents or price
    order.size_contracts = count
    order.purpose = OrderPurpose.ENTRY
    order.action = "buy"
    order.expires_at = approved.expiry_ts
    order.market_close_time = market_close_time or order.market_close_time
    order.status = OrderStatus.APPROVED
    order.status_reason = note
    order.last_updated_at = utcnow()
    ledger.save_order(order)
    return await _submit(order, client, ledger, post_only=approved.post_only)


async def cancel(order: Order, client: KalshiClient, ledger: Ledger, reason: str) -> Order:
    """Cancel a resting order on Kalshi and record why."""
    if order.kalshi_order_id:
        try:
            await client.cancel_order(order.kalshi_order_id, order.market_ticker)
        except Exception as e:
            # A 404 usually means it already filled or expired; reconciliation will settle it
            log.warning("cancel_failed", order_id=order.order_id, error=str(e))
            ledger.append_audit("CANCEL_FAILED", "order_manager",
                                {"order_id": order.order_id, "error": str(e)[:300]})
            return order
    order.status = OrderStatus.CANCELLED
    order.status_reason = reason
    order.last_updated_at = utcnow()
    ledger.save_order(order)
    ledger.append_audit("ORDER_CANCELLED", "order_manager",
                        {"order_id": order.order_id, "ticker": order.market_ticker, "reason": reason})
    return order


async def replace_at(
    order: Order, new_price: int, client: KalshiClient, ledger: Ledger
) -> Order | None:
    """Cancel ``order`` and post its unfilled remainder at ``new_price``."""
    remaining = order.remaining_contracts
    await cancel(order, client, ledger, reason=f"repriced {order.limit_price_cents}¢→{new_price}¢")
    if order.status != OrderStatus.CANCELLED or remaining < 1:
        return None
    replacement = Order(
        client_order_id=new_id(),
        rec_id=order.rec_id,
        market_ticker=order.market_ticker,
        side=order.side,
        action=order.action,
        purpose=order.purpose,
        limit_price_cents=new_price,
        max_price_cents=order.max_price_cents,
        size_contracts=remaining,
        status=OrderStatus.APPROVED,
        expires_at=order.expires_at,
        market_close_time=order.market_close_time,
        reprice_count=order.reprice_count + 1,
        replaces_order_id=order.order_id,
        env=order.env,
    )
    ledger.save_order(replacement)
    return await _submit(replacement, client, ledger, post_only=True)


async def sell_now(
    ticker: str, side: Side, count: int, floor_price: int, rec_id: str,
    client: KalshiClient, ledger: Ledger, reason: str,
) -> Order:
    """Immediately sell up to ``count`` contracts of ``side``, accepting no less than ``floor_price``.

    Uses IOC + reduce_only so it can only shrink an existing position.
    """
    order = Order(
        client_order_id=new_id(), rec_id=rec_id, market_ticker=ticker, side=side,
        action="sell", purpose=OrderPurpose.EXIT, limit_price_cents=max(1, floor_price),
        size_contracts=count, status=OrderStatus.APPROVED, status_reason=reason, env=_env(),
    )
    ledger.save_order(order)
    return await _submit(order, client, ledger, post_only=False,
                         tif="immediate_or_cancel", reduce_only=True)


# ---------------------------------------------------------------------------
# Flatten: cancel all open orders and close all positions
# ---------------------------------------------------------------------------


async def flatten_all(client: KalshiClient, ledger: Ledger, actor: str = "user") -> dict:
    """Cancel every resting order, then sell every position at any price."""
    cancelled = await client.cancel_all_orders()
    for o in ledger.get_orders_by_status(OrderStatus.SUBMITTED.value,
                                         OrderStatus.PARTIALLY_FILLED.value):
        o.status, o.status_reason, o.last_updated_at = OrderStatus.CANCELLED, "flatten", utcnow()
        ledger.save_order(o)
    ledger.append_audit("FLATTEN_CANCEL_ORDERS", actor, {"cancelled": cancelled})

    closed: list[str] = []
    errors: list[str] = []
    for pos in await client.get_positions():
        if pos.side is None:
            continue
        try:
            await sell_now(pos.market_ticker, pos.side, abs(pos.position), 1, rec_id="flatten",
                           client=client, ledger=ledger, reason="flatten")
            closed.append(pos.market_ticker)
        except Exception as e:
            errors.append(f"{pos.market_ticker}: {e}")
            log.error("flatten_close_failed", ticker=pos.market_ticker, error=str(e))

    ledger.append_audit("FLATTEN_CLOSE_POSITIONS", actor, {"closed": closed, "errors": errors})
    return {"cancelled_orders": cancelled, "closed_positions": closed, "errors": errors}
