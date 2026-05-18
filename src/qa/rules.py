"""Deterministic QA / Risk rule engine.

Hard gates:  deny the trade if any gate fails.
Soft checks: may modify size or price, but do not deny.

All thresholds come from config/risk.yaml; passed in as a RiskConfig dataclass
so this module has no file-system dependency and is trivially unit-testable.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from src.common.schemas import (
    ApprovedOrder,
    Confidence,
    Decision,
    DecisionVerdict,
    Recommendation,
    Side,
    TrustTier,
)
from src.kalshi_client.models import Orderbook
from src.qa.kelly import kelly_contracts, worst_case_loss_cents


# ---------------------------------------------------------------------------
# Config dataclass (populated from risk.yaml by the orchestrator)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RiskConfig:
    # Bankroll
    bankroll_cents: int
    max_total_deployed_cents: int          # hard ceiling on total deployed capital

    # Per-trade limits
    max_loss_cents: int
    max_position_pct: float                # fraction of bankroll, e.g. 0.02
    max_size_contracts: int

    # Daily limits
    max_drawdown_cents: int
    max_new_trades_per_day: int
    max_open_exposure_cents: int

    # Concentration
    max_pct_per_event: float
    max_pct_per_category: float
    max_correlated_positions: int

    # Liquidity floors
    min_open_interest: int
    min_daily_volume: int
    max_spread_bps: int

    # Price staleness
    max_price_staleness_bps: int           # bps drift before aborting pre-flight

    # Research Agent quality gating
    max_brier_score: float | None          # deny if rolling Brier > this (None = skip)
    min_trades_for_brier_gate: int = 20    # don't apply Brier gate until N trades

    # Kill switches
    consecutive_losses_halt: int = 5
    portfolio_drawdown_halt_pct: float = 0.20


# ---------------------------------------------------------------------------
# Portfolio context (passed in per evaluation)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PortfolioContext:
    current_deployed_cents: int = 0
    daily_pnl_cents: int = 0
    today_new_trades: int = 0
    open_exposure_cents: int = 0
    positions_by_event: dict[str, int] = dataclasses.field(default_factory=dict)   # event_ticker -> cents
    positions_by_category: dict[str, int] = dataclasses.field(default_factory=dict)
    correlated_count: int = 0             # open positions sharing this event
    research_brier_by_category: dict[str, float] = dataclasses.field(default_factory=dict)
    research_trade_count_by_category: dict[str, int] = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GateResult:
    passed: bool
    code: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


def gate_position_size(rec: Recommendation, cfg: RiskConfig, orderbook: Orderbook) -> GateResult:
    """Worst-case loss must not exceed per-trade limit."""
    loss = worst_case_loss_cents(rec.target_size_contracts, rec.limit_price_cents)
    if loss > cfg.max_loss_cents:
        return GateResult(False, "SIZE_EXCEEDS_MAX_LOSS",
                          f"Worst-case loss {loss}¢ > limit {cfg.max_loss_cents}¢")
    if rec.target_size_contracts > cfg.max_size_contracts:
        return GateResult(False, "SIZE_EXCEEDS_CONTRACT_CAP",
                          f"{rec.target_size_contracts} > {cfg.max_size_contracts} contracts")
    return GateResult(True, "SIZE_OK")


def gate_daily_drawdown(ctx: PortfolioContext, cfg: RiskConfig, trade_loss: int) -> GateResult:
    """Adding this trade's worst-case loss must not breach daily drawdown budget."""
    projected = abs(min(0, ctx.daily_pnl_cents)) + trade_loss
    if projected > cfg.max_drawdown_cents:
        return GateResult(False, "DAILY_DRAWDOWN_BREACH",
                          f"Projected daily drawdown {projected}¢ > budget {cfg.max_drawdown_cents}¢")
    return GateResult(True, "DRAWDOWN_OK")


def gate_max_deployed(ctx: PortfolioContext, cfg: RiskConfig, new_exposure: int) -> GateResult:
    """Total deployed capital (including this trade) must not exceed hard ceiling."""
    projected = ctx.current_deployed_cents + new_exposure
    if projected > cfg.max_total_deployed_cents:
        return GateResult(False, "MAX_DEPLOYED_BREACH",
                          f"Would deploy {projected}¢ > ceiling {cfg.max_total_deployed_cents}¢")
    return GateResult(True, "DEPLOYED_OK")


def gate_open_exposure(ctx: PortfolioContext, cfg: RiskConfig, new_exposure: int) -> GateResult:
    projected = ctx.open_exposure_cents + new_exposure
    if projected > cfg.max_open_exposure_cents:
        return GateResult(False, "OPEN_EXPOSURE_BREACH",
                          f"Open exposure {projected}¢ > max {cfg.max_open_exposure_cents}¢")
    return GateResult(True, "EXPOSURE_OK")


def gate_daily_trade_count(ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    if ctx.today_new_trades >= cfg.max_new_trades_per_day:
        return GateResult(False, "DAILY_TRADE_CAP",
                          f"{ctx.today_new_trades} trades already today (max {cfg.max_new_trades_per_day})")
    return GateResult(True, "TRADE_COUNT_OK")


def gate_event_concentration(rec: Recommendation, ctx: PortfolioContext, cfg: RiskConfig, new_exposure: int) -> GateResult:
    current = ctx.positions_by_event.get(rec.event_ticker, 0)
    projected_pct = (current + new_exposure) / max(cfg.bankroll_cents, 1)
    if projected_pct > cfg.max_pct_per_event:
        return GateResult(False, "EVENT_CONCENTRATION",
                          f"Event {rec.event_ticker}: {projected_pct:.1%} > {cfg.max_pct_per_event:.1%}")
    return GateResult(True, "EVENT_CONCENTRATION_OK")


def gate_correlated_positions(ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    if ctx.correlated_count >= cfg.max_correlated_positions:
        return GateResult(False, "CORRELATED_POSITION_CAP",
                          f"{ctx.correlated_count} correlated positions ≥ limit {cfg.max_correlated_positions}")
    return GateResult(True, "CORRELATION_OK")


def gate_price_staleness(rec: Recommendation, orderbook: Orderbook, cfg: RiskConfig) -> GateResult:
    """Declared limit price must be within cfg.max_price_staleness_bps of the current mid.

    For YES buys, compare against the YES mid price.
    For NO buys, compare against the NO mid price (100 - YES mid).
    """
    mid_yes = orderbook.mid_price()
    if mid_yes is None:
        return GateResult(False, "NO_ORDERBOOK_MID", "Cannot determine current mid price")

    # Compare against the correct side's reference price
    if rec.side == Side.YES:
        reference = mid_yes
        side_label = "YES"
    else:
        reference = 100.0 - mid_yes  # NO mid price
        side_label = "NO"

    drift_bps = abs(rec.limit_price_cents - reference) / max(reference, 1) * 10000
    if drift_bps > cfg.max_price_staleness_bps:
        return GateResult(False, "PRICE_STALE",
                          f"Limit {rec.limit_price_cents}¢ vs {side_label} mid {reference:.1f}¢ = {drift_bps:.0f}bps drift (limit: {cfg.max_price_staleness_bps}bps)")
    return GateResult(True, "PRICE_OK")


def gate_liquidity(rec: Recommendation, market_data: dict, cfg: RiskConfig) -> GateResult:
    oi = market_data.get("open_interest", 0)
    vol = market_data.get("volume", 0)
    if oi < cfg.min_open_interest:
        return GateResult(False, "LOW_OPEN_INTEREST",
                          f"OI {oi} < floor {cfg.min_open_interest}")
    if vol < cfg.min_daily_volume:
        return GateResult(False, "LOW_VOLUME",
                          f"Volume {vol} < floor {cfg.min_daily_volume}")
    return GateResult(True, "LIQUIDITY_OK")


def gate_source_quality(rec: Recommendation) -> GateResult:
    tiers = {s.trust_tier for s in rec.sources}
    if TrustTier.PRIMARY not in tiers:
        return GateResult(False, "NO_TIER1_SOURCE", "No primary (tier-1) source cited")
    if tiers == {TrustTier.SOCIAL}:
        return GateResult(False, "TIER3_ONLY", "Sources are only tier-3 (social)")
    return GateResult(True, "SOURCE_QUALITY_OK")


def gate_brier_score(rec: Recommendation, ctx: PortfolioContext, cfg: RiskConfig) -> GateResult:
    if cfg.max_brier_score is None:
        return GateResult(True, "BRIER_GATE_DISABLED")
    # Extract category from market ticker (first segment by convention)
    category = rec.market_ticker.split("-")[0].upper()
    count = ctx.research_trade_count_by_category.get(category, 0)
    if count < cfg.min_trades_for_brier_gate:
        return GateResult(True, "BRIER_INSUFFICIENT_HISTORY")
    brier = ctx.research_brier_by_category.get(category, 0.0)
    if brier > cfg.max_brier_score:
        return GateResult(False, "BRIER_THRESHOLD_BREACH",
                          f"Brier {brier:.3f} > threshold {cfg.max_brier_score:.3f} for {category}")
    return GateResult(True, "BRIER_OK")


# ---------------------------------------------------------------------------
# Soft checks (modify, don't deny)
# ---------------------------------------------------------------------------


def soft_check_volatility(rec: Recommendation, cfg: RiskConfig) -> dict[str, Any]:
    """Cap size down if confidence is LOW."""
    if rec.confidence == Confidence.LOW:
        reduced = max(1, rec.target_size_contracts // 2)
        return {"target_size_contracts": reduced, "_note": "Halved size due to low confidence"}
    return {}


def soft_check_spread(rec: Recommendation, orderbook: Orderbook, cfg: RiskConfig) -> dict[str, Any]:
    """Tighten limit price if spread is wide."""
    spread = orderbook.spread_bps()
    if spread is not None and spread > cfg.max_spread_bps:
        mid = orderbook.mid_price()
        if mid is not None:
            # Tighten to 2bps inside mid
            tighter = int(mid - 0.02 * mid) if rec.side == Side.YES else int(mid + 0.02 * mid)
            tighter = max(1, min(99, tighter))
            return {"limit_price_cents": tighter, "_note": f"Tightened price: spread {spread}bps"}
    return {}


def soft_check_category_concentration(
    rec: Recommendation, ctx: PortfolioContext, cfg: RiskConfig, new_exposure: int
) -> dict[str, Any]:
    category = rec.market_ticker.split("-")[0].upper()
    current = ctx.positions_by_category.get(category, 0)
    projected_pct = (current + new_exposure) / max(cfg.bankroll_cents, 1)
    if projected_pct > cfg.max_pct_per_category * 0.8:  # within 80% of soft limit
        reduced = max(1, rec.target_size_contracts * 3 // 4)
        return {"target_size_contracts": reduced, "_note": f"Reduced size: category concentration at {projected_pct:.1%}"}
    return {}


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------


def evaluate(
    rec: Recommendation,
    cfg: RiskConfig,
    ctx: PortfolioContext,
    orderbook: Orderbook,
    market_data: dict,
) -> Decision:
    """Run all gates and soft checks; return a Decision artifact."""
    reason_codes: list[str] = []
    deny_reasons: list[str] = []
    modifications: dict[str, Any] = {}

    # --- Hard gates ---
    exposure_cents = rec.target_size_contracts * rec.limit_price_cents
    worst_loss = worst_case_loss_cents(rec.target_size_contracts, rec.limit_price_cents)

    gates = [
        gate_position_size(rec, cfg, orderbook),
        gate_daily_drawdown(ctx, cfg, worst_loss),
        gate_max_deployed(ctx, cfg, exposure_cents),
        gate_open_exposure(ctx, cfg, exposure_cents),
        gate_daily_trade_count(ctx, cfg),
        gate_event_concentration(rec, ctx, cfg, exposure_cents),
        gate_correlated_positions(ctx, cfg),
        gate_price_staleness(rec, orderbook, cfg),
        gate_liquidity(rec, market_data, cfg),
        gate_source_quality(rec),
        gate_brier_score(rec, ctx, cfg),
    ]

    for g in gates:
        reason_codes.append(g.code)
        if not g.passed:
            deny_reasons.append(f"{g.code}: {g.detail}")

    if deny_reasons:
        return Decision(
            rec_id=rec.rec_id,
            decision=DecisionVerdict.DENY,
            reason_codes=reason_codes,
            modifications={},
            approved_order=None,
            qa_notes="; ".join(deny_reasons),
        )

    # --- Soft checks (may modify working copy of rec fields) ---
    working_size = rec.target_size_contracts
    working_price = rec.limit_price_cents

    for mod in [
        soft_check_volatility(rec, cfg),
        soft_check_spread(rec, orderbook, cfg),
        soft_check_category_concentration(rec, ctx, cfg, exposure_cents),
    ]:
        if "target_size_contracts" in mod:
            working_size = mod["target_size_contracts"]
            modifications["target_size_contracts"] = working_size
        if "limit_price_cents" in mod:
            working_price = mod["limit_price_cents"]
            modifications["limit_price_cents"] = working_price

    # Apply ¼-Kelly sizing as a final sanity check — cap down to Kelly if research over-sized
    kelly_size = kelly_contracts(
        rec.estimated_fair_prob,
        working_price,
        cfg.bankroll_cents,
        cfg.max_position_pct,
        cfg.max_size_contracts,
    )
    if kelly_size < working_size:
        working_size = kelly_size
        modifications["target_size_contracts"] = working_size
        modifications["_kelly_capped"] = True

    # If sizing landed at 0 contracts, deny — can't submit a 0-contract order
    if working_size == 0:
        return Decision(
            rec_id=rec.rec_id,
            decision=DecisionVerdict.DENY,
            reason_codes=reason_codes + ["KELLY_ZERO"],
            modifications={},
            approved_order=None,
            qa_notes=(
                "KELLY_ZERO: Quarter-Kelly sizing produced 0 contracts — "
                "edge is too small relative to bankroll/price to justify a trade"
            ),
        )

    verdict = DecisionVerdict.MODIFY if modifications else DecisionVerdict.APPROVE

    # Build the approved order
    from datetime import timedelta
    expiry = datetime.now(timezone.utc) + timedelta(hours=4)  # default 4-hour GTD
    approved_order = ApprovedOrder(
        market_ticker=rec.market_ticker,
        side=rec.side,
        order_type=rec.order_type,
        limit_price_cents=working_price,
        size_contracts=working_size,
        expiry_ts=expiry,
    )

    return Decision(
        rec_id=rec.rec_id,
        decision=verdict,
        reason_codes=reason_codes,
        modifications=modifications,
        approved_order=approved_order,
        qa_notes="; ".join(f"{k}={v}" for k, v in modifications.items()),
    )
