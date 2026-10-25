"""Tests for the SQLite ledger."""

from datetime import timedelta

from sqlalchemy import text

from src.common.schemas import (
    Assessment,
    Decision,
    DecisionVerdict,
    Fill,
    MarketSettlement,
    Order,
    OrderStatus,
    Recommendation,
    ResearchRun,
    Side,
    utcnow,
)


def _rec(ticker="KXTEST-1") -> Recommendation:
    return Recommendation(market_ticker=ticker, event_ticker="KXTEST", side=Side.YES,
                          limit_price_cents=50, target_size_contracts=10, estimated_fair_prob=0.6)


def _order(**kw) -> Order:
    base = dict(client_order_id="c", rec_id="rec-1", market_ticker="KXTEST-1", side=Side.YES,
                limit_price_cents=50, size_contracts=10, status=OrderStatus.PENDING_APPROVAL)
    base.update(kw)
    return Order(**base)


class TestRecommendationsAndDecisions:
    def test_round_trip(self, ledger):
        rec = _rec()
        ledger.save_recommendation(rec)
        ledger.save_decision(Decision(rec_id=rec.rec_id, decision=DecisionVerdict.APPROVE,
                                      reason_codes=["EDGE_OK"]))
        assert ledger.get_recommendation(rec.rec_id).market_ticker == rec.market_ticker
        assert ledger.get_decision(rec.rec_id).decision == DecisionVerdict.APPROVE
        assert ledger.get_recommendation("nonexistent") is None

    def test_ids_are_unique_per_recommendation(self, ledger):
        a, b = _rec(), _rec()
        ledger.save_recommendation(a)
        ledger.save_recommendation(b)
        with ledger._session() as s:
            assert s.execute(text("SELECT COUNT(*) FROM recommendations")).scalar() == 2


class TestOrders:
    def test_status_query_with_several_statuses(self, ledger):
        ledger.save_order(_order(status=OrderStatus.SUBMITTED))
        ledger.save_order(_order(status=OrderStatus.PARTIALLY_FILLED))
        ledger.save_order(_order(status=OrderStatus.FILLED))
        got = ledger.get_orders_by_status("SUBMITTED", "PARTIALLY_FILLED")
        assert len(got) == 2

    def test_status_query_is_parameterized(self, ledger):
        ledger.save_order(_order())
        assert ledger.get_orders_by_status("x') OR ('1'='1") == []

    def test_lookup_by_kalshi_id(self, ledger):
        o = _order(kalshi_order_id="k-9", status=OrderStatus.SUBMITTED)
        ledger.save_order(o)
        assert ledger.get_order_by_kalshi_id("k-9").order_id == o.order_id

    def test_expire_unsent(self, ledger):
        old = _order(status=OrderStatus.APPROVED, last_updated_at=utcnow() - timedelta(hours=30))
        sent = _order(status=OrderStatus.APPROVED, kalshi_order_id="k1",
                      last_updated_at=utcnow() - timedelta(hours=30))
        fresh = _order(status=OrderStatus.APPROVED)
        for o in (old, sent, fresh):
            ledger.save_order(o)
        assert ledger.expire_unsent_orders(24) == 1
        assert ledger.get_order(old.order_id).status == OrderStatus.EXPIRED


class TestFillsAndSettlements:
    def test_fill_dedupe(self, ledger):
        ledger.save_fill(Fill(kalshi_trade_id="t1", order_id="o1", rec_id="r1",
                              market_ticker="KXTEST-1", side=Side.YES, price_cents=50,
                              count=2, is_taker=False))
        assert ledger.has_fill("t1") and not ledger.has_fill("t2")
        assert ledger.latest_fill_time() is not None

    def test_settlement_recorded_once(self, ledger):
        st = MarketSettlement(market_ticker="KXTEST-1", result="yes", pnl_cents=40,
                              settled_at=utcnow())
        assert ledger.save_settlement(st) is True
        assert ledger.save_settlement(st) is False


class TestAssessmentsAndRuns:
    def test_recent_and_resolution(self, ledger):
        ledger.save_assessment(Assessment(market_ticker="A", fair_yes_prob=0.4))
        ledger.save_assessment(Assessment(market_ticker="B", skip_reason="no info"))
        assert ledger.recently_assessed_tickers(1) == {"A", "B"}
        assert ledger.unresolved_assessment_tickers() == {"A", "B"}
        assert ledger.resolve_assessments("A", False, utcnow()) == 1
        assert [a.market_ticker for a in ledger.list_assessments(resolved_only=True)] == ["A"]

    def test_run_spend(self, ledger):
        ledger.save_run(ResearchRun(cost_usd=0.12))
        ledger.save_run(ResearchRun(cost_usd=0.08))
        assert abs(ledger.api_spend_since(utcnow() - timedelta(days=1)) - 0.20) < 1e-9
        assert len(ledger.list_runs()) == 2


class TestSystemState:
    def test_halt_resume(self, ledger, tmp_path):
        assert not ledger.is_halted()
        ledger.halt("test halt")
        assert ledger.is_halted() and (tmp_path / "HALT").exists()
        ledger.resume()
        assert not ledger.is_halted()

    def test_audit_chain(self, ledger):
        ledger.append_audit("E1", "test", {"k": "¢ ok"})
        ledger.append_audit("E2", "test", {"k": 2})
        rows = ledger.list_audit()
        assert [r["type"] for r in rows[:2]] == ["E2", "E1"]
