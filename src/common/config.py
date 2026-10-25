"""Typed, validated access to config/*.yaml.

Every loader re-reads its file on each call so dashboard edits take effect on
the next cycle without a restart. Validation is strict: a value in the wrong
units raises ConfigError rather than being reinterpreted.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parents[2] / "config"

EDITABLE_SECTIONS = ("risk", "universe", "runtime")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def save_yaml(name: str, data: dict) -> None:
    header = f"# Updated via dashboard — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"
    (CONFIG_DIR / f"{name}.yaml").write_text(
        header + yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def deep_merge(base: dict, update: dict) -> dict:
    """Return ``base`` with ``update`` merged in; keys absent from ``update`` are kept."""
    out = copy.deepcopy(base)
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _get(cfg: dict, path: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# (path, lower bound, upper bound, unit hint)
_RISK_BOUNDS: list[tuple[str, float, float, str]] = [
    ("bankroll.total_capital_usd", 0, 1e7, "dollars"),
    ("bankroll.max_total_deployed_usd", 0, 1e7, "dollars"),
    ("per_trade.max_loss_usd", 0, 1e7, "dollars"),
    ("per_trade.max_position_pct_of_bankroll", 0, 1, "a fraction, e.g. 0.10 for 10%"),
    ("per_trade.max_size_contracts", 1, 1e6, "contracts"),
    ("per_trade.min_edge_cents", 0, 50, "cents of edge after fees"),
    ("per_day.max_drawdown_usd", 0, 1e7, "dollars"),
    ("per_day.max_new_trades", 0, 1e4, "trades"),
    ("per_day.max_open_exposure_usd", 0, 1e7, "dollars"),
    ("concentration.max_pct_per_event", 0, 1, "a fraction, e.g. 0.25 for 25%"),
    ("concentration.max_pct_per_category", 0, 1, "a fraction, e.g. 0.50 for 50%"),
    ("concentration.max_correlated_positions", 0, 1000, "positions"),
    ("liquidity_floors.min_open_interest", 0, 1e9, "contracts"),
    ("liquidity_floors.min_daily_volume_contracts", 0, 1e9, "contracts"),
    ("liquidity_floors.max_spread_cents", 1, 99, "cents"),
    ("kill_switches.portfolio_drawdown_halt_pct", 0, 1, "a fraction, e.g. 0.30 for 30%"),
    ("kill_switches.consecutive_losses_halt", 1, 1000, "trades"),
    ("execution.reprice_interval_minutes", 1, 1440, "minutes"),
    ("execution.max_reprices", 0, 100, "count"),
    ("execution.order_ttl_hours", 0.1, 720, "hours"),
    ("execution.cancel_before_close_minutes", 0, 1440, "minutes"),
    ("research_quality.max_brier_score_for_category", 0, 1, "a Brier score between 0 and 1"),
    ("exits.take_profit_capture", 0.05, 1, "a fraction of the entry→fair move, e.g. 0.8"),
    ("exits.stop_loss_pct", 0.05, 1, "a fraction of the entry price, e.g. 0.5"),
    ("exits.stop_loss_min_hours_to_close", 0, 720, "hours"),
    ("exits.max_slippage_cents", 0, 20, "cents below the bid"),
    ("exits.news_check_minutes", 0, 10080, "minutes (0 = off)"),
]


def validate_risk(cfg: dict) -> list[str]:
    errors: list[str] = []
    for path, lo, hi, hint in _RISK_BOUNDS:
        val = _get(cfg, path)
        if val is None:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            errors.append(f"{path} must be a number ({hint}), got {val!r}")
        elif not lo <= val <= hi:
            errors.append(f"{path} = {val} is out of range [{lo}, {hi}] — expected {hint}")
    style = _get(cfg, "execution.entry_style", "passive")
    if style not in ("passive", "take"):
        errors.append(f"execution.entry_style must be 'passive' or 'take', got {style!r}")
    action = _get(cfg, "exits.on_invalidation", "alert")
    if action not in ("alert", "exit"):
        errors.append(f"exits.on_invalidation must be 'alert' or 'exit', got {action!r}")
    return errors


def validate_universe(cfg: dict) -> list[str]:
    errors: list[str] = []
    inc = _get(cfg, "categories.include", [])
    if not isinstance(inc, list) or not all(isinstance(s, str) for s in inc):
        errors.append("categories.include must be a list of series tickers")
    for path in ("time_filters.min_hours_to_resolution", "time_filters.max_days_to_resolution"):
        val = _get(cfg, path)
        if val is not None and (not isinstance(val, (int, float)) or val < 0):
            errors.append(f"{path} must be a non-negative number")
    return errors


def validate_runtime(cfg: dict) -> list[str]:
    errors: list[str] = []
    for path, lo, hi in [
        ("research.interval_minutes", 5, 10080),
        ("research.max_candidates", 1, 200),
        ("research.max_recommendations_per_run", 1, 100),
        ("research.reassess_after_hours", 0, 720),
        ("research.max_tool_iterations", 1, 50),
        ("order_management.interval_minutes", 1, 1440),
        ("mark_to_market.interval_minutes", 1, 1440),
    ]:
        val = _get(cfg, path)
        if val is not None and (not isinstance(val, (int, float)) or not lo <= val <= hi):
            errors.append(f"{path} = {val!r} must be between {lo} and {hi}")
    return errors


VALIDATORS = {"risk": validate_risk, "universe": validate_universe, "runtime": validate_runtime}


def update_section(section: str, changes: dict) -> dict:
    """Merge ``changes`` into a config file, validate, and save. Returns the new config.

    Raises ConfigError (and writes nothing) if the merged result is invalid.
    """
    if section not in EDITABLE_SECTIONS:
        raise ConfigError(f"Unknown config section: {section}")
    merged = deep_merge(load_yaml(section), changes)
    errors = VALIDATORS[section](merged)
    if errors:
        raise ConfigError("; ".join(errors))
    save_yaml(section, merged)
    return merged


# ---------------------------------------------------------------------------
# Typed views
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ExecutionConfig:
    entry_style: str = "passive"            # "passive" (post inside spread) | "take"
    min_edge_cents: float = 3.0             # required edge per contract after fees
    max_spread_cents: int | None = None     # skip markets with a wider spread
    reprice_interval_minutes: int = 10
    max_reprices: int = 6
    order_ttl_hours: float = 6.0
    cancel_before_close_minutes: int = 15


@dataclasses.dataclass
class ExitConfig:
    enabled: bool = True
    take_profit_capture: float = 0.8         # sell once the bid captures this share of entry→fair
    stop_loss_pct: float = 0.5               # sell if the bid falls this far below entry
    stop_loss_min_hours_to_close: float = 0.5  # don't stop out this close to resolution
    max_slippage_cents: int = 2              # accept up to this much below the bid when selling
    news_check_minutes: int = 120            # 0 disables the news watch
    on_invalidation: str = "alert"           # "alert" | "exit"


def load_exit_config(cfg: dict | None = None) -> ExitConfig:
    cfg = cfg if cfg is not None else load_yaml("risk")
    ex = cfg.get("exits") or {}
    d = ExitConfig()
    return ExitConfig(**{f.name: type(getattr(d, f.name))(ex.get(f.name, getattr(d, f.name)))
                         for f in dataclasses.fields(ExitConfig)})


def load_execution_config(cfg: dict | None = None) -> ExecutionConfig:
    cfg = cfg if cfg is not None else load_yaml("risk")
    ex = cfg.get("execution") or {}
    d = ExecutionConfig()
    return ExecutionConfig(
        entry_style=ex.get("entry_style", d.entry_style),
        min_edge_cents=float(_get(cfg, "per_trade.min_edge_cents", d.min_edge_cents)),
        max_spread_cents=_get(cfg, "liquidity_floors.max_spread_cents", d.max_spread_cents),
        reprice_interval_minutes=int(ex.get("reprice_interval_minutes", d.reprice_interval_minutes)),
        max_reprices=int(ex.get("max_reprices", d.max_reprices)),
        order_ttl_hours=float(ex.get("order_ttl_hours", d.order_ttl_hours)),
        cancel_before_close_minutes=int(
            ex.get("cancel_before_close_minutes", d.cancel_before_close_minutes)
        ),
    )


def load_risk_config():
    """Build the QA engine's RiskConfig from risk.yaml (raises ConfigError if invalid)."""
    from src.qa.rules import RiskConfig

    cfg = load_yaml("risk")
    errors = validate_risk(cfg)
    if errors:
        raise ConfigError("risk.yaml: " + "; ".join(errors))
    b, pt, pd = cfg["bankroll"], cfg["per_trade"], cfg["per_day"]
    conc, liq = cfg["concentration"], cfg["liquidity_floors"]
    rq = cfg.get("research_quality") or {}
    ks = cfg.get("kill_switches") or {}
    return RiskConfig(
        bankroll_cents=int(b["total_capital_usd"] * 100),
        max_total_deployed_cents=int(b["max_total_deployed_usd"] * 100),
        max_loss_cents=int(pt["max_loss_usd"] * 100),
        max_position_pct=pt["max_position_pct_of_bankroll"],
        max_size_contracts=pt["max_size_contracts"],
        max_drawdown_cents=int(pd["max_drawdown_usd"] * 100),
        max_new_trades_per_day=pd["max_new_trades"],
        max_open_exposure_cents=int(pd["max_open_exposure_usd"] * 100),
        max_pct_per_event=conc["max_pct_per_event"],
        max_pct_per_category=conc["max_pct_per_category"],
        max_correlated_positions=conc["max_correlated_positions"],
        min_open_interest=liq.get("min_open_interest", 0),
        min_daily_volume=liq.get("min_daily_volume_contracts", 0),
        min_edge_cents=float(pt.get("min_edge_cents", 3.0)),
        kelly_fraction=float((cfg.get("position_sizing") or {}).get("kelly_fraction", 0.25)),
        max_brier_score=rq.get("max_brier_score_for_category"),
        min_trades_for_brier_gate=rq.get("min_trades_before_brier_gate", 20),
        consecutive_losses_halt=ks.get("consecutive_losses_halt", 5),
        portfolio_drawdown_halt_pct=ks.get("portfolio_drawdown_halt_pct", 0.20),
    )


def load_universe_config() -> dict:
    cfg = load_yaml("universe")
    errors = validate_universe(cfg)
    if errors:
        raise ConfigError("universe.yaml: " + "; ".join(errors))
    cfg.setdefault("categories", {}).setdefault("include", [])
    cfg["categories"].setdefault("exclude", [])
    cfg.setdefault("time_filters", {}).setdefault("min_hours_to_resolution", 1)
    return cfg


_RUNTIME_DEFAULTS: dict[str, Any] = {
    "research": {
        "interval_minutes": 240,
        "max_candidates": 12,
        "max_recommendations_per_run": 5,
        "reassess_after_hours": 12,
        "max_tool_iterations": 12,
        "show_market_prices": False,
        "event_trigger": {
            "enabled": True,
            "check_every_minutes": 30,     # cheap check, no LLM
            "window_hours": [2, 12],       # research markets closing this far ahead
            "window_overrides": {},        # per series, e.g. {KXFEDDECISION: [6, 72]}
            "min_new_markets": 1,
            "min_gap_minutes": 60,         # between event-triggered runs
        },
    },
    "models": {"research": "claude-sonnet-5-5", "local": "qwen3.5:4b"},
    "local_llm": {
        "enabled": True,
        "host": None,                 # default: $OLLAMA_HOST or http://localhost:11434
        "evidence_prepass": True,     # local model searches + digests evidence per event
        "summarize_pages": True,      # local model condenses fetched pages for Claude
        "pool_multiplier": 2,         # shortlist this many × max_candidates for the pre-pass
        "timeout_seconds": 180,
        "num_ctx": 16384,
    },
    "budget": {"monthly_usd": 10, "per_run_usd": 0.40},
    "order_management": {"interval_minutes": 5},
    "mark_to_market": {"interval_minutes": 5},
    "reporting": {"daily_report_utc_hour": 23},
}


def load_runtime_config() -> dict:
    cfg = deep_merge(_RUNTIME_DEFAULTS, load_yaml("runtime"))
    errors = validate_runtime(cfg)
    if errors:
        raise ConfigError("runtime.yaml: " + "; ".join(errors))
    return cfg
