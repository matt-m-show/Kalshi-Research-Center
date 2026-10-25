"""Performance metrics: P&L, Brier score, Sharpe/Sortino, drawdown.

All functions are pure (no I/O) so they're trivially unit-testable.
Input is a list of ClosedTrade records built from the ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass
class ClosedTrade:
    rec_id: str
    market_ticker: str
    category: str                  # first segment of ticker, e.g. "KXPRES"
    side: str                      # "YES" | "NO"
    avg_fill_price_cents: float    # price paid for the side held
    size_contracts: int
    resolved_price_cents: float    # YES settlement value: 0 or 100 for binary markets
    estimated_fair_prob: float     # fair YES probability at recommendation time
    confidence: str
    closed_at: datetime
    pnl_override_cents: float | None = None   # Kalshi's settlement P&L (fees, early exits)


def realized_pnl_cents(trade: ClosedTrade) -> float:
    """Net P&L for one closed trade, in cents."""
    if trade.pnl_override_cents is not None:
        return trade.pnl_override_cents
    payout = trade.resolved_price_cents if trade.side == "YES" else 100 - trade.resolved_price_cents
    return trade.size_contracts * (payout - trade.avg_fill_price_cents)


def brier_score(trades: Sequence[ClosedTrade]) -> float | None:
    """Mean Brier score across closed trades. Lower is better (0 = perfect).

    Brier = mean((p_estimate - outcome)^2)
    outcome = 1 if YES resolved, 0 if NO resolved
    """
    if not trades:
        return None
    squared_errors = []
    for t in trades:
        outcome = 1.0 if t.resolved_price_cents >= 50 else 0.0
        squared_errors.append((t.estimated_fair_prob - outcome) ** 2)
    return sum(squared_errors) / len(squared_errors)


def log_loss(trades: Sequence[ClosedTrade], eps: float = 1e-7) -> float | None:
    """Mean log loss across closed trades. Lower is better."""
    if not trades:
        return None
    total = 0.0
    for t in trades:
        outcome = 1.0 if t.resolved_price_cents >= 50 else 0.0
        p = max(eps, min(1 - eps, t.estimated_fair_prob))
        total += -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))
    return total / len(trades)


def edge_capture(trades: Sequence[ClosedTrade]) -> float | None:
    """Realized P&L ÷ predicted edge, aggregated. Values near 1.0 are good."""
    if not trades:
        return None
    realized = sum(realized_pnl_cents(t) for t in trades)
    # Predicted edge in cents = sum(size * abs(estimated_fair_prob - fill_prob) * 100)
    predicted = sum(
        t.size_contracts * abs(t.estimated_fair_prob - t.avg_fill_price_cents / 100) * 100
        for t in trades
    )
    return realized / predicted if predicted != 0 else None


def hit_rate_by_confidence(trades: Sequence[ClosedTrade]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for t in trades:
        win = 1.0 if realized_pnl_cents(t) > 0 else 0.0
        buckets.setdefault(t.confidence, []).append(win)
    return {k: sum(v) / len(v) for k, v in buckets.items() if v}


def max_drawdown(pnl_series: Sequence[float]) -> float:
    """Peak-to-trough drawdown on a cumulative P&L series."""
    if not pnl_series:
        return 0.0
    peak = pnl_series[0]
    max_dd = 0.0
    running = 0.0
    for delta in pnl_series:
        running += delta
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


def sharpe_ratio(returns: Sequence[float], risk_free: float = 0.0) -> float | None:
    """Annualized Sharpe ratio on a sequence of daily returns (decimal)."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return None
    return (mean - risk_free) / std * math.sqrt(252)


def sortino_ratio(returns: Sequence[float], risk_free: float = 0.0) -> float | None:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < risk_free]
    if not downside:
        return None
    downside_var = sum((r - risk_free) ** 2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0:
        return None
    return (mean - risk_free) / downside_std * math.sqrt(252)


@dataclass
class RollingMetrics:
    """Snapshot of all rolling performance metrics."""
    as_of: datetime
    total_trades: int
    total_pnl_cents: float
    unrealized_pnl_cents: float
    brier_score: float | None
    log_loss: float | None
    edge_capture: float | None
    hit_rate_by_confidence: dict[str, float]
    max_drawdown_cents: float
    sharpe_30d: float | None
    sortino_30d: float | None
    pnl_by_category: dict[str, float]
    open_positions_count: int
    consecutive_losses: int
    assessment_brier: float | None = None     # over every resolved estimate, traded or not
    assessments_resolved: int = 0


def compute_rolling_metrics(
    closed_trades: Sequence[ClosedTrade],
    unrealized_cents: float = 0.0,
    open_positions_count: int = 0,
) -> RollingMetrics:
    """Compute all metrics from a list of closed trades."""
    pnl_list = [realized_pnl_cents(t) for t in closed_trades]
    total_pnl = sum(pnl_list)

    # Category breakdown
    cat_pnl: dict[str, float] = {}
    for t, p in zip(closed_trades, pnl_list):
        cat_pnl[t.category] = cat_pnl.get(t.category, 0.0) + p

    # Consecutive losses (from most recent)
    consec = 0
    for p in reversed(pnl_list):
        if p < 0:
            consec += 1
        else:
            break

    # Daily returns for Sharpe (30d window)
    from collections import defaultdict
    daily: dict[str, float] = defaultdict(float)
    for t, p in zip(closed_trades, pnl_list):
        day_key = t.closed_at.strftime("%Y-%m-%d")
        daily[day_key] += p
    daily_returns_30d = list(daily.values())[-30:]

    return RollingMetrics(
        as_of=datetime.utcnow(),
        total_trades=len(closed_trades),
        total_pnl_cents=total_pnl,
        unrealized_pnl_cents=unrealized_cents,
        brier_score=brier_score(closed_trades),
        log_loss=log_loss(closed_trades),
        edge_capture=edge_capture(closed_trades),
        hit_rate_by_confidence=hit_rate_by_confidence(closed_trades),
        max_drawdown_cents=max_drawdown(pnl_list),
        sharpe_30d=sharpe_ratio(daily_returns_30d),
        sortino_30d=sortino_ratio(daily_returns_30d),
        pnl_by_category=cat_pnl,
        open_positions_count=open_positions_count,
        consecutive_losses=consec,
    )
