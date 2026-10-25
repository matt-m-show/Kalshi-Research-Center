"""Tests for order placement against a fake Kalshi client."""

from datetime import timedelta

import pytest

from src.common.schemas import ApprovedOrder, Order, OrderStatus, Side, utcnow
from src.execute.orders import PreflightError, flatten_all, place_order, replace_at, sell_now
from src.kalshi_client.client import KalshiAPIError
from src.kalshi_client.models import Position
from tests.fakes import FakeKalshi, book

T = "KXNBA-26OCT25LALBOS-LAL"


def approved(side=Side.YES, price=41, max_price=50, size=5) -> ApprovedOrder:
    return ApprovedOrder(market_ticker=T, side=side, limit_price_cents=price,
                         max_price_cents=max_price, size_contracts=size,
                         expiry_ts=utcnow() + timedelta(hours=2))


async def test_yes_entry_posts_post_only_bid(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    order = await place_order(approved(), "rec1", k, ledger)
    req = k.created[0]
    assert (req.side, req.price, req.count, req.post_only) == ("bid", "0.41", "5", True)
    assert req.time_in_force == "good_till_canceled" and req.expiration_time is not None
    assert order.status == OrderStatus.SUBMITTED and order.kalshi_order_id == "k1"


async def test_no_entry_is_a_yes_ask(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)          # NO bid 55 / ask 60
    await place_order(approved(Side.NO, price=56, max_price=70), "rec1", k, ledger)
    req = k.created[0]
    assert (req.side, req.price) == ("ask", "0.44")   # buy NO @56 = sell YES @44


async def test_requotes_when_book_moved(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 43, 47)
    order = await place_order(approved(price=41, max_price=50), "rec1", k, ledger)
    assert order.limit_price_cents == 44 and "re-quoted" in order.status_reason


async def test_preflight_blocks_when_bid_above_max(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 52, 55)
    with pytest.raises(PreflightError):
        await place_order(approved(max_price=50), "rec1", k, ledger)
    assert not k.created


async def test_size_cut_to_cash(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    k.balance_cents = 100
    order = await place_order(approved(size=5), "rec1", k, ledger)
    assert order.size_contracts == 2 and "cash" in order.status_reason


async def test_halted_refuses(ledger):
    ledger.halt("test")
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    with pytest.raises(PreflightError):
        await place_order(approved(), "rec1", k, ledger)


async def test_api_error_marks_failed(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    k.fail_create = '{"code":"insufficient_balance"}'
    with pytest.raises(KalshiAPIError):
        await place_order(approved(), "rec1", k, ledger)
    failed = ledger.get_orders_by_status(OrderStatus.FAILED.value)
    assert len(failed) == 1 and "insufficient_balance" in failed[0].status_reason


async def test_hitl_order_updated_in_place(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    pending = Order(client_order_id="c1", rec_id="rec1", market_ticker=T, side=Side.YES,
                    limit_price_cents=41, max_price_cents=50, size_contracts=5)
    ledger.save_order(pending)
    await place_order(approved(), "rec1", k, ledger, existing=pending)
    assert len(ledger.get_orders_for_rec("rec1")) == 1
    assert ledger.get_order(pending.order_id).status == OrderStatus.SUBMITTED


async def test_replace_keeps_remaining_and_max(ledger):
    k = FakeKalshi()
    k.books[T] = book(T, 40, 45)
    first = await place_order(approved(), "rec1", k, ledger)
    first.filled_contracts = 2
    ledger.save_order(first)
    new = await replace_at(first, 44, k, ledger)
    assert k.cancelled == ["k1"]
    assert new.size_contracts == 3 and new.limit_price_cents == 44 and new.max_price_cents == 50
    assert new.reprice_count == 1 and new.replaces_order_id == first.order_id


async def test_sell_now_is_ioc_reduce_only(ledger):
    k = FakeKalshi()
    await sell_now(T, Side.NO, 3, 20, "rec1", k, ledger, reason="stop loss")
    req = k.created[0]
    assert (req.side, req.price, req.time_in_force, req.reduce_only) == \
        ("bid", "0.80", "immediate_or_cancel", True)


async def test_flatten_sells_both_sides(ledger):
    k = FakeKalshi()
    k.positions = [Position(market_ticker="A", position=3), Position(market_ticker="B", position=-2)]
    result = await flatten_all(k, ledger)
    sides = {r.ticker: (r.side, r.price) for r in k.created}
    assert sides == {"A": ("ask", "0.01"), "B": ("bid", "0.99")}
    assert result["closed_positions"] == ["A", "B"]
