"""Turns ledger rows into performance inputs: closed trades and calibration.

Calibration is scored on every resolved Assessment (traded or not); P&L comes
from Kalshi's own settlement records, so it already includes fees and any
contracts sold before resolution.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from src.common.ledger import Ledger
from src.common.schemas import Assessment, Side
from src.performance.metrics import ClosedTrade, RollingMetrics, compute_rolling_metrics
from src.qa.rules import category_of


def calibration_by_category(assessments: Iterable[Assessment]) -> dict[str, dict]:
    """Brier score and count of resolved estimates, per series."""
    sums: dict[str, list[float]] = defaultdict(list)
    for a in assessments:
        if a.resolved_yes is None or a.fair_yes_prob is None:
            continue
        outcome = 1.0 if a.resolved_yes else 0.0
        sums[category_of(a.market_ticker)].append((a.fair_yes_prob - outcome) ** 2)
    return {k: {"brier": sum(v) / len(v), "n": len(v)} for k, v in sums.items() if v}


def reliability_bins(assessments: Iterable[Assessment], bins: int = 10) -> list[dict]:
    """Predicted vs observed YES frequency, for a calibration chart."""
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for a in assessments:
        if a.resolved_yes is None or a.fair_yes_prob is None:
            continue
        idx = min(bins - 1, int(a.fair_yes_prob * bins))
        buckets[idx].append((a.fair_yes_prob, 1.0 if a.resolved_yes else 0.0))
    out = []
    for i, b in enumerate(buckets):
        if b:
            out.append({
                "bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}",
                "predicted": sum(p for p, _ in b) / len(b),
                "observed": sum(o for _, o in b) / len(b),
                "n": len(b),
            })
    return out


def closed_trades(ledger: Ledger) -> list[ClosedTrade]:
    """One ClosedTrade per settled market we held, using Kalshi's settlement P&L."""
    trades: list[ClosedTrade] = []
    for st in ledger.list_settlements():
        entries = [f for f in ledger.get_fills_for_ticker(st.market_ticker) if f.action == "buy"]
        if not entries:
            continue
        side = entries[0].side
        size = sum(f.count for f in entries)
        avg = sum(f.price_cents * f.count for f in entries) / max(size, 1)
        rec = ledger.get_recommendation(entries[0].rec_id)
        fair_yes = (rec.fair_yes_prob if rec and rec.fair_yes_prob is not None
                    else (rec.estimated_fair_prob if rec and side == Side.YES
                          else (1 - rec.estimated_fair_prob) if rec else 0.5))
        trades.append(ClosedTrade(
            rec_id=entries[0].rec_id,
            market_ticker=st.market_ticker,
            category=category_of(st.market_ticker),
            side=side.value,
            avg_fill_price_cents=avg,
            size_contracts=size,
            resolved_price_cents=100.0 if st.result == "yes" else 0.0,
            estimated_fair_prob=fair_yes,
            confidence=rec.confidence.value if rec else "medium",
            closed_at=st.settled_at or entries[-1].filled_at,
            pnl_override_cents=float(st.pnl_cents),
        ))
    return trades


def current_metrics(ledger: Ledger, unrealized_cents: float = 0.0, open_positions: int = 0) -> RollingMetrics:
    resolved = ledger.list_assessments(resolved_only=True, limit=10000)
    metrics = compute_rolling_metrics(closed_trades(ledger), unrealized_cents, open_positions)
    # Contracts sold before settlement are recorded as "exit" rows
    metrics.total_pnl_cents += sum(s.pnl_cents for s in ledger.list_settlements() if s.result == "exit")
    calib = [a for a in resolved if a.fair_yes_prob is not None]
    if calib:
        metrics.assessment_brier = sum(
            (a.fair_yes_prob - (1.0 if a.resolved_yes else 0.0)) ** 2 for a in calib
        ) / len(calib)
        metrics.assessments_resolved = len(calib)
    return metrics
