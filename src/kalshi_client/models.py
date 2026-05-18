"""Typed request/response models for the Kalshi REST API v2.

The production API (api.elections.kalshi.com) uses dollar-denominated fields
with a _dollars or _fp suffix. The model validators below normalize these to
integer cents so the rest of the codebase stays unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def _d2c(val: Any) -> int:
    """Convert dollar string/float to integer cents. Returns 0 on missing/null."""
    if val is None or val == "" or val == "0" or val == "0.0000":
        return 0
    try:
        return int(round(float(val) * 100))
    except (ValueError, TypeError):
        return 0


def _fp2i(val: Any) -> int:
    """Convert float-string open-interest to int contracts."""
    if val is None or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


class Market(BaseModel):
    ticker: str
    event_ticker: str = ""
    market_type: str = ""
    title: str = ""
    status: str = ""
    yes_bid: int = 0       # cents
    yes_ask: int = 0       # cents
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0
    volume: int = 0
    open_interest: int = 0
    liquidity: int = 0
    close_time: datetime | None = None
    result: str | None = None
    can_close_early: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalise_dollar_fields(cls, data: Any) -> Any:
        """Map the new _dollars / _fp field names to the canonical cent fields."""
        if not isinstance(data, dict):
            return data

        def set_if_missing(field: str, raw_val: Any, converter=_d2c) -> None:
            if not data.get(field):
                data[field] = converter(raw_val)

        set_if_missing("yes_bid",      data.get("yes_bid_dollars"))
        set_if_missing("yes_ask",      data.get("yes_ask_dollars"))
        set_if_missing("no_bid",       data.get("no_bid_dollars"))
        set_if_missing("no_ask",       data.get("no_ask_dollars"))
        set_if_missing("last_price",   data.get("last_price_dollars"))
        set_if_missing("liquidity",    data.get("liquidity_dollars"))
        set_if_missing("open_interest", data.get("open_interest_fp"), _fp2i)
        set_if_missing("volume",        data.get("volume_fp"),        _fp2i)

        return data


class OrderbookLevel(BaseModel):
    price: int   # cents
    delta: int   # positive = more contracts at this price


class Orderbook(BaseModel):
    market_ticker: str
    yes: list[OrderbookLevel] = Field(default_factory=list)
    no: list[OrderbookLevel] = Field(default_factory=list)

    def best_yes_bid(self) -> int | None:
        bids = [l.price for l in self.yes if l.delta > 0]
        return max(bids) if bids else None

    def best_yes_ask(self) -> int | None:
        bids = [l.price for l in self.no if l.delta > 0]
        # NO bids are sorted ascending in the new API; best NO bid = max.
        # YES ask = 100 - best_NO_bid  (the highest someone will pay for NO
        # implies the minimum others will accept to sell YES).
        if bids:
            return 100 - max(bids)
        return None

    def mid_price(self) -> float | None:
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return None

    def spread_bps(self) -> int | None:
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is not None and ask is not None and bid > 0:
            return int((ask - bid) * 10000 / bid)
        return None


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class Balance(BaseModel):
    balance: int = 0          # cents
    payout: int = 0
    fees_paid: int = 0


class Position(BaseModel):
    market_ticker: str
    event_ticker: str = ""
    position: int = 0         # positive = YES, negative = NO contracts
    market_exposure: int = 0  # cents
    realized_pnl: int = 0
    unrealized_pnl: int = 0
    total_cost: int = 0


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    ticker: str
    action: str                 # "buy" | "sell"
    side: str                   # "yes" | "no"
    type: str = "limit"
    count: int
    yes_price: int | None = None
    no_price: int | None = None
    expiration_ts: int | None = None   # Unix seconds, for GTD
    client_order_id: str | None = None


class KalshiOrder(BaseModel):
    order_id: str = ""
    client_order_id: str | None = None
    ticker: str = ""
    action: str = ""
    side: str = ""
    type: str = ""
    status: str = ""
    count: int = 0
    filled_count: int = 0
    remaining_count: int = 0
    yes_price: int | None = None
    no_price: int | None = None
    created_time: datetime | None = None
    expiration_time: datetime | None = None


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


class KalshiFill(BaseModel):
    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    side: str = ""
    action: str = ""
    count: int = 0
    yes_price: int = 0
    no_price: int = 0
    is_taker: bool = False
    created_time: datetime | None = None


# ---------------------------------------------------------------------------
# Generic paginated response helper
# ---------------------------------------------------------------------------


class PagedResponse(BaseModel):
    cursor: str | None = None
    data: list[Any] = Field(default_factory=list)
