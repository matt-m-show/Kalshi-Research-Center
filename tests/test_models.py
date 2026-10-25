"""Tests for Kalshi API model normalization and V2 order translation."""

import pytest

from src.common.schemas import Side
from src.kalshi_client.models import (
    CreateOrderV2Request,
    CreateOrderV2Response,
    KalshiFill,
    KalshiOrder,
    Market,
    Position,
    Settlement,
)


def test_market_from_dollar_fields():
    m = Market.model_validate({
        "ticker": "KXNBA-26OCT25LALBOS-LAL", "event_ticker": "KXNBA-26OCT25LALBOS",
        "yes_sub_title": "Lakers", "yes_bid_dollars": "0.4100", "yes_ask_dollars": "0.4400",
        "volume_fp": "1520.00", "volume_24h_fp": "300.00", "open_interest_fp": "880.00",
    })
    assert (m.yes_bid, m.yes_ask, m.volume, m.volume_24h, m.open_interest) == (41, 44, 1520, 300, 880)
    assert m.title == "Lakers"
    assert m.series_ticker == "KXNBA"


def test_position_from_v2_fields():
    p = Position.model_validate({
        "ticker": "KXFED-26DEC-T4.25", "position_fp": "-7.00",
        "market_exposure_dollars": "2.1700", "realized_pnl_dollars": "0.0000",
        "fees_paid_dollars": "0.0500", "total_traded_dollars": "2.1700",
    })
    assert p.market_ticker == "KXFED-26DEC-T4.25"
    assert p.position == -7 and p.side == Side.NO
    assert p.market_exposure == 217 and p.fees_paid == 5


def test_fill_normalization():
    f = KalshiFill.model_validate({
        "fill_id": "f1", "order_id": "o1", "ticker": "T", "outcome_side": "no",
        "book_side": "ask", "count_fp": "3.00", "yes_price_dollars": "0.3600",
        "no_price_dollars": "0.6400", "fee_cost": "0.0200", "is_taker": False,
    })
    assert f.trade_id == "f1" and f.count == 3 and f.fee == 2
    assert f.price_for(Side.NO) == 64 and f.price_for(Side.YES) == 36


def test_order_normalization():
    o = KalshiOrder.model_validate({
        "order_id": "o1", "ticker": "T", "outcome_side": "yes", "book_side": "bid",
        "status": "resting", "initial_count_fp": "5.00", "fill_count_fp": "2.00",
        "remaining_count_fp": "3.00", "yes_price_dollars": "0.4100", "no_price_dollars": "0.5900",
        "taker_fees_dollars": "0.0000", "maker_fees_dollars": "0.0100",
    })
    assert (o.count, o.filled_count, o.remaining_count, o.yes_price, o.fees) == (5, 2, 3, 41, 1)


def test_settlement_pnl():
    s = Settlement.model_validate({
        "ticker": "T", "market_result": "yes", "yes_count_fp": "5.00", "no_count_fp": "0.00",
        "yes_total_cost_dollars": "2.0500", "no_total_cost_dollars": "0.0000",
        "revenue": 500, "fee_cost": "0.0400",
    })
    assert s.pnl_cents == 500 - 205 - 4


def test_create_order_response_counts():
    r = CreateOrderV2Response.model_validate({
        "order_id": "o1", "fill_count": "2.00", "remaining_count": "3.00",
        "average_fill_price": "0.4100", "ts_ms": 1,
    })
    assert r.fill_count == 2 and r.remaining_count == 3 and r.average_fill_price == 41


@pytest.mark.parametrize("side,action,price,book_side,yes_price", [
    (Side.YES, "buy", 41, "bid", "0.41"),
    (Side.NO, "buy", 36, "ask", "0.64"),     # buying NO at 36¢ = selling YES at 64¢
    (Side.YES, "sell", 70, "ask", "0.70"),
    (Side.NO, "sell", 20, "bid", "0.80"),    # selling NO at 20¢ = buying YES at 80¢
])
def test_v2_side_translation(side, action, price, book_side, yes_price):
    req = CreateOrderV2Request.for_side(ticker="T", side=side, action=action,
                                        price_cents=price, count=4)
    assert req.side == book_side and req.price == yes_price and req.count == "4"


def test_v2_rejects_bad_price():
    with pytest.raises(ValueError):
        CreateOrderV2Request.for_side(ticker="T", side=Side.YES, action="buy", price_cents=0, count=1)
