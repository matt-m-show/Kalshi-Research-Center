"""Tests for the order manager's sync, repricing and settlement logic."""

from datetime import timedelta

from src.common.config import ExecutionConfig
from src.common.schemas import Assessment, Order, OrderStatus, Side, utcnow
from src.execute.manager import OrderManager
from src.kalshi_client.models import KalshiFill, KalshiOrder, Market, Settlement
from tests.fakes import FakeKalshi, book

T = "KXFED-26DEC-T4.25"


def resting(ledger, **overrides) -> Order:
    o = Order(client_order_id="c1", rec_id="rec1", market_ticker=T, side=Side.YES,
              limit_price_cents=41, max_price_cents=50, size_contracts=5,
              status=OrderStatus.SUBMITTED, kalshi_order_id="k1",
              submitted_at=utcnow() - timedelta(minutes=30),
              last_updated_at=utcnow() - timedelta(minutes=30))
    for k, v in overrides.items():
        setattr(o, k, v)
    ledger.save_order(o)
    return o


async def test_sync_marks_partial_and_filled(ledger):
    o = resting(ledger)
    k = FakeKalshi()
    k.orders["k1"] = KalshiOrder(order_id="k1", status="resting", count=5, filled_count=2)
    mgr = OrderManager(ledger)
    assert await mgr.sync_orders(k) == 1
    assert ledger.get_order(o.order_id).status == OrderStatus.PARTIALLY_FILLED
    k.orders["k1"].status, k.orders["k1"].filled_count = "executed", 5
    await mgr.sync_orders(k)
    assert ledger.get_order(o.order_id).status == OrderStatus.FILLED


async def test_fills_recorded_once(ledger):
    o = resting(ledger)
    k = FakeKalshi()
    k.fills = [KalshiFill(trade_id="f1", order_id="k1", ticker=T, outcome_side="yes",
                          count=5, yes_price=41, no_price=59, fee=0)]
    mgr = OrderManager(ledger)
    assert await mgr.sync_fills(k) == 1
    assert await mgr.sync_fills(k) == 0
    updated = ledger.get_order(o.order_id)
    assert updated.avg_fill_price_cents == 41 and updated.status == OrderStatus.FILLED


async def test_reprices_outbid_order(ledger):
    resting(ledger)
    k = FakeKalshi()
    k.books[T] = book(T, 43, 48)
    cfg = ExecutionConfig(reprice_interval_minutes=10, max_reprices=6)
    assert await OrderManager(ledger).manage_resting(k, cfg) == 1
    assert k.cancelled == ["k1"]
    assert k.created[0].price == "0.44"


async def test_cancels_after_ttl(ledger):
    o = resting(ledger, expires_at=utcnow() - timedelta(minutes=1))
    k = FakeKalshi()
    await OrderManager(ledger).manage_resting(k, ExecutionConfig())
    assert ledger.get_order(o.order_id).status == OrderStatus.CANCELLED


async def test_cancels_near_close(ledger):
    o = resting(ledger, market_close_time=utcnow() + timedelta(minutes=5))
    k = FakeKalshi()
    await OrderManager(ledger).manage_resting(k, ExecutionConfig(cancel_before_close_minutes=15))
    assert ledger.get_order(o.order_id).status == OrderStatus.CANCELLED


async def test_settlement_resolves_assessments(ledger):
    ledger.save_assessment(Assessment(market_ticker=T, fair_yes_prob=0.7))
    k = FakeKalshi()
    k.settlements = [Settlement(ticker=T, market_result="yes", yes_count=5,
                                yes_total_cost=205, revenue=500, fee_cost=0)]
    mgr = OrderManager(ledger)
    assert await mgr.sync_settlements(k) == 1
    assert await mgr.sync_settlements(k) == 0
    a = ledger.list_assessments(resolved_only=True)
    assert len(a) == 1 and a[0].resolved_yes is True
    assert ledger.list_settlements()[0].pnl_cents == 295


async def test_untraded_assessments_scored(ledger):
    ledger.save_assessment(Assessment(market_ticker="KXNBA-X-Y", fair_yes_prob=0.3))
    k = FakeKalshi()
    k.markets["KXNBA-X-Y"] = Market(ticker="KXNBA-X-Y", result="no")
    assert await OrderManager(ledger).resolve_assessments(k) == 1
    assert ledger.list_assessments(resolved_only=True)[0].resolved_yes is False
