"""Tests for the deterministic QA rule engine."""

from datetime import timedelta

from src.common.schemas import (
    Confidence,
    DecisionVerdict,
    Recommendation,
    Side,
    Source,
    TrustTier,
    utcnow,
)
from src.qa.rules import (
    PortfolioContext,
    RiskConfig,
    effective_bankroll,
    evaluate,
    gate_brier_score,
    gate_daily_drawdown,
    gate_source_quality,
    size_position,
)

MARKET = {"open_interest": 1000, "volume": 500}


def cfg(**overrides) -> RiskConfig:
    base = dict(
        bankroll_cents=100_000, max_total_deployed_cents=80_000, max_loss_cents=5_000,
        max_position_pct=0.05, max_size_contracts=200, max_drawdown_cents=10_000,
        max_new_trades_per_day=10, max_open_exposure_cents=50_000, max_pct_per_event=0.20,
        max_pct_per_category=0.30, max_correlated_positions=3, min_open_interest=100,
        min_daily_volume=50, min_edge_cents=3.0,
    )
    base.update(overrides)
    return RiskConfig(**base)


def rec(**overrides) -> Recommendation:
    base = dict(
        market_ticker="KXPRES-25NOV-X", event_ticker="KXPRES-25NOV", side=Side.YES,
        limit_price_cents=45, max_price_cents=55, target_size_contracts=0,
        estimated_fair_prob=0.60, fair_yes_prob=0.60, edge_cents_after_fees=15.0,
        confidence=Confidence.MEDIUM, thesis="Strong indicator X supports YES.",
        sources=[Source(title="AP", url="https://ap.org/1", trust_tier=TrustTier.PRIMARY)],
    )
    base.update(overrides)
    return Recommendation(**base)


class TestSizing:
    def test_kelly_binds_by_default(self):
        size, binding = size_position(rec(), cfg(), PortfolioContext())
        assert binding == "KELLY" and size > 0

    def test_max_loss_binds(self):
        size, binding = size_position(rec(), cfg(max_loss_cents=450, max_position_pct=1.0),
                                      PortfolioContext())
        assert (size, binding) == (10, "MAX_LOSS")

    def test_cash_binds(self):
        # Mostly deployed already: bankroll is large, but only $2 of cash is free
        ctx = PortfolioContext(available_cash_cents=200, current_deployed_cents=50_000)
        size, binding = size_position(rec(), cfg(), ctx)
        assert binding == "CASH" and size == 4

    def test_deploy_ceiling_binds(self):
        ctx = PortfolioContext(current_deployed_cents=79_900)
        size, binding = size_position(rec(), cfg(), ctx)
        assert binding == "DEPLOY_CEILING" and size == 2

    def test_event_concentration_binds(self):
        ctx = PortfolioContext(positions_by_event={"KXPRES-25NOV": 19_900})
        size, binding = size_position(rec(), cfg(), ctx)
        assert binding == "EVENT_CONCENTRATION" and size == 2

    def test_bankroll_capped_by_live_account(self):
        ctx = PortfolioContext(available_cash_cents=3_000, current_deployed_cents=2_000)
        assert effective_bankroll(cfg(), ctx) == 5_000

    def test_no_side_uses_side_probability(self):
        # Buying NO at 40¢ with 70% NO probability is a positive-Kelly trade
        size, _ = size_position(rec(side=Side.NO, limit_price_cents=40, estimated_fair_prob=0.70,
                                    fair_yes_prob=0.30), cfg(), PortfolioContext())
        assert size > 0


class TestGates:
    def test_drawdown(self):
        assert gate_daily_drawdown(PortfolioContext(daily_pnl_cents=-2000), cfg(), 3000).passed
        assert not gate_daily_drawdown(PortfolioContext(daily_pnl_cents=-8000), cfg(), 3000).passed

    def test_source_quality_requires_tier1(self):
        assert gate_source_quality(rec()).passed
        social = rec(sources=[Source(title="r", url="https://reddit.com", trust_tier=TrustTier.SOCIAL)])
        assert not gate_source_quality(social).passed
        assert not gate_source_quality(rec(sources=[])).passed

    def test_brier_gate(self):
        c = cfg(max_brier_score=0.25, min_trades_for_brier_gate=10)
        bad = PortfolioContext(research_trade_count_by_category={"KXPRES": 15},
                               research_brier_by_category={"KXPRES": 0.35})
        thin = PortfolioContext(research_trade_count_by_category={"KXPRES": 5})
        assert not gate_brier_score(rec(), bad, c).passed
        assert gate_brier_score(rec(), thin, c).passed
        assert gate_brier_score(rec(), bad, cfg()).code == "BRIER_GATE_DISABLED"


class TestEvaluate:
    def test_approves_and_sizes(self):
        d = evaluate(rec(), cfg(), PortfolioContext(), MARKET)
        assert d.decision in (DecisionVerdict.APPROVE, DecisionVerdict.MODIFY)
        ao = d.approved_order
        assert ao is not None and ao.size_contracts > 0
        assert ao.limit_price_cents == 45 and ao.max_price_cents == 55 and ao.post_only

    def test_denies_low_edge(self):
        d = evaluate(rec(edge_cents_after_fees=1.0), cfg(), PortfolioContext(), MARKET)
        assert d.decision == DecisionVerdict.DENY and "EDGE_BELOW_MIN" in d.qa_notes

    def test_denies_when_already_held(self):
        ctx = PortfolioContext(held_tickers={"KXPRES-25NOV-X"})
        assert evaluate(rec(), cfg(), ctx, MARKET).decision == DecisionVerdict.DENY

    def test_denies_correlated(self):
        ctx = PortfolioContext(positions_count_by_event={"KXPRES-25NOV": 3})
        assert evaluate(rec(), cfg(), ctx, MARKET).decision == DecisionVerdict.DENY

    def test_denies_size_zero(self):
        d = evaluate(rec(), cfg(), PortfolioContext(available_cash_cents=10), MARKET)
        assert d.decision == DecisionVerdict.DENY and "SIZE_ZERO" in d.reason_codes

    def test_low_confidence_halves(self):
        full = evaluate(rec(), cfg(), PortfolioContext(), MARKET).approved_order.size_contracts
        low = evaluate(rec(confidence=Confidence.LOW), cfg(), PortfolioContext(), MARKET)
        assert low.approved_order.size_contracts == max(1, full // 2)

    def test_expiry_before_market_close(self):
        close = utcnow() + timedelta(hours=1)
        d = evaluate(rec(market_close_time=close), cfg(), PortfolioContext(), MARKET,
                     order_ttl_hours=6, cancel_before_close_minutes=15)
        assert d.approved_order.expiry_ts <= close - timedelta(minutes=14)
