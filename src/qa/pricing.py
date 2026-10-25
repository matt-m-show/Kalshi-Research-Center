"""Deterministic pricing: turns a fair-probability estimate into an order price.

The research model only supplies ``fair_yes_prob``. Everything about price is
decided here from the live orderbook, so the LLM never sets what we pay.

Conventions: prices are integer cents of the side being bought (YES or NO),
1 tick = 1 cent.

Kalshi fees (per fee schedule; multiplier comes from the series):
  taker: ceil(0.07   x M x C x P x (1-P))  dollars, P in dollars
  maker: ceil(0.0175 x M x C x P x (1-P))  only on series whose fee_type
         includes maker fees; otherwise resting orders pay nothing.
"""

from __future__ import annotations

import dataclasses
import math

from src.common.schemas import Side
from src.kalshi_client.models import Orderbook

TAKER_RATE = 0.07
MAKER_RATE = 0.0175
_MAKER_FEE_TYPES = {"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


@dataclasses.dataclass(frozen=True)
class FeeModel:
    fee_type: str = "quadratic"
    multiplier: float = 1.0

    def rate(self, maker: bool) -> float:
        if maker:
            return MAKER_RATE if self.fee_type in _MAKER_FEE_TYPES else 0.0
        return TAKER_RATE

    def total_cents(self, price_cents: int, count: int, maker: bool) -> int:
        """Fee for ``count`` contracts at ``price_cents``, rounded up to the cent."""
        p = price_cents / 100.0
        raw = self.rate(maker) * self.multiplier * count * p * (1 - p) * 100.0
        return math.ceil(raw - 1e-9) if raw > 0 else 0

    def per_contract_cents(self, price_cents: int, maker: bool) -> float:
        """Unrounded expected fee per contract (used for edge math)."""
        p = price_cents / 100.0
        return self.rate(maker) * self.multiplier * p * (1 - p) * 100.0


@dataclasses.dataclass(frozen=True)
class Quote:
    side: Side
    price_cents: int              # where we will post (or take)
    max_price_cents: int          # the most we will ever pay when repricing
    fair_cents: float             # fair value of the side, in cents
    edge_cents: float             # fair - price - fee, per contract
    fee_cents: float              # expected fee per contract
    maker: bool
    bid_cents: int | None         # best bid on our side when quoted
    ask_cents: int | None         # best ask on our side when quoted

    @property
    def fair_prob(self) -> float:
        return self.fair_cents / 100.0


@dataclasses.dataclass(frozen=True)
class NoQuote:
    reason: str
    detail: str = ""


def side_fair_cents(fair_yes_prob: float, side: Side) -> float:
    p = fair_yes_prob if side == Side.YES else 1.0 - fair_yes_prob
    return p * 100.0


def max_acceptable_price(
    fair_cents: float, min_edge_cents: float, fees: FeeModel, maker: bool
) -> int | None:
    """Highest whole-cent price that still leaves ``min_edge_cents`` after fees."""
    start = min(99, math.floor(fair_cents - min_edge_cents))
    for price in range(start, 0, -1):
        if fair_cents - price - fees.per_contract_cents(price, maker) >= min_edge_cents - 1e-9:
            return price
    return None


def passive_price(ob: Orderbook, side: Side, max_price: int) -> int | None:
    """Price for a post-only bid one tick inside the spread, capped at ``max_price``.

    Returns None when the best bid is already above what we're willing to pay
    (we'd be queued behind better bids at a worse price — adverse selection).
    """
    bid, ask = ob.bid(side), ob.ask(side)
    if bid is None:
        target = (ask - 1) if ask is not None else max_price
    else:
        target = bid + 1
        if ask is not None and target >= ask:
            target = bid          # 1-tick spread: join the best bid instead of crossing
        if max_price < bid:
            return None
    price = min(target, max_price)
    if ask is not None and price >= ask:
        price = ask - 1
    return price if 1 <= price <= 99 else None


def quote(
    fair_yes_prob: float,
    ob: Orderbook,
    min_edge_cents: float,
    fees: FeeModel,
    style: str = "passive",
    max_spread_cents: int | None = None,
) -> Quote | NoQuote:
    """Pick the side with edge and the price to enter at.

    style="passive": post-only bid inside the spread (maker).
    style="take":    cross the spread at the current ask (taker).
    """
    best: Quote | None = None
    reasons: list[str] = []
    maker = style == "passive"

    for side in (Side.YES, Side.NO):
        fair = side_fair_cents(fair_yes_prob, side)
        cap = max_acceptable_price(fair, min_edge_cents, fees, maker)
        bid, ask = ob.bid(side), ob.ask(side)
        if cap is None:
            reasons.append(f"{side.value}: fair {fair:.1f}¢ leaves no room for {min_edge_cents}¢ edge")
            continue
        if max_spread_cents is not None and bid is not None and ask is not None and ask - bid > max_spread_cents:
            reasons.append(f"{side.value}: spread {ask - bid}¢ > max {max_spread_cents}¢")
            continue

        if maker:
            price = passive_price(ob, side, cap)
            if price is None:
                reasons.append(f"{side.value}: best bid {bid}¢ already above our max {cap}¢")
                continue
        else:
            if ask is None or ask > cap:
                reasons.append(f"{side.value}: ask {ask}¢ above our max {cap}¢")
                continue
            price = ask

        fee = fees.per_contract_cents(price, maker)
        q = Quote(
            side=side, price_cents=price, max_price_cents=cap, fair_cents=fair,
            edge_cents=fair - price - fee, fee_cents=fee, maker=maker,
            bid_cents=bid, ask_cents=ask,
        )
        if best is None or q.edge_cents > best.edge_cents:
            best = q

    if best is None:
        return NoQuote("NO_EDGE_AT_MARKET", "; ".join(reasons))
    return best


def reprice(
    ob: Orderbook, side: Side, current_price: int, max_price: int
) -> int | None:
    """New price for a resting passive bid that has been outbid, or None to stay put.

    Only moves up when someone else now holds a strictly better bid, and never
    above ``max_price``. (Kalshi's book doesn't identify our own order, so a
    best bid equal to our price is assumed to include us.)
    """
    bid, ask = ob.bid(side), ob.ask(side)
    if bid is None or bid <= current_price:
        return None
    target = passive_price(ob, side, max_price)
    if target is None or target <= current_price:
        return None
    return target
