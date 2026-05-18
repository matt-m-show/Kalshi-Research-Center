"""Rich terminal dashboard — live view of recommendations and paper performance.

Panels:
  • Header  : system mode / halted status / current time
  • Main    : recent recommendations table  |  pending HITL approvals
  • Footer  : performance summary (P&L, Brier, drawdown, counts)

Usage:
    python -m src.dashboard               # standalone (reads existing ledger)
    python scheduler.py --dashboard       # alongside the scheduler

Refresh every REFRESH_SECONDS seconds; press Ctrl+C to exit.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.common.ledger import DATA_DIR, Ledger

console = Console()
REFRESH_SECONDS = 10


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_cents(cents: float | int | None) -> str:
    if cents is None:
        return "—"
    sign = "+" if cents >= 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


def _verdict_style(v: str) -> str:
    return {"APPROVE": "bold green", "MODIFY": "bold yellow", "DENY": "bold red"}.get(v, "white")


def _confidence_style(c: str) -> str:
    return {"high": "green", "medium": "yellow", "low": "red"}.get(c.lower(), "white")


def _side_style(s: str) -> str:
    return "green" if s == "YES" else "red"


# ---------------------------------------------------------------------------
# Data loaders (all read-only from the ledger)
# ---------------------------------------------------------------------------


def _load_recent_recs(ledger: Ledger, limit: int = 20) -> list[dict[str, Any]]:
    from sqlalchemy import text as sql_text

    rows: list[dict] = []
    try:
        with ledger._session() as s:
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
                    "ts": r.get("created_at", "")[:16].replace("T", " "),
                    "market": r.get("market_ticker", ""),
                    "side": r.get("side", ""),
                    "price": r.get("limit_price_cents"),
                    "size": r.get("target_size_contracts"),
                    "edge_bps": r.get("edge_bps"),
                    "confidence": r.get("confidence", ""),
                    "verdict": verdict or "—",
                    "notes": (d.get("qa_notes") or thesis)[:70],
                    "rec_id": r.get("rec_id", ""),
                }
            )
    except Exception:
        pass
    return rows


def _load_pending(ledger: Ledger) -> list[dict]:
    try:
        orders = ledger.get_orders_by_status("PENDING_APPROVAL")
        return [
            {
                "rec_id": o.rec_id[:8] + "…",
                "market": o.market_ticker,
                "side": o.side.value,
                "price": o.limit_price_cents,
                "size": o.size_contracts,
            }
            for o in orders
        ]
    except Exception:
        return []


def _load_stats(ledger: Ledger) -> dict:
    from sqlalchemy import text as sql_text

    stats: dict[str, Any] = {}
    try:
        with ledger._session() as s:
            stats["total_recs"] = (
                s.execute(sql_text("SELECT COUNT(*) FROM recommendations")).scalar() or 0
            )
            stats["approved"] = (
                s.execute(
                    sql_text("SELECT COUNT(*) FROM decisions WHERE decision IN ('APPROVE','MODIFY')")
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
    except Exception:
        pass

    # Rolling metrics snapshot written by mark-to-market job
    metrics_path = DATA_DIR.parent / "metrics" / "rolling.json"
    if metrics_path.exists():
        try:
            m = json.loads(metrics_path.read_text())
            stats.update(
                {
                    "total_pnl_cents": m.get("total_pnl_cents", 0),
                    "brier_score": m.get("brier_score"),
                    "max_drawdown_cents": m.get("max_drawdown_cents", 0),
                    "total_trades": m.get("total_trades", 0),
                    "sharpe_30d": m.get("sharpe_30d"),
                    "edge_capture": m.get("edge_capture"),
                }
            )
        except Exception:
            pass
    return stats


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def _build_header(ledger: Ledger) -> Panel:
    state = ledger.get_system_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")

    status_text = Text("HALTED", style="bold red blink") if state.halted else Text("running", style="bold green")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold dim", min_width=14)
    grid.add_column(min_width=14)
    grid.add_column(style="bold dim", min_width=14)
    grid.add_column(min_width=14)
    grid.add_column(style="bold dim", min_width=14)
    grid.add_column()

    grid.add_row("Mode:", state.mode.value.upper(), "Status:", status_text, "Time:", now)
    if state.halted and state.halt_reason:
        grid.add_row("Halt reason:", state.halt_reason, "", "", "", "")

    return Panel(grid, title="[bold blue]Kalshi Trading System[/bold blue]", border_style="blue")


def _build_recs_table(recs: list[dict]) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold", expand=True)
    t.add_column("Time (UTC)", style="dim", width=17, no_wrap=True)
    t.add_column("Market", style="cyan", min_width=20)
    t.add_column("Side", width=5, no_wrap=True)
    t.add_column("Price", width=6, no_wrap=True)
    t.add_column("Size", width=5, no_wrap=True)
    t.add_column("Edge", width=8, no_wrap=True)
    t.add_column("Conf", width=5, no_wrap=True)
    t.add_column("QA", width=8, no_wrap=True)
    t.add_column("Notes / Thesis")

    for r in recs:
        t.add_row(
            r["ts"],
            r["market"],
            Text(r["side"], style=_side_style(r["side"])),
            f"{r['price']}¢" if r["price"] else "—",
            str(r["size"]) if r["size"] else "—",
            f"{r['edge_bps']}bp" if r["edge_bps"] else "—",
            Text((r["confidence"] or "")[:3].upper(), style=_confidence_style(r["confidence"])),
            Text(r["verdict"], style=_verdict_style(r["verdict"])),
            r["notes"],
        )

    count = len(recs)
    return Panel(
        t,
        title=f"[bold]Recent Recommendations[/bold] ({count})",
        border_style="cyan",
    )


def _build_pending_panel(pending: list[dict]) -> Panel:
    if not pending:
        return Panel(
            Text("  No trades pending approval.", style="dim"),
            title="[bold yellow]Pending Approval[/bold yellow]",
            border_style="yellow",
            height=10,
        )

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow", expand=True)
    t.add_column("rec_id", width=10)
    t.add_column("Market", style="cyan")
    t.add_column("Side", width=5)
    t.add_column("¢", width=4)
    t.add_column("N", width=4)
    t.add_column("Command", style="bold green")

    for p in pending:
        full_id = p["rec_id"].rstrip("…")
        t.add_row(
            p["rec_id"],
            p["market"],
            Text(p["side"], style=_side_style(p["side"])),
            str(p["price"]),
            str(p["size"]),
            f"/approve {full_id}",
        )

    return Panel(
        t,
        title=f"[bold yellow]Pending Approval ({len(pending)})[/bold yellow]",
        border_style="yellow",
    )


def _build_performance_panel(stats: dict) -> Panel:
    pnl = stats.get("total_pnl_cents", 0)
    pnl_style = "green" if pnl >= 0 else "red"
    brier = stats.get("brier_score")
    dd = stats.get("max_drawdown_cents", 0)
    sharpe = stats.get("sharpe_30d")
    edge_cap = stats.get("edge_capture")

    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="bold dim", min_width=22)
    grid.add_column(min_width=14)
    grid.add_column(style="bold dim", min_width=22)
    grid.add_column(min_width=14)
    grid.add_column(style="bold dim", min_width=22)
    grid.add_column()

    grid.add_row(
        "Total recommendations:", str(stats.get("total_recs", "—")),
        "Paper P&L (simulated):", Text(_fmt_cents(pnl), style=pnl_style),
        "Brier score:", f"{brier:.3f}" if brier is not None else "— (need closed trades)",
    )
    grid.add_row(
        "Approved / Denied:", f"{stats.get('approved','—')} / {stats.get('denied','—')}",
        "Max drawdown:", _fmt_cents(dd),
        "Sharpe (30d):", f"{sharpe:.2f}" if sharpe is not None else "—",
    )
    grid.add_row(
        "Pending / Submitted:", f"{stats.get('pending','—')} / {stats.get('submitted','—')}",
        "Closed trades:", str(stats.get("total_trades", "—")),
        "Edge capture:", f"{edge_cap:.1%}" if edge_cap is not None else "—",
    )

    return Panel(grid, title="[bold]Performance Summary[/bold]", border_style="green")


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------


def _render(ledger: Ledger) -> Layout:
    recs = _load_recent_recs(ledger)
    pending = _load_pending(ledger)
    stats = _load_stats(ledger)

    layout = Layout()
    layout.split_column(
        Layout(_build_header(ledger), name="header", size=5),
        Layout(name="body"),
        Layout(_build_performance_panel(stats), name="footer", size=7),
    )
    layout["body"].split_row(
        Layout(_build_recs_table(recs), name="recs", ratio=3),
        Layout(_build_pending_panel(pending), name="pending", ratio=1),
    )
    return layout


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_dashboard(ledger: Ledger | None = None, scheduler=None) -> None:
    """Async entry point — use with `await` or inside an event loop."""
    if ledger is None:
        ledger = Ledger()

    console.clear()
    with Live(_render(ledger), console=console, refresh_per_second=0.2, screen=True) as live:
        try:
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                live.update(_render(ledger))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


def main() -> None:
    """Standalone entry point: `python -m src.dashboard`"""
    asyncio.run(run_dashboard())


if __name__ == "__main__":
    main()
