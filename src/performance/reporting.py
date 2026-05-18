"""Daily and weekly Markdown report generation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.performance.metrics import RollingMetrics

REPORTS_DIR = Path(__file__).parents[2] / "reports"
METRICS_DIR = Path(__file__).parents[2] / "metrics"


def _fmt_cents(c: float) -> str:
    sign = "+" if c >= 0 else "-"
    return f"{sign}${abs(c)/100:.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2%}"


def generate_daily_report(metrics: RollingMetrics, bankroll_cents: int, date: datetime | None = None) -> Path:
    """Write a daily Markdown report. Returns the file path."""
    date = date or datetime.utcnow()
    date_str = date.strftime("%Y-%m-%d")
    path = REPORTS_DIR / "daily" / f"{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    brier = f"{metrics.brier_score:.4f}" if metrics.brier_score is not None else "n/a"
    log_loss = f"{metrics.log_loss:.4f}" if metrics.log_loss is not None else "n/a"
    edge_cap = f"{metrics.edge_capture:.2%}" if metrics.edge_capture is not None else "n/a"
    dd_pct = f"{metrics.max_drawdown_cents / max(bankroll_cents, 1):.2%}"

    cat_rows = "\n".join(
        f"| {cat} | {_fmt_cents(pnl)} |"
        for cat, pnl in sorted(metrics.pnl_by_category.items(), key=lambda x: -abs(x[1]))
    ) or "| — | — |"

    conf_rows = "\n".join(
        f"| {conf} | {rate:.1%} |"
        for conf, rate in metrics.hit_rate_by_confidence.items()
    ) or "| — | — |"

    content = f"""# Kalshi Agent Daily Report — {date_str}

_Generated at {metrics.as_of.strftime('%Y-%m-%dT%H:%M:%SZ')}_

## P&L Summary

| Metric | Value |
|--------|-------|
| Total trades (closed) | {metrics.total_trades} |
| Realized P&L | {_fmt_cents(metrics.total_pnl_cents)} |
| Unrealized P&L | {_fmt_cents(metrics.unrealized_pnl_cents)} |
| Open positions | {metrics.open_positions_count} |
| Consecutive losses | {metrics.consecutive_losses} |

## Calibration

| Metric | Value |
|--------|-------|
| Brier score (lower = better) | {brier} |
| Log loss | {log_loss} |
| Edge capture | {edge_cap} |

## Risk

| Metric | Value |
|--------|-------|
| Max drawdown | {_fmt_cents(metrics.max_drawdown_cents)} ({dd_pct} of bankroll) |
| Sharpe (30d) | {_fmt_pct(metrics.sharpe_30d)} |
| Sortino (30d) | {_fmt_pct(metrics.sortino_30d)} |

## P&L by Category

| Category | P&L |
|----------|-----|
{cat_rows}

## Hit Rate by Confidence

| Confidence | Hit Rate |
|------------|----------|
{conf_rows}
"""

    path.write_text(content)
    return path


def save_rolling_metrics(metrics: RollingMetrics) -> None:
    """Persist rolling metrics JSON for the QA Agent to read."""
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "as_of": metrics.as_of.isoformat() + "Z",
        "total_trades": metrics.total_trades,
        "total_pnl_cents": metrics.total_pnl_cents,
        "brier_score": metrics.brier_score,
        "log_loss": metrics.log_loss,
        "edge_capture": metrics.edge_capture,
        "hit_rate_by_confidence": metrics.hit_rate_by_confidence,
        "max_drawdown_cents": metrics.max_drawdown_cents,
        "sharpe_30d": metrics.sharpe_30d,
        "sortino_30d": metrics.sortino_30d,
        "pnl_by_category": metrics.pnl_by_category,
        "consecutive_losses": metrics.consecutive_losses,
    }
    (METRICS_DIR / "rolling.json").write_text(json.dumps(data, indent=2, default=str))
