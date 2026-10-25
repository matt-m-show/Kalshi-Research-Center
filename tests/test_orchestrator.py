"""End-to-end pipeline test: fake Kalshi + stubbed research model."""

from datetime import timedelta
from unittest import mock

import pytest
import yaml

from src import orchestrator as orch_mod
from src.common import config
from src.common.schemas import (
    Assessment,
    OperationalMode,
    OrderStatus,
    SystemState,
    utcnow,
)
from src.kalshi_client.models import Market
from tests.fakes import FakeKalshi, book

EDGE = "KXNBA-26OCT26LALBOS-LAL"      # fair 0.60 vs book 40/45 → YES edge
FAIR = "KXNBA-26OCT26NYKMIA-NYK"      # fair 0.425 = mid of 40/45 → no edge on either side


@pytest.fixture
def env(tmp_path, ledger):
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump({
        "bankroll": {"total_capital_usd": 100, "max_total_deployed_usd": 90},
        "per_trade": {"max_loss_usd": 20, "max_position_pct_of_bankroll": 0.2,
                      "max_size_contracts": 50, "min_edge_cents": 3},
        "per_day": {"max_drawdown_usd": 50, "max_new_trades": 5, "max_open_exposure_usd": 90},
        "concentration": {"max_pct_per_event": 0.5, "max_pct_per_category": 0.5,
                          "max_correlated_positions": 3},
        "liquidity_floors": {"min_open_interest": 0, "min_daily_volume_contracts": 0,
                             "max_spread_cents": 15},
    }), encoding="utf-8")
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(
        {"categories": {"include": ["KXNBA"]}, "time_filters": {"min_hours_to_resolution": 1}}),
        encoding="utf-8")
    (tmp_path / "runtime.yaml").write_text("research:\n  max_candidates: 5\nlocal_llm:\n  enabled: false\n", encoding="utf-8")

    k = FakeKalshi()
    close = utcnow() + timedelta(hours=8)
    for t in (EDGE, FAIR):
        k.markets[t] = Market(ticker=t, event_ticker=t.rsplit("-", 1)[0], title=t, volume=500,
                              close_time=close)
        k.books[t] = book(t, 40, 45)

    async def fake_research(self, events, run_id, show_prices=False):
        self.meter.by_model = {}
        return [
            Assessment(run_id=run_id, market_ticker=EDGE, event_ticker=EDGE.rsplit("-", 1)[0],
                       fair_yes_prob=0.60, thesis="Edge", sources=[
                           {"title": "AP", "url": "https://apnews.com", "trust_tier": 1}]),
            Assessment(run_id=run_id, market_ticker=FAIR, fair_yes_prob=0.425, thesis="Fair"),
        ], {}

    with mock.patch.object(config, "CONFIG_DIR", tmp_path), \
         mock.patch.object(orch_mod, "KalshiClient", lambda: k), \
         mock.patch.object(orch_mod.ResearchAgent, "run", fake_research):
        yield ledger, k


def set_mode(ledger, mode):
    ledger.set_system_state(SystemState(mode=mode))


async def test_autonomous_submits_only_the_edge(env):
    ledger, k = env
    set_mode(ledger, OperationalMode.AUTONOMOUS)
    events = []
    run = await orch_mod.Orchestrator(ledger, on_event=lambda t, d: events.append(t)).run_research_cycle()
    assert run.status == "ok", run.error
    assert (run.assessments, run.recommendations, run.approved, run.submitted) == (2, 1, 1, 1)
    stages = {c["ticker"]: c["stage"] for c in run.candidates}
    assert stages == {EDGE: "submitted", FAIR: "no_edge"}
    req = k.created[0]
    assert req.ticker == EDGE and req.side == "bid" and req.post_only
    assert "run_finished" in events


async def test_hitl_queues_for_approval(env):
    ledger, k = env
    set_mode(ledger, OperationalMode.HITL)
    with mock.patch.object(orch_mod, "notify_pending_approval"):
        run = await orch_mod.Orchestrator(ledger).run_research_cycle()
    pending = ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
    assert len(pending) == 1 and pending[0].market_ticker == EDGE
    assert pending[0].max_price_cents == 57 and not k.created
    assert {c["ticker"]: c["stage"] for c in run.candidates}[EDGE] == "awaiting_approval"


async def test_recently_assessed_markets_are_skipped(env):
    ledger, k = env
    set_mode(ledger, OperationalMode.PAPER)
    await orch_mod.Orchestrator(ledger).run_research_cycle()
    second = await orch_mod.Orchestrator(ledger).run_research_cycle()
    assert all(c["stage"] == "filtered" for c in second.candidates)


async def test_prepass_drops_dead_events_and_attaches_evidence(env, tmp_path):
    ledger, k = env
    set_mode(ledger, OperationalMode.PAPER)
    (tmp_path / "runtime.yaml").write_text(
        "research:\n  max_candidates: 5\nlocal_llm:\n  enabled: true\n  evidence_prepass: true\n",
        encoding="utf-8")
    from src.research.local_assist import Evidence

    class FakeAssist:
        client = type("C", (), {"calls": 0, "model": "fake"})()

        async def gather(self, ev, today):
            if ev.event_ticker == EDGE.rsplit("-", 1)[0]:
                return Evidence([("injury news", "https://espn.com")], 4, "fresh news", ["q"])
            return Evidence([], 1, "nothing", ["q"])

        def page_hook(self, focus):
            return None

    seen = {}

    async def capture(self, events, run_id, show_prices=False):
        seen["events"] = events
        return [], {}

    async def fake_assist(self, runtime, run):
        return FakeAssist()

    with mock.patch.object(orch_mod.Orchestrator, "_local_assist", fake_assist), \
         mock.patch.object(orch_mod.ResearchAgent, "run", capture):
        run = await orch_mod.Orchestrator(ledger).run_research_cycle()
    assert [ev.event_ticker for ev in seen["events"]] == [EDGE.rsplit("-", 1)[0]]
    assert "injury news" in seen["events"][0].evidence
    assert {c["ticker"]: c["stage"] for c in run.candidates}[FAIR] == "triaged_out"


async def test_monthly_budget_skips_run(env, tmp_path):
    ledger, k = env
    (tmp_path / "runtime.yaml").write_text("budget:\n  monthly_usd: 0.01\nlocal_llm:\n  enabled: false\n", encoding="utf-8")
    from src.common.schemas import ResearchRun
    ledger.save_run(ResearchRun(cost_usd=0.02))
    run = await orch_mod.Orchestrator(ledger).run_research_cycle()
    assert run.status == "skipped" and "budget" in run.notes[0]
