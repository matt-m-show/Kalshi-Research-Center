"""Tests for performance metrics."""

from datetime import datetime

import pytest

from src.performance.metrics import (
    ClosedTrade,
    brier_score,
    compute_rolling_metrics,
    edge_capture,
    hit_rate_by_confidence,
    log_loss,
    max_drawdown,
    realized_pnl_cents,
    sharpe_ratio,
    sortino_ratio,
)


def _trade(prob: float, fill: float, resolved: float, size: int = 10, conf: str = "medium") -> ClosedTrade:
    return ClosedTrade(
        rec_id="r1",
        market_ticker="KXTEST-1",
        category="KXTEST",
        side="YES",
        avg_fill_price_cents=fill,
        size_contracts=size,
        resolved_price_cents=resolved,
        estimated_fair_prob=prob,
        confidence=conf,
        closed_at=datetime.utcnow(),
    )


class TestRealizedPnl:
    def test_winning_yes(self):
        t = _trade(0.6, 50, 100)  # bought YES at 50¢, resolved YES (100¢)
        assert realized_pnl_cents(t) == pytest.approx(500.0)

    def test_losing_yes(self):
        t = _trade(0.6, 50, 0)  # bought YES at 50¢, resolved NO (0¢)
        assert realized_pnl_cents(t) == pytest.approx(-500.0)


class TestBrierScore:
    def test_perfect_calibration(self):
        trades = [_trade(1.0, 50, 100), _trade(0.0, 50, 0)]
        bs = brier_score(trades)
        assert bs == pytest.approx(0.0)

    def test_worst_calibration(self):
        trades = [_trade(0.0, 50, 100), _trade(1.0, 50, 0)]
        bs = brier_score(trades)
        assert bs == pytest.approx(1.0)

    def test_empty(self):
        assert brier_score([]) is None


class TestMaxDrawdown:
    def test_no_drawdown(self):
        assert max_drawdown([100, 200, 300]) == pytest.approx(0.0)

    def test_basic_drawdown(self):
        # gains 300, then loses 200 → drawdown of 200
        assert max_drawdown([100, 100, 100, -200]) == pytest.approx(200.0)

    def test_empty(self):
        assert max_drawdown([]) == pytest.approx(0.0)


class TestSharpe:
    def test_none_with_no_variance(self):
        assert sharpe_ratio([0.01, 0.01, 0.01]) is None

    def test_positive_sharpe(self):
        returns = [0.01] * 20 + [-0.005] * 5
        s = sharpe_ratio(returns)
        assert s is not None and s > 0


class TestComputeRollingMetrics:
    def test_basic(self):
        trades = [
            _trade(0.65, 45, 100, conf="high"),
            _trade(0.55, 50, 0, conf="medium"),
            _trade(0.70, 40, 100, conf="high"),
        ]
        m = compute_rolling_metrics(trades)
        assert m.total_trades == 3
        assert m.brier_score is not None
        assert "high" in m.hit_rate_by_confidence
