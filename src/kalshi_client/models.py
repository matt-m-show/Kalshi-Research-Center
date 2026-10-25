"""Typed request/response models for the Kalshi REST API v2.

The production API reports prices as fixed-point dollar strings (``*_dollars``)
and counts as fixed-point strings (``*_fp``). The validators below normalize
these to integer cents / integer contracts so the rest of the codebase works in
whole cents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.common.schemas import Side


# ---------------------------------------------------------------------------
# Unit converters
# ---------------------------------------------------------------------------


def _d2c(val: Any) -> int:
    """Convert dollar string/float to integer cents. Returns 0 on missing/null."""
    if val is None or val == "":
        return 0
    try:
        return int(round(float(val) * 100))
    except (ValueError, TypeError):
        return 0


def _fp2i(val: Any) -> int:
    """Convert a fixed-point count string ("10.00") to whole contracts."""
    if val is None or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def cents_to_dollars(cents: int) -> str:
    """Format integer cents as the fixed-point dollar string the API expects."""
    return f"{cents / 100:.2f}"


def _fill(data: dict, field: str, raw: Any, converter=_d2c) -> None:
    """Set ``data[field]`` from a new-style raw value when the legacy field is absent."""
    if data.get(field) in (None, "") and raw not in (None, ""):
        data[field] = converter(raw)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class Market(BaseModel):
    ticker: str
    event_ticker: str = ""
    market_type: str = ""
    title: str = ""
    yes_sub_title: str = ""
    status: str = ""
    yes_bid: int = 0       # cents
    yes_ask: int = 0       # cents
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    close_time: datetime | None = None
    expected_expiration_time: datetime | None = None
    result: str | None = None
    can_close_early: bool = False
    rules_primary: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalise_dollar_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _fill(data, "yes_bid", data.get("yes_bid_dollars"))
        _fill(data, "yes_ask", data.get("yes_ask_dollars"))
        _fill(data, "no_bid", data.get("no_bid_dollars"))
        _fill(data, "no_ask", data.get("no_ask_dollars"))
        _fill(data, "last_price", data.get("last_price_dollars"))
        _fill(data, "open_interest", data.get("open_interest_fp"), _fp2i)
        _fill(data, "volume", data.get("volume_fp"), _fp2i)
        _fill(data, "volume_24h", data.get("volume_24h_fp"), _fp2i)
        # `title` is deprecated; fall back to the YES subtitle for display
        if not data.get("title") and data.get("yes_sub_title"):
            data["title"] = data["yes_sub_title"]
        return data

    @property
    def series_ticker(self) -> str:
        return self.event_ticker.split("-")[0] if self.event_ticker else self.ticker.split("-")[0]


class Series(BaseModel):
    ticker: str
    title: str = ""
    category: str = ""
    categories: list[str] = Field(default_factory=list)
    tags: list[str] | None = None
    frequency: str = ""
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    volume: int = 0

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _fill(data, "volume", data.get("volume_fp"), _fp2i)
        if data.get("tags") is None:
            data["tags"] = []
        if data.get("categories") is None:
            data["categories"] = []
        return data


class OrderbookLevel(BaseModel):
    price: int   # cents
    delta: int   # contracts resting at this price


class Orderbook(BaseModel):
    """Both sides of a Kalshi book are BIDS: ``yes`` = YES bids, ``no`` = NO bids.

    A NO bid at X is a YES ask at 100 - X, and vice versa.
    """

    market_ticker: str
    yes: list[OrderbookLevel] = Field(default_factory=list)
    no: list[OrderbookLevel] = Field(default_factory=list)

    def _levels(self, side: Side) -> list[OrderbookLevel]:
        return [l for l in (self.yes if side == Side.YES else self.no) if l.delta > 0]

    def bid(self, side: Side) -> int | None:
        """Best price someone will pay for ``side``."""
        levels = self._levels(side)
        return max(l.price for l in levels) if levels else None

    def ask(self, side: Side) -> int | None:
        """Cheapest price ``side`` can be bought at right now."""
        other = Side.NO if side == Side.YES else Side.YES
        b = self.bid(other)
        return 100 - b if b is not None else None

    def ask_depth(self, side: Side, max_price: int) -> int:
        """Contracts of ``side`` purchasable at or below ``max_price``."""
        other = Side.NO if side == Side.YES else Side.YES
        return sum(l.delta for l in self._levels(other) if 100 - l.price <= max_price)

    def bid_depth(self, side: Side, min_price: int) -> int:
        """Contracts of ``side`` that could be sold at or above ``min_price``."""
        return sum(l.delta for l in self._levels(side) if l.price >= min_price)

    def mid(self, side: Side) -> float | None:
        b, a = self.bid(side), self.ask(side)
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    # Legacy YES-only helpers (kept for callers that only think in YES terms)
    def best_yes_bid(self) -> int | None:
        return self.bid(Side.YES)

    def best_yes_ask(self) -> int | None:
        return self.ask(Side.YES)

    def mid_price(self) -> float | None:
        return self.mid(Side.YES)

    def spread_cents(self) -> int | None:
        b, a = self.bid(Side.YES), self.ask(Side.YES)
        return a - b if b is not None and a is not None else None

    def spread_bps(self) -> int | None:
        b, a = self.bid(Side.YES), self.ask(Side.YES)
        if b is not None and a is not None and b > 0:
            return int((a - b) * 10000 / b)
        return None


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class Balance(BaseModel):
    balance: int = 0           # cents, available cash
    portfolio_value: int = 0   # cents


class Position(BaseModel):
    market_ticker: str
    position: int = 0          # positive = YES contracts, negative = NO contracts
    market_exposure: int = 0   # cents
    realized_pnl: int = 0      # cents
    fees_paid: int = 0         # cents
    total_traded: int = 0      # cents

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get("market_ticker") and data.get("ticker"):
            data["market_ticker"] = data["ticker"]
        _fill(data, "position", data.get("position_fp"), _fp2i)
        _fill(data, "market_exposure", data.get("market_exposure_dollars"))
        _fill(data, "realized_pnl", data.get("realized_pnl_dollars"))
        _fill(data, "fees_paid", data.get("fees_paid_dollars"))
        _fill(data, "total_traded", data.get("total_traded_dollars"))
        return data

    @property
    def side(self) -> Side | None:
        if self.position > 0:
            return Side.YES
        if self.position < 0:
            return Side.NO
        return None


class Settlement(BaseModel):
    ticker: str
    event_ticker: str = ""
    market_result: str = ""        # "yes" | "no" | "scalar"
    yes_count: int = 0
    no_count: int = 0
    yes_total_cost: int = 0        # cents
    no_total_cost: int = 0         # cents
    revenue: int = 0               # cents
    fee_cost: int = 0              # cents
    value: int | None = None       # cents paid per YES contract
    settled_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _fill(data, "yes_count", data.get("yes_count_fp"), _fp2i)
        _fill(data, "no_count", data.get("no_count_fp"), _fp2i)
        _fill(data, "yes_total_cost", data.get("yes_total_cost_dollars"))
        _fill(data, "no_total_cost", data.get("no_total_cost_dollars"))
        if isinstance(data.get("fee_cost"), str):
            data["fee_cost"] = _d2c(data["fee_cost"])
        return data

    @property
    def pnl_cents(self) -> int:
        return self.revenue - self.yes_total_cost - self.no_total_cost - self.fee_cost


# ---------------------------------------------------------------------------
# Orders (V2: everything is quoted on the YES leg)
# ---------------------------------------------------------------------------


class CreateOrderV2Request(BaseModel):
    """Body for ``POST /portfolio/events/orders``.

    ``side`` is the YES book side: ``bid`` buys YES, ``ask`` sells YES (which is
    economically a NO buy at ``1 - price``).
    """

    ticker: str
    side: str                      # "bid" | "ask"
    count: str                     # fixed-point count, e.g. "5"
    price: str                     # YES price in dollars, e.g. "0.43"
    time_in_force: str = "good_till_canceled"
    self_trade_prevention_type: str = "taker_at_cross"
    client_order_id: str | None = None
    expiration_time: int | None = None   # unix seconds, GTC only
    post_only: bool | None = None
    reduce_only: bool | None = None      # IOC only

    @classmethod
    def for_side(
        cls,
        *,
        ticker: str,
        side: Side,
        action: str,
        price_cents: int,
        count: int,
        **kwargs: Any,
    ) -> "CreateOrderV2Request":
        """Translate "buy/sell <side> at <side price>" into the YES-leg V2 form."""
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents must be 1-99, got {price_cents}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        buying_yes_exposure = (side == Side.YES) == (action == "buy")
        book_side = "bid" if buying_yes_exposure else "ask"
        yes_price = price_cents if side == Side.YES else 100 - price_cents
        return cls(
            ticker=ticker,
            side=book_side,
            count=str(count),
            price=cents_to_dollars(yes_price),
            **kwargs,
        )


class CreateOrderV2Response(BaseModel):
    order_id: str
    client_order_id: str | None = None
    fill_count: int = 0
    remaining_count: int = 0
    average_fill_price: int | None = None   # cents (YES terms)
    average_fee_paid: int | None = None     # cents per contract

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for f in ("fill_count", "remaining_count"):
            if isinstance(data.get(f), str):
                data[f] = _fp2i(data[f])
        for f in ("average_fill_price", "average_fee_paid"):
            if isinstance(data.get(f), str):
                data[f] = _d2c(data[f])
        return data


class KalshiOrder(BaseModel):
    order_id: str = ""
    client_order_id: str | None = None
    ticker: str = ""
    outcome_side: str = ""     # "yes" | "no" — the exposure this order creates
    book_side: str = ""        # "bid" | "ask" on the YES leg
    status: str = ""           # "resting" | "canceled" | "executed"
    count: int = 0
    filled_count: int = 0
    remaining_count: int = 0
    yes_price: int | None = None
    no_price: int | None = None
    fees: int = 0              # cents, maker + taker
    created_time: datetime | None = None
    expiration_time: datetime | None = None
    last_update_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _fill(data, "count", data.get("initial_count_fp"), _fp2i)
        _fill(data, "filled_count", data.get("fill_count_fp"), _fp2i)
        _fill(data, "remaining_count", data.get("remaining_count_fp"), _fp2i)
        _fill(data, "yes_price", data.get("yes_price_dollars"))
        _fill(data, "no_price", data.get("no_price_dollars"))
        if not data.get("outcome_side") and data.get("side") in ("yes", "no"):
            data["outcome_side"] = data["side"]
        if not data.get("fees"):
            data["fees"] = _d2c(data.get("taker_fees_dollars")) + _d2c(data.get("maker_fees_dollars"))
        return data


class KalshiFill(BaseModel):
    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    outcome_side: str = ""     # "yes" | "no"
    book_side: str = ""        # "bid" | "ask"
    action: str = ""           # deprecated by Kalshi; kept when present
    count: int = 0
    yes_price: int = 0         # cents
    no_price: int = 0          # cents
    fee: int = 0               # cents
    is_taker: bool = False
    created_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get("trade_id") and data.get("fill_id"):
            data["trade_id"] = data["fill_id"]
        if not data.get("ticker") and data.get("market_ticker"):
            data["ticker"] = data["market_ticker"]
        _fill(data, "count", data.get("count_fp"), _fp2i)
        _fill(data, "yes_price", data.get("yes_price_dollars"))
        _fill(data, "no_price", data.get("no_price_dollars"))
        _fill(data, "fee", data.get("fee_cost"))
        if not data.get("outcome_side") and data.get("side") in ("yes", "no"):
            data["outcome_side"] = data["side"]
        return data

    def price_for(self, side: Side) -> int:
        return self.yes_price if side == Side.YES else self.no_price
