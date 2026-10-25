"""FastAPI backend for the Kalshi trading dashboard.

REST endpoints under /api, a WebSocket at /ws for live events, and the
single-page dashboard at /.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.common.config import ConfigError, EDITABLE_SECTIONS, load_yaml, update_section
from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.web import auth
from src.common.schemas import (
    ApprovedOrder,
    OperationalMode,
    OrderPurpose,
    OrderStatus,
    Side,
    new_id,
    utcnow,
)

log = get_logger("web")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------


class AppState:
    def __init__(self) -> None:
        self.ledger: Ledger = Ledger()
        self.jobs = None                  # src.jobs.Jobs
        self.scheduler = None             # AsyncIOScheduler | None
        self.agents_running = False
        self.agent_statuses: dict[str, str] = {
            "research": "stopped", "qa": "stopped", "execute": "stopped", "performance": "stopped",
        }
        self.kalshi_connected = False
        self.kalshi_checked_at = 0.0
        self.live_feed: deque = deque(maxlen=300)
        self.ws_clients: set[WebSocket] = set()
        self.series_cache: tuple[float, list] | None = None


state = AppState()


def push_event(event_type: str, data: dict) -> None:
    event: dict[str, Any] = {"type": event_type, "ts": utcnow().isoformat() + "Z", **data}
    state.live_feed.append(event)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_broadcast(event))
    except RuntimeError:
        pass


async def _broadcast(event: dict) -> None:
    msg = json.dumps(event, default=str)
    dead = set()
    for ws in list(state.ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead


def _on_job_status(job: str, status: str) -> None:
    keys = ("research", "qa") if job == "research" else ("execute", "performance")
    for k in keys:
        state.agent_statuses[k] = status
        push_event("agent_status", {"agent": k, "status": status})


async def _check_kalshi(max_age: float = 60.0) -> bool:
    if time.monotonic() - state.kalshi_checked_at < max_age:
        return state.kalshi_connected
    from src.kalshi_client.client import KalshiClient
    try:
        async with KalshiClient(timeout=5) as client:
            await client.get_balance()
        state.kalshi_connected = True
    except Exception:
        state.kalshi_connected = False
    state.kalshi_checked_at = time.monotonic()
    return state.kalshi_connected


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.jobs import Jobs
    from src.orchestrator import Orchestrator

    state.jobs = Jobs(state.ledger, Orchestrator(state.ledger, on_event=push_event), _on_job_status)
    await _check_kalshi()
    push_event("system", {"message": "Kalshi Trading System web server started"})
    log.info("web_server_started")
    yield
    if state.scheduler and state.scheduler.running:
        state.scheduler.shutdown(wait=False)
    log.info("web_server_stopped")


app = FastAPI(title="Kalshi Trading System", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(auth.AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    if request.headers.get(auth.CSRF_HEADER) != auth.CSRF_VALUE:
        return JSONResponse({"error": "csrf_check_failed"}, status_code=403)
    ip = request.client.host if request.client else "?"
    if auth.rate_limited(ip):
        return JSONResponse({"error": "Too many attempts — wait a few minutes."}, status_code=429)
    body = await request.json()
    if not auth.check_password(str(body.get("password", ""))):
        state.ledger.append_audit("LOGIN_FAILED", "web", {"ip": ip})
        return JSONResponse({"error": "Wrong password, or no DASHBOARD_PASSWORD is configured."},
                            status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, auth.make_session(), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="strict", secure=auth.cookie_secure(request))
    state.ledger.append_audit("LOGIN", "user", {"ip": ip})
    return resp


@app.post("/api/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Status & control
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    """Liveness check that touches nothing external."""
    return {"ok": True}


@app.get("/api/status")
async def get_status() -> dict:
    sys_state = state.ledger.get_system_state()
    await _check_kalshi()
    next_runs = {}
    if state.scheduler and state.scheduler.running:
        for job in state.scheduler.get_jobs():
            if job.next_run_time:
                next_runs[job.id] = job.next_run_time.isoformat()
    return {
        "mode": sys_state.mode.value,
        "halted": sys_state.halted,
        "halt_reason": sys_state.halt_reason,
        "agents_running": state.agents_running,
        "research_running": bool(state.jobs and state.jobs.research_running),
        "agent_statuses": state.agent_statuses,
        "kalshi_connected": state.kalshi_connected,
        "env": os.getenv("KALSHI_ENV", "demo"),
        "next_runs": next_runs,
        "password_set": auth._password() is not None,
    }


@app.get("/api/local-llm")
async def local_llm_status() -> dict:
    from src.common.config import load_runtime_config
    from src.llm.ollama import OllamaClient

    rt = load_runtime_config()
    cfg = rt.get("local_llm") or {}
    client = OllamaClient(rt["models"].get("local", "qwen3.5:4b"), host=cfg.get("host"))
    ok, detail = await client.health()
    return {"enabled": bool(cfg.get("enabled")), "model": client.model, "host": client.host,
            "ok": ok, "detail": detail}


@app.post("/api/agents/start")
async def start_agents() -> dict:
    if state.agents_running:
        return {"success": False, "message": "Agents already running"}
    try:
        state.scheduler = state.jobs.build_scheduler()
    except ConfigError as e:
        return {"success": False, "message": str(e)}
    state.scheduler.start()
    state.agents_running = True
    for k in state.agent_statuses:
        state.agent_statuses[k] = "idle"
    push_event("agents_started", {"message": "Trading agents started"})
    asyncio.create_task(state.jobs.order_manager())
    return {"success": True}


@app.post("/api/agents/stop")
async def stop_agents() -> dict:
    if state.scheduler and state.scheduler.running:
        state.scheduler.shutdown(wait=False)
    state.scheduler = None
    state.agents_running = False
    for k in state.agent_statuses:
        state.agent_statuses[k] = "stopped"
    push_event("agents_stopped", {"message": "Trading agents stopped"})
    return {"success": True}


@app.post("/api/agents/run-now")
async def run_now() -> dict:
    if state.jobs.research_running:
        return {"success": False, "message": "Research cycle already in progress"}
    asyncio.create_task(state.jobs.research(trigger="manual"))
    push_event("system", {"message": "Research cycle triggered manually"})
    return {"success": True}


@app.post("/api/orders/sync")
async def sync_orders_now() -> dict:
    summary = await state.jobs.order_manager()
    return {"success": summary is not None, "summary": summary}


@app.post("/api/mode/{mode}")
async def set_mode(mode: str) -> dict:
    try:
        new_mode = OperationalMode(mode.lower())
    except ValueError:
        return {"success": False, "error": f"Invalid mode '{mode}'. Valid: {[m.value for m in OperationalMode]}"}
    sys_state = state.ledger.get_system_state()
    sys_state.mode = new_mode
    state.ledger.set_system_state(sys_state, actor="user")
    push_event("system", {"message": f"Mode changed to {new_mode.value.upper()}"})
    return {"success": True, "mode": new_mode.value}


@app.post("/api/halt")
async def halt_system(reason: str = "Manual halt via dashboard") -> dict:
    state.ledger.halt(reason, "user")
    push_event("system_halted", {"reason": reason})
    return {"success": True}


@app.post("/api/resume")
async def resume_system() -> dict:
    state.ledger.resume("user")
    push_event("system_resumed", {})
    return {"success": True}


@app.post("/api/flatten")
async def flatten() -> dict:
    """Halt, cancel every resting order and sell every position at market."""
    from src.execute.orders import flatten_all
    from src.kalshi_client.client import KalshiClient

    state.ledger.halt("Flatten requested from dashboard", "user")
    async with KalshiClient() as client:
        result = await flatten_all(client, state.ledger, actor="user")
    push_event("system_halted", {"reason": "Flattened all positions"})
    return {"success": not result["errors"], **result}


# ---------------------------------------------------------------------------
# Research runs & assessments
# ---------------------------------------------------------------------------


@app.get("/api/runs")
async def list_runs(limit: int = 30) -> dict:
    runs = state.ledger.list_runs(limit)
    return {"runs": [
        {k: v for k, v in r.model_dump(mode="json").items() if k != "candidates"}
        | {"candidate_count": len(r.candidates)}
        for r in runs
    ]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    run = state.ledger.get_run(run_id)
    if run is None:
        return {"error": "not found"}
    return {"run": run.model_dump(mode="json"),
            "assessments": [a.model_dump(mode="json") for a in state.ledger.list_assessments(run_id=run_id)]}


@app.get("/api/assessments")
async def list_assessments(limit: int = 200, resolved_only: bool = False) -> dict:
    return {"assessments": [a.model_dump(mode="json")
                            for a in state.ledger.list_assessments(limit=limit, resolved_only=resolved_only)]}


@app.get("/api/recommendations")
async def get_recommendations(limit: int = 100) -> dict:
    from sqlalchemy import text as sql_text

    rows = []
    with state.ledger._session() as s:
        result = s.execute(sql_text(
            "SELECT r.json_blob, d.decision, d.json_blob FROM recommendations r "
            "LEFT JOIN decisions d ON r.rec_id = d.rec_id ORDER BY r.created_at DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    for rec_blob, verdict, dec_blob in result:
        r, d = json.loads(rec_blob), (json.loads(dec_blob) if dec_blob else {})
        ao = d.get("approved_order") or {}
        rows.append({
            "rec_id": r.get("rec_id"),
            "created_at": r.get("created_at"),
            "market": r.get("market_ticker"),
            "title": r.get("title", ""),
            "side": r.get("side"),
            "price": r.get("limit_price_cents"),
            "max_price": r.get("max_price_cents"),
            "size": ao.get("size_contracts") or r.get("target_size_contracts"),
            "fair_yes_prob": r.get("fair_yes_prob"),
            "edge_cents": r.get("edge_cents_after_fees"),
            "edge_bps": r.get("edge_bps"),
            "confidence": r.get("confidence"),
            "verdict": verdict or "pending",
            "qa_notes": d.get("qa_notes", ""),
            "thesis": r.get("thesis", ""),
            "sources": r.get("sources", []),
        })
    return {"recommendations": rows}


# ---------------------------------------------------------------------------
# Orders & approvals
# ---------------------------------------------------------------------------


def _order_view(o) -> dict:
    rec = state.ledger.get_recommendation(o.rec_id) if o.rec_id not in ("external", "flatten") else None
    return {
        "order_id": o.order_id, "rec_id": o.rec_id, "market": o.market_ticker,
        "title": rec.title if rec else "", "side": o.side.value, "action": o.action,
        "purpose": o.purpose.value, "price": o.limit_price_cents, "max_price": o.max_price_cents,
        "size": o.size_contracts, "filled": o.filled_contracts,
        "avg_fill_price": o.avg_fill_price_cents, "fees_cents": o.fees_cents,
        "status": o.status.value, "status_reason": o.status_reason,
        "updated_at": o.last_updated_at.isoformat() if o.last_updated_at else None,
        "expires_at": o.expires_at.isoformat() if o.expires_at else None,
        "kalshi_order_id": o.kalshi_order_id,
        "thesis": rec.thesis[:240] if rec else None,
        "confidence": rec.confidence.value if rec else None,
        "fair_yes_prob": rec.fair_yes_prob if rec else None,
        "edge_cents": rec.edge_cents_after_fees if rec else None,
        "sources": [s.model_dump(mode="json") for s in rec.sources] if rec else [],
    }


@app.get("/api/pending")
async def get_pending() -> dict:
    orders = state.ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
    out = []
    for o in orders:
        v = _order_view(o)
        dec = state.ledger.get_decision(o.rec_id)
        v["qa_notes"] = dec.qa_notes if dec else ""
        v["edge_bps"] = int(round((v["edge_cents"] or 0) * 100))
        v["sources"] = [{"title": s["title"], "url": s["url"], "trust_tier": s["trust_tier"]}
                        for s in v["sources"]]
        out.append(v)
    return {"pending": out}


@app.get("/api/orders")
async def get_recent_orders(limit: int = 50) -> dict:
    return {"orders": [_order_view(o) for o in state.ledger.list_orders(limit)
                       if o.status != OrderStatus.PENDING_APPROVAL]}


async def _submit_entry(order, size: int | None = None, max_price: int | None = None) -> dict:
    """Pre-flight and submit an entry order that is waiting locally."""
    from src.execute.orders import PreflightError, place_order
    from src.kalshi_client.client import KalshiClient

    ao = ApprovedOrder(
        market_ticker=order.market_ticker, side=order.side,
        limit_price_cents=min(order.limit_price_cents, max_price or 99),
        max_price_cents=max_price or order.max_price_cents or order.limit_price_cents,
        size_contracts=size or order.size_contracts,
        expiry_ts=order.expires_at or utcnow() + timedelta(hours=6),
        client_order_id=order.client_order_id,
    )
    state.agent_statuses["execute"] = "running"
    try:
        async with KalshiClient() as client:
            submitted = await place_order(ao, order.rec_id, client, state.ledger, existing=order,
                                          market_close_time=order.market_close_time)
        push_event("order_submitted", {"rec_id": order.rec_id, "market": order.market_ticker,
                                       "price": submitted.limit_price_cents,
                                       "size": submitted.size_contracts,
                                       "kalshi_order_id": submitted.kalshi_order_id})
        return {"success": True, "kalshi_order_id": submitted.kalshi_order_id,
                "price": submitted.limit_price_cents, "size": submitted.size_contracts,
                "note": submitted.status_reason}
    except PreflightError as e:
        return {"success": False, "error": f"Not sent: {e}"}
    except Exception as e:
        log.error("submit_failed", rec_id=order.rec_id, error=str(e))
        return {"success": False, "error": f"Submission failed: {e}"}
    finally:
        state.agent_statuses["execute"] = "idle"


@app.post("/api/approve/{rec_id}")
async def approve_trade(rec_id: str, request: Request) -> dict:
    """Approve a pending trade. Optional JSON body {size, max_price} may only tighten
    what QA approved: fewer contracts, or a lower maximum price."""
    pending = [o for o in state.ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
               if o.rec_id == rec_id]
    if not pending:
        return {"success": False, "error": "Order not found or already processed"}
    order = pending[0]
    try:
        body = await request.json()
    except Exception:
        body = {}
    size = body.get("size")
    max_price = body.get("max_price")
    if size is not None and not (isinstance(size, int) and 1 <= size <= order.size_contracts):
        return {"success": False, "error": f"size must be 1–{order.size_contracts}"}
    cap = order.max_price_cents or order.limit_price_cents
    if max_price is not None and not (isinstance(max_price, int) and 1 <= max_price <= cap):
        return {"success": False, "error": f"max_price must be 1–{cap}¢"}
    state.ledger.append_audit("HITL_APPROVED", "user", {"rec_id": rec_id, "order_id": order.order_id,
                                                        "size": size, "max_price": max_price})
    push_event("trade_approved", {"rec_id": rec_id, "market": order.market_ticker})
    return await _submit_entry(order, size=size, max_price=max_price)


@app.post("/api/deny/{rec_id}")
async def deny_trade(rec_id: str, reason: str = "Denied via dashboard") -> dict:
    from src.execute.hitl import deny as hitl_deny

    if hitl_deny(rec_id, state.ledger, reason):
        push_event("trade_denied", {"rec_id": rec_id, "reason": reason})
        return {"success": True}
    return {"success": False, "error": "Order not found or already processed"}


@app.post("/api/resubmit/{rec_id}")
async def resubmit_order(rec_id: str, contracts: int | None = None) -> dict:
    """Send a new entry order for a recommendation whose earlier order didn't fill.

    Pre-flight always runs: the price is re-quoted from the live book and is
    never above the recommendation's max price.
    """
    retryable = {OrderStatus.APPROVED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                 OrderStatus.FAILED, OrderStatus.PENDING_APPROVAL}
    orders = [o for o in state.ledger.get_orders_for_rec(rec_id)
              if o.purpose == OrderPurpose.ENTRY]
    if any(o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED) for o in orders):
        return {"success": False, "error": "An order for this trade is already resting"}
    last = next((o for o in reversed(orders) if o.status in retryable), None)
    if last is None:
        return {"success": False, "error": f"No retryable order for rec_id={rec_id}"}
    if contracts is not None and contracts < 1:
        return {"success": False, "error": "contracts must be >= 1"}
    fresh = last.model_copy(update={
        "order_id": new_id(), "client_order_id": new_id(), "kalshi_order_id": None,
        "filled_contracts": 0, "status": OrderStatus.APPROVED, "status_reason": "resubmitted",
        "expires_at": utcnow() + timedelta(hours=6), "replaces_order_id": last.order_id,
    })
    state.ledger.save_order(fresh)
    return await _submit_entry(fresh, size=contracts)


@app.post("/api/exit/{ticker}")
async def exit_position(ticker: str) -> dict:
    """Sell an open position now (IOC, reduce-only) at no less than bid − max slippage."""
    from src.common.config import load_exit_config
    from src.execute.orders import sell_now
    from src.kalshi_client.client import KalshiClient

    cfg = load_exit_config()
    async with KalshiClient() as client:
        pos = next((p for p in await client.get_positions() if p.market_ticker == ticker and p.side), None)
        if pos is None:
            return {"success": False, "error": "No open position in that market"}
        bid = (await client.get_orderbook(ticker)).bid(pos.side)
        if bid is None:
            return {"success": False, "error": "Nobody is bidding on that side right now"}
        floor = max(1, bid - cfg.max_slippage_cents)
        rec_id = next((o.rec_id for o in reversed(state.ledger.get_orders_for_ticker(ticker))
                       if o.purpose == OrderPurpose.ENTRY), "manual")
        state.ledger.append_audit("EXIT_TRIGGERED", "user", {"ticker": ticker, "floor": floor,
                                                             "reason": "manual exit from dashboard"})
        order = await sell_now(ticker, pos.side, abs(pos.position), floor, rec_id, client,
                               state.ledger, reason="manual exit from dashboard")
    push_event("exit", {"ticker": ticker, "reason": "manual", "filled": order.filled_contracts,
                        "contracts": abs(pos.position)})
    return {"success": True, "filled": order.filled_contracts, "contracts": abs(pos.position),
            "floor": floor}


@app.post("/api/cancel/{order_id}")
async def cancel_resting(order_id: str) -> dict:
    from src.execute.orders import cancel
    from src.kalshi_client.client import KalshiClient

    order = state.ledger.get_order(order_id)
    if order is None or order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
        return {"success": False, "error": "No resting order with that id"}
    async with KalshiClient() as client:
        await cancel(order, client, state.ledger, reason="cancelled from dashboard")
    return {"success": order.status == OrderStatus.CANCELLED}


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@app.get("/api/market/{ticker}")
async def market_snapshot(ticker: str) -> dict:
    """Live book for one market, from both sides, for the approval cards."""
    from src.kalshi_client.client import KalshiClient
    try:
        async with KalshiClient() as client:
            m = await client.get_market(ticker)
            ob = await client.get_orderbook(ticker)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    side = lambda s: {"bid": ob.bid(s), "ask": ob.ask(s),
                      "bid_depth": ob.bid_depth(s, ob.bid(s)) if ob.bid(s) else 0}
    return {"ok": True, "ticker": ticker, "title": m.title, "status": m.status,
            "close_time": m.close_time.isoformat() if m.close_time else None,
            "volume_24h": m.volume_24h, "open_interest": m.open_interest,
            "yes": side(Side.YES), "no": side(Side.NO)}


async def _series_list() -> list[dict]:
    """All Kalshi series (cached 6h in memory and on disk)."""
    from src.common.ledger import DATA_DIR
    from src.kalshi_client.client import KalshiClient

    if state.series_cache and time.time() - state.series_cache[0] < 6 * 3600:
        return state.series_cache[1]
    cache_file = DATA_DIR / "series_cache.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 6 * 3600:
        rows = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        async with KalshiClient(timeout=60) as client:
            series = await client.get_series_list(include_volume=True)
        rows = [{"ticker": s.ticker, "title": s.title, "category": s.category,
                 "frequency": s.frequency, "volume": s.volume, "tags": s.tags or []}
                for s in series]
        cache_file.write_text(json.dumps(rows), encoding="utf-8")
    rows.sort(key=lambda r: r["volume"], reverse=True)
    state.series_cache = (time.time(), rows)
    return rows


@app.get("/api/series")
async def browse_series(category: str | None = None, q: str | None = None,
                        frequency: str | None = None, limit: int = 60) -> dict:
    try:
        rows = await _series_list()
    except Exception as e:
        return {"ok": False, "error": str(e), "series": [], "categories": []}
    categories: dict[str, int] = {}
    for r in rows:
        categories[r["category"]] = categories.get(r["category"], 0) + 1
    out = rows
    if category:
        out = [r for r in out if r["category"] == category]
    if frequency:
        out = [r for r in out if r["frequency"] == frequency]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r["ticker"].lower() or ql in r["title"].lower()
               or any(ql in t.lower() for t in r["tags"])]
    included = set((load_yaml("universe").get("categories") or {}).get("include") or [])
    return {
        "ok": True,
        "categories": sorted(categories.items(), key=lambda kv: -kv[1]),
        "series": [r | {"included": r["ticker"] in included} for r in out[:limit]],
        "total": len(out),
    }


# ---------------------------------------------------------------------------
# Portfolio & performance
# ---------------------------------------------------------------------------


@app.get("/api/portfolio")
async def get_portfolio() -> dict:
    """Live balance, positions marked at the current bid, and resting orders."""
    from src.kalshi_client.client import KalshiClient
    try:
        async with KalshiClient() as client:
            balance = await client.get_balance()
            positions = [p for p in await client.get_positions() if p.position != 0]
            resting = await client.get_orders(status="resting")
            marks = {}
            if positions:
                for m in await client.get_markets(status=None, tickers=[p.market_ticker for p in positions],
                                                  limit=len(positions)):
                    marks[m.ticker] = m
        reserved = sum((o.yes_price if o.outcome_side == "yes" else o.no_price or 0) * o.remaining_count
                       for o in resting)
        from src.common.config import load_exit_config
        from src.execute.exits import plan_for

        exit_cfg = load_exit_config()
        pos_rows, total_unrealized = [], 0
        for p in positions:
            side = p.side or Side.YES
            m = marks.get(p.market_ticker)
            bid = (m.yes_bid if side == Side.YES else m.no_bid) if m else None
            qty = abs(p.position)
            value = bid * qty if bid is not None else None
            unrealized = (value - abs(p.market_exposure)) if value is not None else None
            total_unrealized += unrealized or 0
            plan = plan_for(state.ledger, p.market_ticker, side, qty, abs(p.market_exposure), exit_cfg)
            pos_rows.append({
                "managed": plan is not None,
                "entry_cents": plan.entry_cents if plan else None,
                "fair_cents": plan.fair_cents if plan else None,
                "take_profit_cents": plan.take_profit_cents if plan and exit_cfg.enabled else None,
                "stop_loss_cents": plan.stop_loss_cents if plan and exit_cfg.enabled else None,
                "invalidation_event": plan.invalidation_event if plan else "",
                "ticker": p.market_ticker, "title": m.title if m else "",
                "side": side.value, "position": p.position, "contracts": qty,
                "exposure_cents": abs(p.market_exposure), "mark_cents": bid,
                "value_cents": value, "unrealized_pnl_cents": unrealized,
                "realized_pnl_cents": p.realized_pnl, "fees_cents": p.fees_paid,
                "total_cost_cents": abs(p.market_exposure),
                "close_time": m.close_time.isoformat() if m and m.close_time else None,
            })
        return {
            "ok": True,
            "balance_cents": balance.balance,
            "portfolio_value_cents": balance.portfolio_value,
            "reserved_cents": reserved,
            "unrealized_pnl_cents": total_unrealized,
            "open_orders_count": len(resting),
            "positions": pos_rows,
        }
    except Exception as e:
        log.warning("portfolio_fetch_failed", error=str(e))
        return {"ok": False, "error": str(e), "balance_cents": None, "reserved_cents": None,
                "unrealized_pnl_cents": None, "open_orders_count": None, "positions": []}


@app.get("/api/performance")
async def get_performance() -> dict:
    from sqlalchemy import text as sql_text

    from src.performance.tracker import calibration_by_category, current_metrics, reliability_bins

    stats: dict[str, Any] = {}
    with state.ledger._session() as s:
        q = lambda sql: s.execute(sql_text(sql)).scalar() or 0
        stats["total_recs"] = q("SELECT COUNT(*) FROM recommendations")
        stats["approved"] = q("SELECT COUNT(*) FROM decisions WHERE decision IN ('APPROVE','MODIFY')")
        stats["denied"] = q("SELECT COUNT(*) FROM decisions WHERE decision = 'DENY'")
        stats["pending"] = q("SELECT COUNT(*) FROM orders WHERE status = 'PENDING_APPROVAL'")
        stats["submitted"] = q("SELECT COUNT(*) FROM orders WHERE kalshi_order_id IS NOT NULL")
        stats["assessments"] = q("SELECT COUNT(*) FROM assessments WHERE fair_yes_prob IS NOT NULL")

    m = current_metrics(state.ledger)
    resolved = state.ledger.list_assessments(resolved_only=True, limit=10000)
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    budget = (load_yaml("runtime").get("budget") or {})
    pnl_series, running = [], 0
    for st in state.ledger.list_settlements():
        running += st.pnl_cents
        pnl_series.append({"t": st.settled_at.isoformat() if st.settled_at else None,
                           "pnl_cents": running, "ticker": st.market_ticker, "delta": st.pnl_cents})
    spend_series, spent = [], 0.0
    for r in sorted(state.ledger.list_runs(limit=5000), key=lambda r: r.started_at):
        if r.cost_usd:
            spent += r.cost_usd
            spend_series.append({"t": r.started_at.isoformat(), "spend_cents": round(spent * 100, 2)})
    stats.update({
        "pnl_series": pnl_series,
        "spend_series": spend_series,
        "realized_pnl_cents": running,
        "settled_markets": len(pnl_series),
        "total_trades": m.total_trades,
        "total_pnl_cents": m.total_pnl_cents,
        "brier_score": m.brier_score,
        "assessment_brier": m.assessment_brier,
        "assessments_resolved": m.assessments_resolved,
        "max_drawdown_cents": m.max_drawdown_cents,
        "consecutive_losses": m.consecutive_losses,
        "pnl_by_category": m.pnl_by_category,
        "calibration_by_category": calibration_by_category(resolved),
        "reliability": reliability_bins(resolved),
        "api_spend_month_usd": round(state.ledger.api_spend_since(month_start), 4),
        "api_spend_total_usd": round(state.ledger.api_spend_since(utcnow() - timedelta(days=3650)), 4),
        "api_budget_month_usd": budget.get("monthly_usd"),
    })
    return stats


@app.get("/api/settlements")
async def get_settlements() -> dict:
    return {"settlements": [s.model_dump(mode="json") for s in state.ledger.list_settlements()]}


@app.get("/api/audit")
async def get_audit(limit: int = 100) -> dict:
    return {"entries": state.ledger.list_audit(limit)}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.get("/api/settings")
async def get_settings() -> dict:
    return {name: load_yaml(name) for name in EDITABLE_SECTIONS}


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        else:
            out[path] = v
    return out


def _nest(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for path, v in flat.items():
        node = out
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = v
    return out


def _apply_changes(body: dict, actor: str = "user", note: str = "") -> dict:
    saved, errors = [], []
    for section, data in body.items():
        if not isinstance(data, dict):
            errors.append(f"{section}: expected an object")
            continue
        before_all = _flatten(load_yaml(section)) if section in EDITABLE_SECTIONS else {}
        try:
            after_all = _flatten(update_section(section, data))
        except ConfigError as e:
            errors.append(f"{section}: {e}")
            continue
        changed = {k: {"before": before_all.get(k), "after": v} for k, v in after_all.items()
                   if before_all.get(k) != v}
        if changed:
            state.ledger.append_audit("SETTINGS_CHANGED", actor,
                                      {"section": section, "changes": changed, "note": note})
        saved.append(section)
    return {"success": not errors, "saved": saved, "errors": errors}


@app.post("/api/settings")
async def save_settings(request: Request) -> dict:
    """Merge the posted sections into the config files (validated before writing)."""
    return _apply_changes(await request.json())


@app.get("/api/settings/history")
async def settings_history(limit: int = 30) -> dict:
    entries = [e for e in state.ledger.list_audit(limit=2000) if e["type"] == "SETTINGS_CHANGED"]
    return {"history": entries[:limit]}


@app.post("/api/settings/revert/{seq}")
async def revert_settings(seq: int) -> dict:
    entry = next((e for e in state.ledger.list_audit(limit=5000)
                  if e["seq"] == seq and e["type"] == "SETTINGS_CHANGED"), None)
    if entry is None:
        return {"success": False, "errors": ["No such settings change"]}
    changes = entry["payload"].get("changes") or {}
    if not all(isinstance(c, dict) and "before" in c for c in changes.values()):
        return {"success": False, "errors": ["This change predates revert support"]}
    flat = {k: c["before"] for k, c in changes.items() if c["before"] is not None}
    return _apply_changes({entry["payload"]["section"]: _nest(flat)}, note=f"revert of #{seq}")


_INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "value_json": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "value_json", "reason"],
            },
        },
        "unsupported": {"type": "string"},
    },
    "required": ["changes", "unsupported"],
}


@app.post("/api/settings/interpret")
async def interpret_settings(request: Request) -> dict:
    """Turn a plain-English request into proposed settings changes (nothing is saved)."""
    from src.common.config import VALIDATORS, deep_merge, load_runtime_config
    from src.llm.ollama import OllamaClient, OllamaUnavailable

    text = str((await request.json()).get("text", ""))[:1000].strip()
    if not text:
        return {"ok": False, "error": "Describe the change you want."}
    current = {s: load_yaml(s) for s in EDITABLE_SECTIONS}
    flat = {f"{s}.{k}": v for s in EDITABLE_SECTIONS for k, v in _flatten(current[s]).items()}
    rt = load_runtime_config()
    client = OllamaClient(rt["models"].get("local", "qwen3.5:4b"),
                          host=(rt.get("local_llm") or {}).get("host"), timeout=120)
    listing = "\n".join(f"{k} = {json.dumps(v)}" for k, v in flat.items())
    try:
        catalog = "\n".join(f"{r['ticker']} — {r['title']} ({r['category']}, {r['frequency']})"
                            for r in (await _series_list())[:150])
    except Exception:
        catalog = "(unavailable)"
    try:
        out = await client.json_chat(
            "You translate a trader's plain-English request into changes to a YAML config. "
            "Only use paths from the settings list. Percentages are fractions (25% = 0.25). "
            "Dollar amounts are plain numbers. value_json is ONLY the new value encoded as JSON "
            "— a bare number like 5, a string, true/false, or a list — never an object. "
            "Example: {\"path\": \"risk.per_trade.max_loss_usd\", \"value_json\": \"5\", ...}. "
            "universe.categories.include is the full new list of Kalshi series tickers; pick "
            "tickers from the series catalog (series with 'GAME' in the ticker are individual "
            "games; others are often season-long futures). If part of the request can't be "
            "expressed with these settings, say so in 'unsupported'. Reply with JSON only.",
            f"Current settings:\n{listing}\n\nKalshi series catalog (busiest first):\n{catalog}\n\n"
            f"Request: {text}",
            _INTERPRET_SCHEMA,
        )
    except OllamaUnavailable as e:
        return {"ok": False, "error": f"Local model unavailable: {e}"}

    proposed: dict[str, dict] = {}
    rejected: list[str] = []
    rows = []
    for ch in out.get("changes", []):
        path = str(ch.get("path", ""))
        if path not in flat:
            rejected.append(f"{path}: not a known setting")
            continue
        try:
            value = json.loads(ch.get("value_json", ""))
        except json.JSONDecodeError:
            rejected.append(f"{path}: unreadable value {ch.get('value_json')!r}")
            continue
        # Small models sometimes wrap a scalar as {"name": value}; unwrap to the setting's type
        if isinstance(value, dict) and not isinstance(flat[path], dict) and len(value) == 1:
            value = next(iter(value.values()))
        if isinstance(flat[path], (int, float)) and not isinstance(flat[path], bool) \
                and isinstance(value, str):
            try:
                value = float(value.strip().lstrip("$").rstrip("%"))
            except ValueError:
                pass
        section, rest = path.split(".", 1)
        proposed.setdefault(section, {})[rest] = value
        rows.append({"path": path, "before": flat[path], "after": value,
                     "reason": str(ch.get("reason", ""))[:200]})
    errors = []
    nested = {s: _nest(v) for s, v in proposed.items()}
    for s, changes in nested.items():
        errors += [f"{s}: {e}" for e in VALIDATORS[s](deep_merge(current[s], changes))]
    return {"ok": True, "changes": rows, "apply": nested, "errors": errors,
            "rejected": rejected, "unsupported": str(out.get("unsupported", ""))[:500]}


@app.get("/api/feed")
async def get_feed(limit: int = 100) -> dict:
    return {"events": list(state.live_feed)[-limit:]}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not auth.authorized(websocket) or not auth.origin_ok(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    state.ws_clients.add(websocket)
    try:
        sys_state = state.ledger.get_system_state()
        await websocket.send_text(json.dumps({
            "type": "init",
            "feed": list(state.live_feed)[-50:],
            "agent_statuses": state.agent_statuses,
            "agents_running": state.agents_running,
            "kalshi_connected": state.kalshi_connected,
            "halted": sys_state.halted,
        }, default=str))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.ws_clients.discard(websocket)
