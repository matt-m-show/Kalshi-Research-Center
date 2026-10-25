"""Smoke tests for web endpoints that don't call Kalshi."""

from unittest import mock

import pytest
import yaml
from fastapi.testclient import TestClient

from src.common import config
from src.common.schemas import Order, OrderStatus, Recommendation, ResearchRun, Side
from src.web import app as web


@pytest.fixture
def client(ledger, tmp_path):
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump(
        {"per_trade": {"min_edge_cents": 3}, "concentration": {"max_pct_per_event": 0.25}}),
        encoding="utf-8")
    async def offline():
        return False
    with mock.patch.object(web.state, "ledger", ledger), \
         mock.patch.object(config, "CONFIG_DIR", tmp_path), \
         mock.patch.object(web, "_check_kalshi", offline), \
         mock.patch.object(web.auth, "_SECRET_PATH", tmp_path / ".session_secret"), \
         TestClient(web.app, headers={"X-Requested-With": "kalshi-dashboard"}) as c:
        web.state.jobs.ledger = ledger
        yield c


def test_settings_merge_and_validation(client, tmp_path):
    r = client.post("/api/settings", json={"risk": {"per_trade": {"min_edge_cents": 4}}}).json()
    assert r["success"]
    saved = yaml.safe_load((tmp_path / "risk.yaml").read_text(encoding="utf-8"))
    assert saved["per_trade"]["min_edge_cents"] == 4
    assert saved["concentration"]["max_pct_per_event"] == 0.25

    bad = client.post("/api/settings", json={"risk": {"concentration": {"max_pct_per_event": 50}}}).json()
    assert not bad["success"] and "fraction" in bad["errors"][0]


def test_runs_and_recommendations(client, ledger):
    ledger.save_run(ResearchRun(status="ok", candidates=[{"ticker": "T", "stage": "no_edge"}]))
    ledger.save_recommendation(Recommendation(market_ticker="T", event_ticker="E", side=Side.YES,
                                              limit_price_cents=40, target_size_contracts=0,
                                              estimated_fair_prob=0.6, edge_cents_after_fees=5))
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["candidate_count"] == 1 and "candidates" not in runs[0]
    run = client.get(f"/api/runs/{runs[0]['run_id']}").json()
    assert run["run"]["candidates"][0]["stage"] == "no_edge"
    assert client.get("/api/recommendations").json()["recommendations"][0]["edge_cents"] == 5


def test_pending_and_deny(client, ledger):
    ledger.save_order(Order(client_order_id="c", rec_id="r1", market_ticker="T", side=Side.NO,
                            limit_price_cents=30, size_contracts=2, status=OrderStatus.PENDING_APPROVAL))
    assert client.get("/api/pending").json()["pending"][0]["side"] == "NO"
    assert client.post("/api/deny/r1").json()["success"]
    assert client.get("/api/pending").json()["pending"] == []


def test_settings_history_and_revert(client, tmp_path):
    client.post("/api/settings", json={"risk": {"per_trade": {"min_edge_cents": 6}}})
    hist = client.get("/api/settings/history").json()["history"]
    change = hist[0]["payload"]["changes"]["per_trade.min_edge_cents"]
    assert change == {"before": 3, "after": 6}
    assert client.post(f"/api/settings/revert/{hist[0]['seq']}").json()["success"]
    saved = yaml.safe_load((tmp_path / "risk.yaml").read_text(encoding="utf-8"))
    assert saved["per_trade"]["min_edge_cents"] == 3


def test_approve_rejects_loosening(client, ledger):
    ledger.save_order(Order(client_order_id="c", rec_id="r2", market_ticker="T", side=Side.YES,
                            limit_price_cents=40, max_price_cents=45, size_contracts=3,
                            status=OrderStatus.PENDING_APPROVAL))
    assert "size" in client.post("/api/approve/r2", json={"size": 4}).json()["error"]
    assert "max_price" in client.post("/api/approve/r2", json={"max_price": 50}).json()["error"]


def test_csrf_header_required(client):
    r = client.post("/api/mode/hitl", headers={"X-Requested-With": ""})
    assert r.status_code == 403


def test_remote_requests_need_login(client):
    r = client.get("/api/status", headers={"X-Forwarded-For": "100.64.0.9"})
    assert r.status_code == 401 and r.json()["error"] == "remote_access_disabled"
    with mock.patch.dict("os.environ", {"DASHBOARD_PASSWORD": "hunter22"}):
        remote = {"X-Forwarded-For": "100.64.0.9"}
        assert client.get("/api/status", headers=remote).status_code == 401
        assert client.post("/api/login", json={"password": "nope"}, headers=remote).status_code == 401
        assert client.post("/api/login", json={"password": "hunter22"}, headers=remote).status_code == 200
        assert client.get("/api/health", headers=remote).status_code == 200
        assert client.get("/api/status", headers=remote).status_code == 200   # cookie now set


def test_security_headers(client):
    h = client.get("/api/health").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"


def test_performance_and_mode(client):
    perf = client.get("/api/performance").json()
    assert "api_spend_month_usd" in perf and "reliability" in perf
    assert client.post("/api/mode/hitl").json()["mode"] == "hitl"
    assert not client.post("/api/mode/bogus").json()["success"]
