"""Deterministic QA / risk engine.

Two stages:
  1. size_position() — fractional-Kelly sizing, shrunk to fit every hard cap
     (per-trade loss, contract cap, deploy ceiling, open exposure, event and
     category concentration). A trade that can't fit even 1 contract is denied.
  2. evaluate() — hard gates that deny outright (drawdown, trade count,
     correlation, liquidity, edge, sources, calibration).

All thresholds come from config/risk.yaml via RiskConfig, so this module does
no I/O and is trivially unit-testable.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from src.common.schemas import (
    ApprovedOrder,
    Confidence,
    Decision,
    DecisionVerdict,
    Recommendation,
    TrustTier,
    utcnow,
)
from src.qa.kelly import kelly_contracts


# ---------------------------------------------------------------------------
# Config & context
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RiskConfig:
    bankroll_cents: int
    max_total_deployed_cents: int
    max_loss_cents: int
    max_position_pct: float            # fraction of bankroll
    max_size_contracts: int
    max_drawdown_cents: int            # per day
    max_new_trades_per_day: int
    max_open_exposure_cents: int
    max_pct_per_event: float           # fraction of bankroll
    max_pct_per_category: float        # fraction of bankroll
    max_correlated_positions: int
    min_open_interest: int
    min_daily_volume: int
    min_edge_cents: float = 3.0
    kelly_fraction: float = 0.25
    max_brier_score: float | None = None
    min_trades_for_brier_gate: int = 20
    consecutive_losses_halt: int = 5
    portfolio_drawdown_halt_pct: float = 0.20


@dataclasses.dataclass
class PortfolioContext:
    current_deployed_cents: int = 0          # open positions + resting entry orders
    daily_pnl_cents: int = 0
    today_new_trades: int = 0
    open_exposure_cents: int = 0
    positions_by_event: dict[str, int] = dataclasses.field(default_factory=dict)       # cents
    positions_by_category: dict[str, int] = dataclasses.field(default_factory=dict)    # cents
    positions_count_by_event: dict[str, int] = dataclasses.field(default_factory=dict)
    held_tickers: set[str] = dataclasses.field(default_factory=set)
    available_cash_cents: int | None = None  # live Kalshi balance, when known
    research_brier_by_category: dict[str, float] = dataclasses.field(default_factory=dict)
    research_trade_count_by_category: dict[str, int] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class GateResult:
    passed: bool
    code: str
    detail: str = ""


def category_of(ticker: str) -> str:
    return ticker.split("-")[0].upper()


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def effective_bankroll(cfg: RiskConfig, ctx: PortfolioContext) -> int:
    """Configured bankroll, but never more than what is actually in the account."""
    if ctx.available_cash_cents is None:
        return cfg.bankroll_cents
    return min(cfg.bankroll_cents, ctx.available_cash_cents + ctx.current_deployed_cents)


def size_position(
    rec: Recommendation, cfg: RiskConfig, ctx: PortfolioContext
) -> tuple[int, str]:
    """Return (contracts, binding_constraint)."""
    price = rec.limit_price_cents
    bankroll = effective_bankroll(cfg, ctx)
    event_cap = int(cfg.max_pct_per_event * bankroll) - ctx.positions_by_event.get(rec.event_ticker, 0)
    category_cap = int(cfg.max_pct_per_category * bankroll) - ctx.positions_by_category.get(
        category_of(rec.market_ticker), 0
    )
    budgets = {
        "KELLY": kelly_contracts(
            rec.estimated_fair_prob, price, bankroll, cfg.max_position_pct,
            cfg.max_size_contracts, cfg.kelly_fraction,
        ),
        "MAX_LOSS": cfg.max_loss_cents // price,
        "CONTRACT_CAP": cfg.max_size_contracts,
        "DEPLOY_CEILING": (cfg.max_total_deployed_cents - ctx.current_deployed_cents) // price,
        "OPEN_EXPOSURE": (cfg.max_open_exposure_cents - ctx.open_exposure_cents) // price,
        "EVENT_CONCENTRATION": event_cap // price,
        "CATEGORY_CONCENTRATION": category_cap // price,
    }
    if ctx.available_cash_cents is not None:
        budgets["CASH"] = ctx.available_cash_cents // price
    binding = min(budgets, key=lambda k: budgets[k])
    return max(0, budgets[binding]), binding


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


def gate_daily_drawdown(ctx: PortfolioContext, cfg: RiskConfig, trade_loss: int) -> GateResult:
    projected = abs(min(0, ctx.daily_pnl_cents)) + trade_loss
    if projected > cfg.max_drawdown_cents:
        return GateResult(False, "DAILY_DRAWDOWN_BREACH",
                          f"Projected daily drawdown {projected}¢ > budget {cfg.max_drawdown_cents}¢")
    return GateResult(True, "DRAWDOWN_OK")


def gate_daily_trade_count(ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    if ctx.today_new_trades >= cfg.max_new_trades_per_day:
        return GateResult(False, "DAILY_TRADE_CAP",
                          f"{ctx.today_new_trades} trades already today (max {cfg.max_new_trades_per_day})")
    return GateResult(True, "TRADE_COUNT_OK")


def gate_correlated_positions(rec: Recommendation, ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    n = ctx.positions_count_by_event.get(rec.event_ticker, 0)
    if n >= cfg.max_correlated_positions:
        return GateResult(False, "CORRELATED_POSITION_CAP",
                          f"{n} open positions in {rec.event_ticker} (limit {cfg.max_correlated_positions})")
    return GateResult(True, "CORRELATION_OK")


def gate_already_held(rec: Recommendation, ctx: PortfolioContext) -> GateResult:
    if rec.market_ticker in ctx.held_tickers:
        return GateResult(False, "ALREADY_POSITIONED",
                          f"Already holding or bidding on {rec.market_ticker}")
    return GateResult(True, "NOT_HELD_OK")


def gate_liquidity(rec: Recommendation, market_data: dict, cfg: RiskConfig) -> GateResult:
    oi = market_data.get("open_interest", 0)
    vol = market_data.get("volume", 0)
    if oi < cfg.min_open_interest:
        return GateResult(False, "LOW_OPEN_INTEREST", f"OI {oi} < floor {cfg.min_open_interest}")
    if vol < cfg.min_daily_volume:
        return GateResult(False, "LOW_VOLUME", f"Volume {vol} < floor {cfg.min_daily_volume}")
    return GateResult(True, "LIQUIDITY_OK")


def gate_edge(rec: Recommendation, cfg: RiskConfig) -> GateResult:
    edge = rec.edge_cents_after_fees
    if edge is None or edge < cfg.min_edge_cents - 1e-9:
        return GateResult(False, "EDGE_BELOW_MIN",
                          f"Edge {edge if edge is not None else 'n/a'}¢ after fees < {cfg.min_edge_cents}¢")
    return GateResult(True, "EDGE_OK")


def gate_source_quality(rec: Recommendation) -> GateResult:
    tiers = {s.trust_tier for s in rec.sources}
    if not tiers:
        return GateResult(False, "NO_SOURCES", "No sources cited")
    if TrustTier.PRIMARY not in tiers:
        return GateResult(False, "NO_TIER1_SOURCE", "No primary (tier-1) source cited")
    return GateResult(True, "SOURCE_QUALITY_OK")


def gate_brier_score(rec: Recommendation, ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    if cfg.max_brier_score is None:
        return GateResult(True, "BRIER_GATE_DISABLED")
    category = category_of(rec.market_ticker)
    if ctx.research_trade_count_by_category.get(category, 0) < cfg.min_trades_for_brier_gate:
        return GateResult(True, "BRIER_INSUFFICIENT_HISTORY")
    brier = ctx.research_brier_by_category.get(category, 0.0)
    if brier > cfg.max_brier_score:
        return GateResult(False, "BRIER_THRESHOLD_BREACH",
                          f"Brier {brier:.3f} > threshold {cfg.max_brier_score:.3f} for {category}")
    return GateResult(True, "BRIER_OK")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def evaluate(
    rec: Recommendation,
    cfg: RiskConfig,
    ctx: PortfolioContext,
    market_data: dict,
    order_ttl_hours: float = 6.0,
    cancel_before_close_minutes: int = 15,
) -> Decision:
    """Size the trade, run the hard gates, and return a Decision."""
    reason_codes: list[str] = []
    deny: list[str] = []
    modifications: dict = {}

    size, binding = size_position(rec, cfg, ctx)
    if rec.confidence == Confidence.LOW and size > 1:
        size = max(1, size // 2)
        modifications["_low_confidence_halved"] = True
    if size != rec.target_size_contracts:
        modifications["target_size_contracts"] = size
        modifications["_binding_constraint"] = binding
    reason_codes.append(f"SIZE_{binding}")

    if size < 1:
        deny.append(f"SIZE_ZERO: no room for even 1 contract (binding: {binding})")

    gates = [
        gate_daily_drawdown(ctx, cfg, size * rec.limit_price_cents),
        gate_daily_trade_count(ctx, cfg),
        gate_correlated_positions(rec, ctx, cfg),
        gate_already_held(rec, ctx),
        gate_liquidity(rec, market_data, cfg),
        gate_edge(rec, cfg),
        gate_source_quality(rec),
        gate_brier_score(rec, ctx, cfg),
    ]
    for g in gates:
        reason_codes.append(g.code)
        if not g.passed:
            deny.append(f"{g.code}: {g.detail}")

    if deny:
        return Decision(
            rec_id=rec.rec_id,
            decision=DecisionVerdict.DENY,
            reason_codes=reason_codes + (["SIZE_ZERO"] if size < 1 else []),
            approved_order=None,
            qa_notes="; ".join(deny),
        )

    # Order expires at the earlier of the TTL and shortly before market close
    expiry = utcnow() + timedelta(hours=order_ttl_hours)
    if rec.market_close_time is not None:
        close = rec.market_close_time.replace(tzinfo=None) - timedelta(minutes=cancel_before_close_minutes)
        expiry = min(expiry, close)

    approved_order = ApprovedOrder(
        market_ticker=rec.market_ticker,
        side=rec.side,
        limit_price_cents=rec.limit_price_cents,
        max_price_cents=rec.max_price_cents or rec.limit_price_cents,
        size_contracts=size,
        expiry_ts=expiry,
    )
    verdict = DecisionVerdict.MODIFY if modifications else DecisionVerdict.APPROVE
    notes = [f"Sized to {size} contract{'s' if size != 1 else ''} "
             f"(limited by {_BINDING_TEXT.get(binding, binding)})"]
    if modifications.get("_low_confidence_halved"):
        notes.append("halved for low confidence")
    return Decision(
        rec_id=rec.rec_id,
        decision=verdict,
        reason_codes=reason_codes,
        modifications=modifications,
        approved_order=approved_order,
        qa_notes="; ".join(notes),
    )


_BINDING_TEXT = {
    "KELLY": "Kelly sizing",
    "MAX_LOSS": "max loss per trade",
    "CONTRACT_CAP": "max contracts per trade",
    "DEPLOY_CEILING": "total deployment cap",
    "OPEN_EXPOSURE": "open exposure cap",
    "EVENT_CONCENTRATION": "per-event cap",
    "CATEGORY_CONCENTRATION": "per-series cap",
    "CASH": "available cash",
}
