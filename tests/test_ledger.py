"""Tests for the SQLite ledger."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.common.ledger import Ledger
from src.common.schemas import (
    Confidence,
    Decision,
    DecisionVerdict,
    Fill,
    KalshiEnv,
    Order,
    OrderStatus,
    Recommendation,
    Side,
    SystemState,
    TimeHorizon,
    TrustTier,
)


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "test_ledger.db"
    import unittest.mock as mock
    # Redirect DATA_DIR and AUDIT_LOG to tmp
    with mock.patch("src.common.ledger.DATA_DIR", tmp_path), \
         mock.patch("src.common.ledger.AUDIT_LOG", tmp_path / "audit.log"):
        yield Ledger(db_path=db)


def _make_rec(ticker="KXTEST-1") -> Recommendation:
    return Recommendation(
        market_ticker=ticker,
        event_ticker="KXTEST",
        side=Side.YES,
        limit_price_cents=50,
        target_size_contracts=10,
        estimated_fair_prob=0.60,
        market_implied_prob=0.50,
        edge_bps=1000,
        time_horizon=TimeHorizon.SWING,
        exit_conditions={"take_profit_prob": 0.68, "stop_loss_prob": 0.40, "invalidation_event": "None"},
        confidence=Confidence.MEDIUM,
        thesis="Test thesis.",
        sources=[{"title": "AP", "url": "https://ap.org", "retrieved_at": datetime.utcnow(), "trust_tier": TrustTier.PRIMARY}],
        hard_constraints_declared={"must_be_in_universe": True, "respects_max_position_pct": True},
    )


class TestRecommendations:
    def test_save_and_retrieve(self, ledger):
        rec = _make_rec()
        ledger.save_recommendation(rec)
        retrieved = ledger.get_recommendation(rec.rec_id)
        assert retrieved is not None
        assert retrieved.rec_id == rec.rec_id
        assert retrieved.market_ticker == rec.market_ticker

    def test_missing_returns_none(self, ledger):
        assert ledger.get_recommendation("nonexistent") is None


class TestDecisions:
    def test_save_and_retrieve(self, ledger):
        rec = _make_rec()
        ledger.save_recommendation(rec)
        decision = Decision(
            rec_id=rec.rec_id,
            decision=DecisionVerdict.APPROVE,
            reason_codes=["SIZE_OK"],
        )
        ledger.save_decision(decision)
        retrieved = ledger.get_decision(rec.rec_id)
        assert retrieved is not None
        assert retrieved.decision == DecisionVerdict.APPROVE


class TestOrders:
    def test_save_and_retrieve_by_status(self, ledger):
        order = Order(
            client_order_id="test-coid-1",
            rec_id="rec-1",
            market_ticker="KXTEST-1",
            side=Side.YES,
            limit_price_cents=50,
            size_contracts=10,
            status=OrderStatus.PENDING_APPROVAL,
        )
        ledger.save_order(order)
        pending = ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
        assert any(o.order_id == order.order_id for o in pending)


class TestSystemState:
    def test_default_not_halted(self, ledger):
        assert not ledger.is_halted()

    def test_halt_and_resume(self, ledger):
        ledger.halt("test halt")
        assert ledger.is_halted()
        ledger.resume()
        assert not ledger.is_halted()

    def test_halt_file_created(self, ledger, tmp_path):
        import unittest.mock as mock
        with mock.patch("src.common.ledger.DATA_DIR", tmp_path):
            ledger.halt("test")
            assert (tmp_path / "HALT").exists()


class TestAuditLog:
    def test_append_and_chain(self, ledger):
        ledger.append_audit("TEST_EVENT_1", "test", {"key": "val1"})
        ledger.append_audit("TEST_EVENT_2", "test", {"key": "val2"})
        # No assertion on hash value — just verify no exceptions and both written
        with ledger._session() as s:
            from sqlalchemy import text
            rows = s.execute(text("SELECT seq, event_type FROM audit ORDER BY seq")).fetchall()
        types = [r[1] for r in rows]
        assert "TEST_EVENT_1" in types
        assert "TEST_EVENT_2" in types
