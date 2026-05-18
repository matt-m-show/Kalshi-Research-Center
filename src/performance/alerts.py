"""Alert triggers for the Performance Agent.

Thresholds come from config/alerts.yaml.
Alerts fire desktop notifications and write to the audit log.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.performance.metrics import RollingMetrics

log = get_logger("performance_agent")


@dataclasses.dataclass
class AlertConfig:
    brier_warn_threshold: float = 0.30
    drawdown_halt_pct: float = 0.20       # fraction of bankroll
    drawdown_warn_pct: float = 0.10
    category_concentration_warn_pct: float = 0.40
    consecutive_losses_warn: int = 4
    consecutive_losses_halt: int = 6
    api_error_rate_halt_per_min: int = 10
    graduation_min_trades: int = 100
    graduation_min_days: int = 30
    graduation_max_brier: float = 0.25
    graduation_min_return_pct: float = 0.05
    graduation_max_drawdown_pct: float = 0.15


@dataclasses.dataclass
class Alert:
    alert_type: str
    severity: str       # "info" | "warn" | "critical"
    message: str
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    fired_at: datetime = dataclasses.field(default_factory=datetime.utcnow)


def _notify(title: str, message: str, severity: str = "info") -> None:
    try:
        from plyer import notification
        notification.notify(title=f"Kalshi [{severity.upper()}] {title}", message=message, timeout=30)
    except Exception:
        pass
    level = log.warning if severity == "warn" else (log.critical if severity == "critical" else log.info)
    level(f"alert_{title.lower().replace(' ', '_')}", message=message)


def check_all(
    metrics: RollingMetrics,
    cfg: AlertConfig,
    bankroll_cents: int,
    ledger: Ledger,
    start_date: datetime | None = None,
) -> list[Alert]:
    """Evaluate all alert conditions; fire notifications and audit entries for any that trigger."""
    alerts: list[Alert] = []

    def fire(alert: Alert) -> None:
        alerts.append(alert)
        _notify(alert.alert_type, alert.message, alert.severity)
        ledger.append_audit(f"ALERT_{alert.alert_type}", "performance_agent",
                            {"severity": alert.severity, "message": alert.message, **alert.payload})

    # --- Brier score miscalibration ---
    if metrics.brier_score is not None and metrics.brier_score > cfg.brier_warn_threshold:
        fire(Alert("BRIER_WARN", "warn",
                   f"Brier score {metrics.brier_score:.3f} exceeds warning threshold {cfg.brier_warn_threshold:.3f}",
                   {"brier": metrics.brier_score}))

    # --- Drawdown ---
    dd_pct = metrics.max_drawdown_cents / max(bankroll_cents, 1)
    if dd_pct >= cfg.drawdown_halt_pct:
        fire(Alert("DRAWDOWN_HALT", "critical",
                   f"Max drawdown {dd_pct:.1%} reached halt threshold {cfg.drawdown_halt_pct:.1%}",
                   {"drawdown_pct": dd_pct}))
        ledger.halt(f"Drawdown {dd_pct:.1%} >= halt threshold", actor="performance_agent")
    elif dd_pct >= cfg.drawdown_warn_pct:
        fire(Alert("DRAWDOWN_WARN", "warn",
                   f"Max drawdown {dd_pct:.1%} approaching halt threshold",
                   {"drawdown_pct": dd_pct}))

    # --- Category concentration ---
    for cat, pnl in metrics.pnl_by_category.items():
        if metrics.total_pnl_cents != 0:
            cat_pct = abs(pnl) / abs(metrics.total_pnl_cents)
            if cat_pct > cfg.category_concentration_warn_pct:
                fire(Alert("CATEGORY_CONCENTRATION", "warn",
                           f"Category {cat} is {cat_pct:.1%} of total P&L contribution",
                           {"category": cat, "pct": cat_pct}))

    # --- Consecutive losses ---
    if metrics.consecutive_losses >= cfg.consecutive_losses_halt:
        fire(Alert("CONSECUTIVE_LOSSES_HALT", "critical",
                   f"{metrics.consecutive_losses} consecutive losing trades — halting",
                   {"count": metrics.consecutive_losses}))
        ledger.halt(f"{metrics.consecutive_losses} consecutive losses", actor="performance_agent")
    elif metrics.consecutive_losses >= cfg.consecutive_losses_warn:
        fire(Alert("CONSECUTIVE_LOSSES_WARN", "warn",
                   f"{metrics.consecutive_losses} consecutive losing trades",
                   {"count": metrics.consecutive_losses}))

    # --- Graduation readiness ---
    if start_date is not None:
        days_elapsed = (datetime.utcnow() - start_date).days
        return_pct = metrics.total_pnl_cents / max(bankroll_cents, 1)
        if (
            days_elapsed >= cfg.graduation_min_days
            and metrics.total_trades >= cfg.graduation_min_trades
            and (metrics.brier_score is None or metrics.brier_score <= cfg.graduation_max_brier)
            and return_pct >= cfg.graduation_min_return_pct
            and dd_pct <= cfg.graduation_max_drawdown_pct
        ):
            fire(Alert("GRADUATION_READY", "info",
                       f"Paper-mode performance meets promotion criteria after {days_elapsed} days / "
                       f"{metrics.total_trades} trades. Run /promote-to-live to review.",
                       {"days": days_elapsed, "trades": metrics.total_trades,
                        "return_pct": return_pct, "brier": metrics.brier_score}))

    return alerts
