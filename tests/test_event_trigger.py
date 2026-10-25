"""Tests for window-triggered research."""

from datetime import timedelta
from unittest import mock

import pytest
import yaml

from src import orchestrator as orch_mod
from src.common import config
from src.common.schemas import ResearchRun, utcnow
from src.jobs import Jobs
from src.kalshi_client.models import Market
from src.orchestrator import Orchestrator, ResearchWindow
from tests.fakes import FakeKalshi


def mk(ticker, hours):
    return Market(ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], title=ticker, volume=10,
                  close_time=utcnow() + timedelta(hours=hours))


def test_window_and_overrides():
    w = ResearchWindow((2, 12), {"KXFEDDECISION": [6, 72]})
    assert w.contains(mk("KXNBAGAME-A-X", 5))
    assert not w.contains(mk("KXNBAGAME-A-X", 20))
    assert w.contains(mk("KXFEDDECISION-26DEC-H0", 48))
    assert not w.contains(mk("KXFEDDECISION-26DEC-H0", 3))


@pytest.fixture
def env(tmp_path, ledger):
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump({
        "bankroll": {"total_capital_usd": 100, "max_total_deployed_usd": 90},
        "per_trade": {"max_loss_usd": 20, "max_position_pct_of_bankroll": 0.2, "max_size_contracts": 50},
        "per_day": {"max_drawdown_usd": 50, "max_new_trades": 5, "max_open_exposure_usd": 90},
        "concentration": {"max_pct_per_event": 0.5, "max_pct_per_category": 0.5, "max_correlated_positions": 3},
        "liquidity_floors": {"min_open_interest": 0, "min_daily_volume_contracts": 0},
    }), encoding="utf-8")
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(
        {"categories": {"include": ["KXNBAGAME"]}, "time_filters": {"min_hours_to_resolution": 1}}),
        encoding="utf-8")
    (tmp_path / "runtime.yaml").write_text("local_llm:\n  enabled: false\n", encoding="utf-8")
    k = FakeKalshi()
    k.markets = {m.ticker: m for m in [mk("KXNBAGAME-A-X", 5), mk("KXNBAGAME-B-Y", 30)]}
    with mock.patch.object(config, "CONFIG_DIR", tmp_path), \
         mock.patch.object(orch_mod, "KalshiClient", lambda: k):
        yield ledger, tmp_path


async def test_count_window_markets(env):
    ledger, _ = env
    n = await Orchestrator(ledger).count_window_markets(ResearchWindow((2, 12)))
    assert n == 1
    assert ledger.list_runs() == []        # a probe leaves no run record


async def test_watch_triggers_once_then_waits(env):
    ledger, _ = env
    jobs = Jobs(ledger, Orchestrator(ledger))
    calls = []

    async def fake_research(trigger="schedule", window=None):
        calls.append((trigger, window.describe()))
    jobs.research = fake_research
    assert await jobs.watch() == 1
    assert calls == [("event", "2–12h")]
    assert await jobs.watch() is None       # inside min_gap_minutes
    assert len(calls) == 1


async def test_watch_respects_budget(env):
    ledger, tmp = env
    (tmp / "runtime.yaml").write_text("local_llm:\n  enabled: false\nbudget:\n  monthly_usd: 0.01\n",
                                      encoding="utf-8")
    ledger.save_run(ResearchRun(cost_usd=0.02))
    jobs = Jobs(ledger, Orchestrator(ledger))
    assert await jobs.watch() is None
