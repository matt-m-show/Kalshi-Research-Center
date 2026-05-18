"""FastAPI web application for the Kalshi trading system.

Serves:
  GET  /                       → dashboard SPA (index.html)
  GET  /api/status             → system state, agent statuses, Kalshi connection
  GET  /api/recommendations    → recent recommendations + QA decisions
  GET  /api/pending            → orders awaiting HITL approval
  POST /api/approve/{rec_id}   → approve a pending trade
  POST /api/deny/{rec_id}      → deny a pending trade
  POST /api/agents/start       → start the APScheduler trading loop
  POST /api/agents/stop        → stop the APScheduler trading loop
  POST /api/agents/run-now     → trigger an immediate research cycle
  POST /api/halt               → emergency halt
  POST /api/resume             → clear halt
  GET  /api/performance        → rolling performance metrics
  GET  /api/feed               → recent live-feed events (REST fallback)
  WS   /ws                     → WebSocket live updates
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.common.ledger import DATA_DIR, Ledger
from src.common.logging import get_logger

log = get_logger("web")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Global application state
# ---------------------------------------------------------------------------


class AppState:
    def __init__(self) -> None:
        self.scheduler = None           # AsyncIOScheduler | None
        self.orchestrator = None        # Orchestrator | None
        self.ledger: Ledger = Ledger()
        self.agents_running: bool = False
        self.research_running: bool = False
        self.agent_statuses: dict[str, str] = {
            "research": "stopped",
            "qa": "stopped",
            "execute": "stopped",
            "performance": "stopped",
        }
        self.kalshi_connected: bool = False
        self.live_feed: deque = deque(maxlen=300)
        self.ws_clients: set[WebSocket] = set()


state = AppState()


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def push_event(event_type: str, data: dict) -> None:
    """Append event to live feed and schedule a WebSocket broadcast."""
    event: dict[str, Any] = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    state.live_feed.append(event)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_broadcast(event))
    except RuntimeError:
        pass


async def _broadcast(event: dict) -> None:
    if not state.ws_clients:
        return
    msg = json.dumps(event)
    dead: set[WebSocket] = set()
    for ws in list(state.ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead


# ---------------------------------------------------------------------------
# Kalshi connectivity check
# ---------------------------------------------------------------------------


async def _check_kalshi() -> bool:
    try:
        from src.kalshi_client.client import KalshiClient
        async with KalshiClient(timeout=5) as client:
            markets = await client.get_markets(status="open", limit=1)
            state.kalshi_connected = True
    except Exception:
        state.kalshi_connected = False
    return state.kalshi_connected


# ---------------------------------------------------------------------------
# Scheduler job wrappers (emit events via push_event)
# ---------------------------------------------------------------------------


async def _research_job() -> None:
    if state.research_running:
        return
    state.research_running = True
    state.agent_statuses["research"] = "running"
    state.agent_statuses["qa"] = "running"
    push_event("agent_status", {"agent": "research", "status": "running"})
    push_event("agent_status", {"agent": "qa", "status": "running"})
    try:
        recs = await state.orchestrator.run_research_cycle()
        state.agent_statuses["research"] = "idle"
        state.agent_statuses["qa"] = "idle"
        push_event("agent_status", {"agent": "research", "status": "idle"})
        push_event("agent_status", {"agent": "qa", "status": "idle"})
        push_event("research_complete", {"rec_count": len(recs)})

        # Emit each approved recommendation with its sources
        for rec in recs:
            dec = state.ledger.get_decision(rec.rec_id)
            push_event(
                "new_recommendation",
                {
                    "rec_id": rec.rec_id,
                    "market": rec.market_ticker,
                    "side": rec.side.value,
                    "price": rec.limit_price_cents,
                    "size": rec.target_size_contracts,
                    "edge_bps": rec.edge_bps,
                    "confidence": rec.confidence.value,
                    "verdict": dec.decision.value if dec else "unknown",
                    "thesis": rec.thesis[:200],
                    "sources": [
                        {"title": s.title, "url": s.url, "tier": int(s.trust_tier)}
                        for s in rec.sources
                    ],
                },
            )
    except Exception as e:
        state.agent_statuses["research"] = "error"
        state.agent_statuses["qa"] = "error"
        push_event("agent_status", {"agent": "research", "status": "error", "error": str(e)})
        log.error("research_job_failed", error=str(e))
    finally:
        state.research_running = False


async def _mtm_job() -> None:
    state.agent_statuses["performance"] = "running"
    push_event("agent_status", {"agent": "performance", "status": "running"})
    try:
        from src.performance.metrics import compute_rolling_metrics
        from src.performance.reporting import save_rolling_metrics

        metrics = compute_rolling_metrics([])
        save_rolling_metrics(metrics)
        state.agent_statuses["performance"] = "idle"
        push_event("agent_status", {"agent": "performance", "status": "idle"})
    except Exception as e:
        state.agent_statuses["performance"] = "error"
        push_event("agent_status", {"agent": "performance", "status": "error"})
        log.error("mtm_job_failed", error=str(e))


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.orchestrator import Orchestrator

    state.orchestrator = Orchestrator()
    await _check_kalshi()
    push_event("system", {"message": "Kalshi Trading System web server started"})
    log.info("web_server_started")
    yield
    if state.scheduler and state.scheduler.running:
        state.scheduler.shutdown(wait=False)
    log.info("web_server_stopped")


# ---------------------------------------------------------------------------
# App + static files
# ---------------------------------------------------------------------------

app = FastAPI(title="Kalshi Trading System", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def get_status() -> dict:
    sys_state = state.ledger.get_system_state()
    await _check_kalshi()
    return {
        "mode": sys_state.mode.value,
        "halted": sys_state.halted,
        "halt_reason": sys_state.halt_reason,
        "agents_running": state.agents_running,
        "agent_statuses": state.agent_statuses,
        "kalshi_connected": state.kalshi_connected,
        "env": os.getenv("KALSHI_ENV", "demo"),
    }


@app.get("/api/recommendations")
async def get_recommendations(limit: int = 100) -> dict:
    from sqlalchemy import text as sql_text

    rows = []
    try:
        with state.ledger._session() as s:
            result = s.execute(
                sql_text(
                    "SELECT r.json_blob, d.decision, d.json_blob "
                    "FROM recommendations r "
                    "LEFT JOIN decisions d ON r.rec_id = d.rec_id "
                    "ORDER BY r.created_at DESC LIMIT :lim"
                ),
                {"lim": limit},
            ).fetchall()
        for rec_blob, verdict, dec_blob in result:
            r = json.loads(rec_blob)
            d = json.loads(dec_blob) if dec_blob else {}
            thesis = r.get("thesis", "")
            rows.append(
                {
                    "rec_id": r.get("rec_id"),
                    "created_at": r.get("created_at"),
                    "market": r.get("market_ticker"),
                    "side": r.get("side"),
                    "price": r.get("limit_price_cents"),
                    "size": r.get("target_size_contracts"),
                    "edge_bps": r.get("edge_bps"),
                    "confidence": r.get("confidence"),
                    "verdict": verdict or "pending",
                    "qa_notes": d.get("qa_notes", ""),
                    "thesis": thesis,
                    "sources": r.get("sources", []),
                }
            )
    except Exception as e:
        log.error("get_recommendations_failed", error=str(e))
    return {"recommendations": rows}


@app.get("/api/pending")
async def get_pending() -> dict:
    try:
        orders = state.ledger.get_orders_by_status("PENDING_APPROVAL")
        result = []
        for o in orders:
            rec = state.ledger.get_recommendation(o.rec_id)
            dec = state.ledger.get_decision(o.rec_id)
            result.append(
                {
                    "rec_id": o.rec_id,
                    "order_id": o.order_id,
                    "market": o.market_ticker,
                    "side": o.side.value,
                    "price": o.limit_price_cents,
                    "size": o.size_contracts,
                    "thesis": rec.thesis if rec else "",
                    "sources": (
                        [
                            {"title": s.title, "url": s.url, "trust_tier": int(s.trust_tier)}
                            for s in rec.sources
                        ]
                        if rec
                        else []
                    ),
                    "qa_notes": dec.qa_notes if dec else "",
                    "edge_bps": rec.edge_bps if rec else None,
                    "confidence": rec.confidence.value if rec else None,
                }
            )
        return {"pending": result}
    except Exception as e:
        return {"pending": [], "error": str(e)}


@app.post("/api/approve/{rec_id}")
async def approve_trade(rec_id: str) -> dict:
    from datetime import timedelta

    from src.common.schemas import ApprovedOrder
    from src.execute.hitl import approve as hitl_approve
    from src.execute.orders import place_order
    from src.kalshi_client.client import KalshiClient

    # Step 1: mark APPROVED in ledger and send notification
    order = hitl_approve(rec_id, state.ledger)
    if not order:
        return {"success": False, "error": "Order not found or already processed"}

    push_event("trade_approved", {"rec_id": rec_id, "market": order.market_ticker})
    state.agent_statuses["execute"] = "running"

    # Step 2: submit to Kalshi
    try:
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        approved_order = ApprovedOrder(
            market_ticker=order.market_ticker,
            side=order.side,
            order_type=order.order_type,
            limit_price_cents=order.limit_price_cents,
            size_contracts=order.size_contracts,
            client_order_id=order.client_order_id,
            expiry_ts=expiry,
        )
        async with KalshiClient() as client:
            submitted = await place_order(approved_order, rec_id, client, state.ledger)

        state.agent_statuses["execute"] = "idle"
        push_event("order_submitted", {
            "rec_id": rec_id,
            "market": order.market_ticker,
            "kalshi_order_id": submitted.kalshi_order_id,
        })
        log.info("order_submitted_via_hitl",
                 rec_id=rec_id,
                 market=order.market_ticker,
                 kalshi_order_id=submitted.kalshi_order_id)
        return {"success": True, "order_id": order.order_id,
                "kalshi_order_id": submitted.kalshi_order_id}

    except Exception as e:
        state.agent_statuses["execute"] = "error"
        log.error("approve_submit_failed", rec_id=rec_id, error=str(e))
        push_event("order_failed", {"rec_id": rec_id, "error": str(e)})
        return {"success": False, "error": f"Order approved but submission failed: {e}"}


@app.post("/api/resubmit/{rec_id}")
async def resubmit_order(
    rec_id: str,
    contracts: int = 1,
    skip_preflight: bool = False,
    price_override: int | None = None,
) -> dict:
    """Re-submit an already-approved order.

    contracts: number of contracts to buy (default 1, minimum 1).
    skip_preflight: bypass the price-staleness check (use when market moved but you still want in).
    price_override: set a new limit price in cents (use with skip_preflight to trade at current market).
    """
    from datetime import timedelta

    from src.common.schemas import ApprovedOrder, OrderStatus
    from src.execute.orders import place_order
    from src.kalshi_client.client import KalshiClient

    try:
        if contracts < 1:
            return {"success": False, "error": "contracts must be >= 1"}

        # Find the order — accept APPROVED, PENDING_APPROVAL, or FAILED status
        all_orders = (
            state.ledger.get_orders_by_status(
                OrderStatus.APPROVED.value,
                OrderStatus.PENDING_APPROVAL.value,
                OrderStatus.FAILED.value,
            )
        )
        order = next((o for o in all_orders if o.rec_id == rec_id), None)
        if order is None:
            return {"success": False, "error": f"No resubmittable order found for rec_id={rec_id}"}

        effective_price = price_override if price_override is not None else order.limit_price_cents
        if not (1 <= effective_price <= 99):
            return {"success": False, "error": f"price_override must be 1-99, got {effective_price}"}

        # Update the order record
        order.size_contracts = contracts
        order.limit_price_cents = effective_price
        order.status = OrderStatus.APPROVED
        state.ledger.save_order(order)

        state.agent_statuses["execute"] = "running"
    except Exception as e:
        log.error("resubmit_setup_failed", rec_id=rec_id, error=str(e))
        return {"success": False, "error": str(e)}

    try:
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        approved_order = ApprovedOrder(
            market_ticker=order.market_ticker,
            side=order.side,
            order_type=order.order_type,
            limit_price_cents=effective_price,
            size_contracts=contracts,
            client_order_id=order.client_order_id,
            expiry_ts=expiry,
        )
        async with KalshiClient() as client:
            submitted = await place_order(
                approved_order, rec_id, client, state.ledger,
                skip_preflight=skip_preflight,
            )

        state.agent_statuses["execute"] = "idle"
        push_event("order_submitted", {
            "rec_id": rec_id,
            "market": order.market_ticker,
            "kalshi_order_id": submitted.kalshi_order_id,
        })
        log.info("order_resubmitted",
                 rec_id=rec_id,
                 market=order.market_ticker,
                 contracts=contracts,
                 price_cents=effective_price,
                 kalshi_order_id=submitted.kalshi_order_id)
        return {"success": True, "kalshi_order_id": submitted.kalshi_order_id,
                "market": order.market_ticker, "contracts": contracts,
                "price_cents": effective_price}

    except Exception as e:
        state.agent_statuses["execute"] = "error"
        log.error("resubmit_failed", rec_id=rec_id, error=str(e))
        return {"success": False, "error": str(e)}


@app.post("/api/deny/{rec_id}")
async def deny_trade(rec_id: str, reason: str = "Denied via dashboard") -> dict:
    from src.execute.hitl import deny as hitl_deny

    order = hitl_deny(rec_id, state.ledger, reason)
    if order:
        push_event("trade_denied", {"rec_id": rec_id, "reason": reason})
        return {"success": True}
    return {"success": False, "error": "Order not found or already processed"}


@app.get("/api/orders")
async def get_recent_orders(limit: int = 50) -> dict:
    """Return recently actioned orders (approved/submitted/cancelled/etc.) for HITL history."""
    from src.common.schemas import OrderStatus
    actioned = [
        OrderStatus.APPROVED.value, OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value,
        OrderStatus.CANCELLED.value, OrderStatus.FAILED.value,
    ]
    orders = state.ledger.get_orders_by_status(*actioned)
    orders.sort(key=lambda o: o.last_updated_at, reverse=True)
    result = []
    for o in orders[:limit]:
        rec = state.ledger.get_recommendation(o.rec_id)
        result.append({
            "order_id": o.order_id,
            "rec_id": o.rec_id,
            "market": o.market_ticker,
            "side": o.side.value,
            "price": o.limit_price_cents,
            "size": o.size_contracts,
            "status": o.status.value,
            "updated_at": o.last_updated_at.isoformat() if o.last_updated_at else None,
            "thesis": rec.thesis[:180] if rec else None,
            "confidence": rec.confidence.value if rec else None,
        })
    return {"orders": result}


@app.get("/api/portfolio")
async def get_portfolio() -> dict:
    """Fetch live balance, open orders, and positions from Kalshi."""
    from src.kalshi_client.client import KalshiClient
    try:
        async with KalshiClient() as client:
            balance = await client.get_balance()
            positions = await client.get_positions()
            orders = await client.get_orders(status="resting")

        # Reserved funds = sum of remaining contracts × price across open orders
        reserved_cents = 0
        for o in orders:
            price = o.yes_price if o.yes_price is not None else (o.no_price or 0)
            remaining = max(0, o.count - o.filled_count)
            reserved_cents += price * remaining

        total_unrealized = sum(p.unrealized_pnl for p in positions)

        return {
            "ok": True,
            "balance_cents": balance.balance,
            "reserved_cents": reserved_cents,
            "unrealized_pnl_cents": total_unrealized,
            "open_orders_count": len(orders),
            "positions": [
                {
                    "ticker": p.market_ticker,
                    "position": p.position,
                    "exposure_cents": p.market_exposure,
                    "unrealized_pnl_cents": p.unrealized_pnl,
                    "realized_pnl_cents": p.realized_pnl,
                    "total_cost_cents": p.total_cost,
                }
                for p in positions if p.position != 0
            ],
        }
    except Exception as e:
        log.warning("portfolio_fetch_failed", error=str(e))
        return {"ok": False, "error": str(e), "balance_cents": None,
                "reserved_cents": None, "unrealized_pnl_cents": None,
                "open_orders_count": None, "positions": []}


@app.get("/api/settings")
async def get_settings() -> dict:
    """Read all yaml config files and return as JSON."""
    import yaml
    from pathlib import Path
    config_dir = Path(__file__).parents[2] / "config"
    result = {}
    for fname in ["risk.yaml", "universe.yaml", "runtime.yaml"]:
        path = config_dir / fname
        if path.exists():
            result[fname.replace(".yaml", "")] = yaml.safe_load(
                path.read_text(encoding="utf-8")
            )
    return result


@app.post("/api/settings")
async def save_settings(request: Request) -> dict:
    """Write updated settings back to yaml config files."""
    import yaml
    from pathlib import Path
    config_dir = Path(__file__).parents[2] / "config"
    body = await request.json()
    ALLOWED = {"risk", "universe", "runtime"}
    saved, errors = [], []
    for section, data in body.items():
        if section not in ALLOWED:
            errors.append(f"Unknown section: {section}")
            continue
        path = config_dir / f"{section}.yaml"
        try:
            header = f"# Updated via dashboard — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            path.write_text(
                header + yaml.dump(data, default_flow_style=False,
                                   allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            saved.append(section)
        except Exception as e:
            errors.append(f"{section}: {e}")
    return {"success": not errors, "saved": saved, "errors": errors}


@app.post("/api/agents/start")
async def start_agents() -> dict:
    if state.agents_running:
        return {"success": False, "message": "Agents already running"}

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from src.orchestrator import load_runtime_config

    runtime_cfg = load_runtime_config()
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        _research_job,
        trigger=IntervalTrigger(minutes=runtime_cfg["research"]["interval_minutes"]),
        id="research",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _mtm_job,
        trigger=IntervalTrigger(minutes=runtime_cfg["mark_to_market"]["interval_minutes"]),
        id="mark_to_market",
        max_instances=1,
    )
    scheduler.start()
    state.scheduler = scheduler
    state.agents_running = True
    for k in state.agent_statuses:
        state.agent_statuses[k] = "idle"
    push_event("agents_started", {"message": "Trading agents started"})
    log.info("agents_started")
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
    log.info("agents_stopped")
    return {"success": True}


@app.post("/api/agents/run-now")
async def run_now() -> dict:
    if state.research_running:
        return {"success": False, "message": "Research cycle already in progress"}
    asyncio.create_task(_research_job())
    push_event("system", {"message": "Research cycle triggered manually"})
    return {"success": True}


@app.post("/api/mode/{mode}")
async def set_mode(mode: str) -> dict:
    from src.common.schemas import OperationalMode, SystemState
    try:
        new_mode = OperationalMode(mode.lower())
    except ValueError:
        valid = [m.value for m in OperationalMode]
        return {"success": False, "error": f"Invalid mode '{mode}'. Valid: {valid}"}
    sys_state = state.ledger.get_system_state()
    sys_state.mode = new_mode
    state.ledger.set_system_state(sys_state)
    push_event("system", {"message": f"Mode changed to {new_mode.value.upper()}"})
    log.info("mode_changed", mode=new_mode.value)
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


@app.get("/api/performance")
async def get_performance() -> dict:
    from sqlalchemy import text as sql_text

    stats: dict[str, Any] = {}
    try:
        with state.ledger._session() as s:
            stats["total_recs"] = (
                s.execute(sql_text("SELECT COUNT(*) FROM recommendations")).scalar() or 0
            )
            stats["approved"] = (
                s.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM decisions WHERE decision IN ('APPROVE','MODIFY')"
                    )
                ).scalar()
                or 0
            )
            stats["denied"] = (
                s.execute(
                    sql_text("SELECT COUNT(*) FROM decisions WHERE decision = 'DENY'")
                ).scalar()
                or 0
            )
            stats["pending"] = (
                s.execute(
                    sql_text("SELECT COUNT(*) FROM orders WHERE status = 'PENDING_APPROVAL'")
                ).scalar()
                or 0
            )
            stats["submitted"] = (
                s.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM orders "
                        "WHERE status IN ('SUBMITTED','PARTIALLY_FILLED','FILLED')"
                    )
                ).scalar()
                or 0
            )
    except Exception as e:
        log.error("get_performance_failed", error=str(e))

    metrics_path = DATA_DIR.parent / "metrics" / "rolling.json"
    if metrics_path.exists():
        try:
            stats.update(json.loads(metrics_path.read_text()))
        except Exception:
            pass
    return stats


@app.get("/api/feed")
async def get_feed(limit: int = 100) -> dict:
    return {"events": list(state.live_feed)[-limit:]}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    state.ws_clients.add(websocket)
    try:
        # Send current state + recent feed on connect
        sys_state = state.ledger.get_system_state()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "init",
                    "feed": list(state.live_feed)[-50:],
                    "agent_statuses": state.agent_statuses,
                    "agents_running": state.agents_running,
                    "kalshi_connected": state.kalshi_connected,
                    "halted": sys_state.halted,
                }
            )
        )
        while True:
            await websocket.receive_text()  # keep-alive; client sends pings
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.ws_clients.discard(websocket)
