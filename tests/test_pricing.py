"""Tests for the deterministic pricing engine."""

import pytest

from src.common.schemas import Side
from src.kalshi_client.models import Orderbook, OrderbookLevel
from src.qa.pricing import (
    FeeModel,
    NoQuote,
    Quote,
    max_acceptable_price,
    passive_price,
    quote,
    reprice,
)


def book(yes_bid: int | None, yes_ask: int | None, depth: int = 100) -> Orderbook:
    """Build a book from YES bid/ask (the NO side holds the mirrored YES ask)."""
    return Orderbook(
        market_ticker="KXTEST-1",
        yes=[OrderbookLevel(price=yes_bid, delta=depth)] if yes_bid is not None else [],
        no=[OrderbookLevel(price=100 - yes_ask, delta=depth)] if yes_ask is not None else [],
    )


NO_FEES = FeeModel(fee_type="quadratic", multiplier=0.0)


class TestFees:
    def test_taker_fee_matches_schedule(self):
        # 0.07 * 10 * 0.5 * 0.5 = $0.175 → rounds up to 18¢
        assert FeeModel().total_cents(50, 10, maker=False) == 18

    def test_maker_free_on_plain_quadratic(self):
        assert FeeModel("quadratic").total_cents(50, 10, maker=True) == 0

    def test_maker_fee_on_maker_series(self):
        # 0.0175 * 10 * 0.25 = $0.04375 → 5¢
        assert FeeModel("quadratic_with_maker_fees").total_cents(50, 10, maker=True) == 5

    def test_multiplier_scales(self):
        assert FeeModel(multiplier=2.0).per_contract_cents(50, maker=False) == pytest.approx(3.5)


class TestOrderbookSides:
    def test_no_side_derived_from_yes(self):
        ob = book(40, 45)
        assert ob.bid(Side.YES) == 40 and ob.ask(Side.YES) == 45
        assert ob.bid(Side.NO) == 55 and ob.ask(Side.NO) == 60


class TestMaxAcceptablePrice:
    def test_without_fees(self):
        assert max_acceptable_price(60.0, 3.0, NO_FEES, maker=True) == 57

    def test_taker_fee_lowers_max(self):
        # at 57¢ the taker fee is ~1.7¢, so 57 no longer leaves 3¢ of edge
        assert max_acceptable_price(60.0, 3.0, FeeModel(), maker=False) == 55

    def test_none_when_fair_too_low(self):
        assert max_acceptable_price(2.0, 3.0, NO_FEES, maker=True) is None


class TestPassivePrice:
    def test_improves_bid_by_one_tick(self):
        assert passive_price(book(40, 45), Side.YES, max_price=50) == 41

    def test_capped_at_max(self):
        assert passive_price(book(40, 45), Side.YES, max_price=40) == 40

    def test_joins_bid_when_spread_is_one_tick(self):
        assert passive_price(book(44, 45), Side.YES, max_price=50) == 44

    def test_none_when_bid_above_max(self):
        assert passive_price(book(40, 45), Side.YES, max_price=38) is None

    def test_empty_bid_side_posts_below_ask(self):
        assert passive_price(book(None, 45), Side.YES, max_price=60) == 44

    def test_never_crosses(self):
        price = passive_price(book(40, 41), Side.YES, max_price=99)
        assert price is not None and price < 41


class TestQuote:
    def test_picks_yes_when_fair_above_market(self):
        q = quote(0.60, book(40, 45), min_edge_cents=3, fees=NO_FEES)
        assert isinstance(q, Quote)
        assert q.side == Side.YES and q.price_cents == 41 and q.max_price_cents == 57
        assert q.edge_cents == pytest.approx(19.0)

    def test_picks_no_when_fair_below_market(self):
        # fair YES 20% → fair NO 80¢; NO bid 55 / ask 60
        q = quote(0.20, book(40, 45), min_edge_cents=3, fees=NO_FEES)
        assert isinstance(q, Quote)
        assert q.side == Side.NO and q.price_cents == 56

    def test_no_quote_when_fairly_priced(self):
        q = quote(0.425, book(40, 45), min_edge_cents=3, fees=NO_FEES)
        assert isinstance(q, NoQuote) and q.reason == "NO_EDGE_AT_MARKET"

    def test_spread_filter(self):
        q = quote(0.60, book(30, 50), min_edge_cents=3, fees=NO_FEES, max_spread_cents=10)
        assert isinstance(q, NoQuote)

    def test_take_style_uses_ask_and_taker_fee(self):
        q = quote(0.60, book(40, 45), min_edge_cents=3, fees=FeeModel(), style="take")
        assert isinstance(q, Quote)
        assert q.price_cents == 45 and not q.maker and q.fee_cents > 0

    def test_take_style_rejects_ask_above_max(self):
        q = quote(0.47, book(40, 45), min_edge_cents=3, fees=FeeModel(), style="take")
        assert isinstance(q, NoQuote)


class TestReprice:
    def test_moves_up_when_outbid(self):
        assert reprice(book(43, 48), Side.YES, current_price=41, max_price=50) == 44

    def test_stays_when_still_best(self):
        assert reprice(book(41, 48), Side.YES, current_price=41, max_price=50) is None

    def test_never_above_max(self):
        assert reprice(book(45, 48), Side.YES, current_price=41, max_price=43) is None
