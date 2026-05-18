"""Tests for the deterministic QA rule engine."""

import dataclasses
from datetime import datetime, timezone

import pytest

from src.common.schemas import (
    Confidence,
    DecisionVerdict,
    Recommendation,
    Side,
    TimeHorizon,
    TrustTier,
)
from src.kalshi_client.models import Orderbook, OrderbookLevel
from src.qa.rules import (
    PortfolioContext,
    RiskConfig,
    evaluate,
    gate_brier_score,
    gate_daily_drawdown,
    gate_event_concentration,
    gate_liquidity,
    gate_max_deployed,
    gate_position_size,
    gate_price_staleness,
    gate_source_quality,
)


def _make_config(**overrides) -> RiskConfig:
    defaults = dict(
        bankroll_cents=100_000,
        max_total_deployed_cents=80_000,
        max_loss_cents=5_000,
        max_position_pct=0.05,
        max_size_contracts=200,
        max_drawdown_cents=10_000,
        max_new_trades_per_day=10,
        max_open_exposure_cents=50_000,
        max_pct_per_event=0.20,
        max_pct_per_category=0.30,
        max_correlated_positions=3,
        min_open_interest=100,
        min_daily_volume=50,
        max_spread_bps=500,
        max_price_staleness_bps=500,
        max_brier_score=None,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _make_rec(**overrides) -> Recommendation:
    base = dict(
        market_ticker="KXPRES-25NOV-X",
        event_ticker="KXPRES-25NOV",
        side=Side.YES,
        limit_price_cents=45,
        target_size_contracts=20,
        estimated_fair_prob=0.60,
        market_implied_prob=0.45,
        edge_bps=1500,
        time_horizon=TimeHorizon.SWING,
        exit_conditions={"take_profit_prob": 0.68, "stop_loss_prob": 0.38, "invalidation_event": "Poll reversal"},
        confidence=Confidence.MEDIUM,
        thesis="Strong indicator X supports YES.",
        sources=[{"title": "AP Report", "url": "https://ap.org/1", "retrieved_at": datetime.utcnow(), "trust_tier": TrustTier.PRIMARY}],
        hard_constraints_declared={"must_be_in_universe": True, "respects_max_position_pct": True},
    )
    base.update(overrides)
    return Recommendation(**base)


def _make_ob(bid: int = 43, ask: int = 47) -> Orderbook:
    return Orderbook(
        market_ticker="KXPRES-25NOV-X",
        yes=[OrderbookLevel(price=bid, delta=100)],
        no=[OrderbookLevel(price=100 - ask, delta=100)],
    )


def _make_ctx(**overrides) -> PortfolioContext:
    defaults = dict(
        current_deployed_cents=0,
        daily_pnl_cents=0,
        today_new_trades=0,
        open_exposure_cents=0,
        positions_by_event={},
        positions_by_category={},
        correlated_count=0,
    )
    defaults.update(overrides)
    return PortfolioContext(**defaults)


class TestGatePositionSize:
    def test_passes_normal(self):
        rec = _make_rec(target_size_contracts=10, limit_price_cents=50)
        cfg = _make_config(max_loss_cents=5000, max_size_contracts=100)
        result = gate_position_size(rec, cfg, _make_ob())
        assert result.passed

    def test_fails_too_large(self):
        rec = _make_rec(target_size_contracts=200, limit_price_cents=50)
        cfg = _make_config(max_loss_cents=500)
        result = gate_position_size(rec, cfg, _make_ob())
        assert not result.passed
        assert "SIZE_EXCEEDS" in result.code

    def test_fails_contract_cap(self):
        rec = _make_rec(target_size_contracts=500)
        cfg = _make_config(max_size_contracts=100)
        result = gate_position_size(rec, cfg, _make_ob())
        assert not result.passed


class TestGateDailyDrawdown:
    def test_passes_under_budget(self):
        ctx = _make_ctx(daily_pnl_cents=-2000)
        cfg = _make_config(max_drawdown_cents=10_000)
        result = gate_daily_drawdown(ctx, cfg, trade_loss=3000)
        assert result.passed

    def test_fails_over_budget(self):
        ctx = _make_ctx(daily_pnl_cents=-8000)
        cfg = _make_config(max_drawdown_cents=10_000)
        result = gate_daily_drawdown(ctx, cfg, trade_loss=3000)
        assert not result.passed


class TestGateMaxDeployed:
    def test_passes(self):
        ctx = _make_ctx(current_deployed_cents=50_000)
        cfg = _make_config(max_total_deployed_cents=80_000)
        result = gate_max_deployed(ctx, cfg, new_exposure=20_000)
        assert result.passed

    def test_fails(self):
        ctx = _make_ctx(current_deployed_cents=70_000)
        cfg = _make_config(max_total_deployed_cents=80_000)
        result = gate_max_deployed(ctx, cfg, new_exposure=15_000)
        assert not result.passed


class TestGatePriceStaleness:
    def test_passes_close_price(self):
        rec = _make_rec(limit_price_cents=45)
        ob = _make_ob(bid=43, ask=47)  # mid = 45
        result = gate_price_staleness(rec, ob)
        assert result.passed

    def test_fails_large_drift(self):
        rec = _make_rec(limit_price_cents=45)
        ob = _make_ob(bid=10, ask=15)  # mid = ~12.5, huge drift from 45
        result = gate_price_staleness(rec, ob)
        assert not result.passed


class TestGateSourceQuality:
    def test_passes_with_tier1(self):
        rec = _make_rec()
        result = gate_source_quality(rec)
        assert result.passed

    def test_fails_social_only(self):
        # Bypass the Recommendation constructor (which enforces tier-1) and test the gate directly
        from unittest.mock import MagicMock
        from src.common.schemas import Source
        mock_rec = MagicMock()
        mock_rec.sources = [
            Source(title="Reddit post", url="https://reddit.com/r/1",
                   retrieved_at=datetime.utcnow(), trust_tier=TrustTier.SOCIAL)
        ]
        result = gate_source_quality(mock_rec)
        assert not result.passed


class TestGateLiquidity:
    def test_passes(self):
        rec = _make_rec()
        cfg = _make_config(min_open_interest=100, min_daily_volume=50)
        market_data = {"open_interest": 500, "volume": 200}
        result = gate_liquidity(rec, market_data, cfg)
        assert result.passed

    def test_fails_low_oi(self):
        rec = _make_rec()
        cfg = _make_config(min_open_interest=1000)
        market_data = {"open_interest": 50, "volume": 200}
        result = gate_liquidity(rec, market_data, cfg)
        assert not result.passed


class TestGateBrierScore:
    def test_skips_when_disabled(self):
        rec = _make_rec()
        cfg = _make_config(max_brier_score=None)
        ctx = _make_ctx()
        result = gate_brier_score(rec, ctx, cfg)
        assert result.passed
        assert result.code == "BRIER_GATE_DISABLED"

    def test_skips_insufficient_history(self):
        rec = _make_rec()
        cfg = _make_config(max_brier_score=0.25, min_trades_for_brier_gate=20)
        ctx = _make_ctx(research_trade_count_by_category={"KXPRES": 5})
        result = gate_brier_score(rec, ctx, cfg)
        assert result.passed

    def test_fails_high_brier(self):
        rec = _make_rec()
        cfg = _make_config(max_brier_score=0.25, min_trades_for_brier_gate=10)
        ctx = _make_ctx(
            research_trade_count_by_category={"KXPRES": 15},
            research_brier_by_category={"KXPRES": 0.35},
        )
        result = gate_brier_score(rec, ctx, cfg)
        assert not result.passed


class TestEvaluateFull:
    def test_approve_clean_trade(self):
        rec = _make_rec(target_size_contracts=5, limit_price_cents=45)
        cfg = _make_config()
        ctx = _make_ctx()
        ob = _make_ob()
        market_data = {"open_interest": 1000, "volume": 500}
        decision = evaluate(rec, cfg, ctx, ob, market_data)
        assert decision.decision in (DecisionVerdict.APPROVE, DecisionVerdict.MODIFY)
        assert decision.approved_order is not None

    def test_deny_oversized(self):
        rec = _make_rec(target_size_contracts=1000, limit_price_cents=90)
        cfg = _make_config(max_loss_cents=1000, max_size_contracts=10)
        ctx = _make_ctx()
        ob = _make_ob(bid=88, ask=92)
        market_data = {"open_interest": 1000, "volume": 500}
        decision = evaluate(rec, cfg, ctx, ob, market_data)
        assert decision.decision == DecisionVerdict.DENY
        assert decision.approved_order is None
