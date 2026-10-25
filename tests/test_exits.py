"""Tests for exit planning, triggered exits, exit P&L and the news watch."""

from datetime import timedelta

from src.common.config import ExitConfig
from src.common.schemas import (
    Assessment,
    Fill,
    Order,
    OrderPurpose,
    OrderStatus,
    Recommendation,
    Side,
    utcnow,
)
from src.execute.exits import decide, plan_for
from src.execute.manager import OrderManager
from src.kalshi_client.models import KalshiFill, Position
from tests.fakes import FakeKalshi, book

T = "KXNBAGAME-26OCT26LALBOS-LAL"
CFG = ExitConfig(take_profit_capture=0.8, stop_loss_pct=0.5, stop_loss_min_hours_to_close=0.5,
                 max_slippage_cents=2, news_check_minutes=0)


def held(ledger, entry=40, fair_yes=0.60, contracts=5, close_in_hours=6.0, invalidation="LeBron out"):
    a = Assessment(market_ticker=T, fair_yes_prob=fair_yes, invalidation_event=invalidation)
    ledger.save_assessment(a)
    rec = Recommendation(market_ticker=T, event_ticker="KXNBAGAME-26OCT26LALBOS", title="Lakers win",
                         side=Side.YES, limit_price_cents=entry, target_size_contracts=contracts,
                         estimated_fair_prob=fair_yes, fair_yes_prob=fair_yes, assessment_id=a.assessment_id,
                         market_close_time=utcnow() + timedelta(hours=close_in_hours))
    ledger.save_recommendation(rec)
    order = Order(client_order_id="c1", rec_id=rec.rec_id, market_ticker=T, side=Side.YES,
                  limit_price_cents=entry, size_contracts=contracts, filled_contracts=contracts,
                  status=OrderStatus.FILLED, kalshi_order_id="k-entry", submitted_at=utcnow())
    ledger.save_order(order)
    ledger.save_fill(Fill(kalshi_trade_id="f-entry", order_id=order.order_id, rec_id=rec.rec_id,
                          market_ticker=T, side=Side.YES, price_cents=entry, count=contracts,
                          is_taker=False, fee_cents=0))
    return rec


class TestPlan:
    def test_levels(self, ledger):
        held(ledger)
        plan = plan_for(ledger, T, Side.YES, 5, 200, CFG)
        assert plan.entry_cents == 40 and plan.fair_cents == 60
        assert plan.take_profit_cents == 56          # 40 + 0.8 × 20
        assert plan.stop_loss_cents == 20
        assert plan.invalidation_event == "LeBron out"

    def test_unmanaged_position(self, ledger):
        assert plan_for(ledger, "OTHER-T", Side.YES, 3, 100, CFG) is None

    def test_decisions(self, ledger):
        held(ledger)
        plan = plan_for(ledger, T, Side.YES, 5, 200, CFG)
        assert decide(plan, 50, CFG) is None
        assert decide(plan, 57, CFG).startswith("take profit")
        assert decide(plan, 19, CFG).startswith("stop loss")
        assert decide(plan, None, CFG) is None

    def test_no_stop_loss_right_before_close(self, ledger):
        held(ledger, close_in_hours=0.2)
        plan = plan_for(ledger, T, Side.YES, 5, 200, CFG)
        assert decide(plan, 10, CFG) is None


class TestManagerExits:
    async def test_take_profit_sells_reduce_only(self, ledger):
        held(ledger)
        k = FakeKalshi()
        k.positions = [Position(market_ticker=T, position=5, market_exposure=200)]
        k.books[T] = book(T, 58, 60)
        mgr = OrderManager(ledger)
        assert await mgr.manage_positions(k, CFG) == 1
        req = k.created[0]
        assert (req.side, req.price, req.time_in_force, req.reduce_only, req.count) == \
            ("ask", "0.56", "immediate_or_cancel", True, "5")
        # Doesn't re-fire on the next pass
        assert await mgr.manage_positions(k, CFG) == 0

    async def test_holds_inside_band(self, ledger):
        held(ledger)
        k = FakeKalshi()
        k.positions = [Position(market_ticker=T, position=5, market_exposure=200)]
        k.books[T] = book(T, 45, 47)
        assert await OrderManager(ledger).manage_positions(k, CFG) == 0

    async def test_exit_fill_records_realized_pnl(self, ledger):
        held(ledger)
        sell = Order(client_order_id="c2", rec_id="r", market_ticker=T, side=Side.YES, action="sell",
                     purpose=OrderPurpose.EXIT, limit_price_cents=56, size_contracts=5,
                     status=OrderStatus.FILLED, kalshi_order_id="k-exit")
        ledger.save_order(sell)
        k = FakeKalshi()
        k.fills = [KalshiFill(trade_id="f-exit", order_id="k-exit", ticker=T, outcome_side="no",
                              count=5, yes_price=58, no_price=42, fee=3)]
        await OrderManager(ledger).sync_fills(k)
        exits = [s for s in ledger.list_settlements() if s.result == "exit"]
        assert len(exits) == 1 and exits[0].pnl_cents == (58 - 40) * 5 - 3

    async def test_news_alert_then_exit(self, ledger):
        held(ledger)
        k = FakeKalshi()
        k.positions = [Position(market_ticker=T, position=5, market_exposure=200)]
        k.books[T] = book(T, 45, 47)
        events = []

        class Assist:
            async def check_invalidation(self, *a):
                return {"invalidated": True, "confidence": "high", "evidence": "LeBron ruled out", "url": ""}

        mgr = OrderManager(ledger, notify=lambda kind, d: events.append(kind))
        async def assist():
            return Assist()
        mgr._local_assist = assist
        cfg = ExitConfig(**{**CFG.__dict__, "news_check_minutes": 60, "on_invalidation": "alert"})
        assert await mgr.manage_positions(k, cfg) == 0
        assert events == ["thesis_invalidated"] and not k.created

        mgr._news_checked.clear()
        cfg.on_invalidation = "exit"
        assert await mgr.manage_positions(k, cfg) == 1
        assert k.created[0].reduce_only
