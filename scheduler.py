#!/usr/bin/env python
"""Kalshi trading system entry point.

Starts the APScheduler with three recurring jobs:
  - Research cycle  (every N minutes during trading hours)
  - Mark-to-market  (every N minutes)
  - Daily report    (cron, at configured UTC hour)

Usage:
    python scheduler.py                  # demo env, no dashboard
    python scheduler.py --env prod       # prod env
    python scheduler.py --dashboard      # open Rich dashboard alongside
    python scheduler.py --run-once       # one research cycle then exit (for testing)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent / "config"


def _load_runtime() -> dict:
    return yaml.safe_load((CONFIG_DIR / "runtime.yaml").read_text())


def _load_universe() -> dict:
    return yaml.safe_load((CONFIG_DIR / "universe.yaml").read_text())


# ---------------------------------------------------------------------------
# Scheduled job functions (must be top-level for APScheduler pickling)
# ---------------------------------------------------------------------------


async def _research_job(orchestrator) -> None:
    from src.common.logging import get_logger
    log = get_logger("scheduler")
    try:
        recs = await orchestrator.run_research_cycle()
        log.info("scheduled_research_done", recommendations=len(recs))
    except Exception as e:
        log.error("scheduled_research_failed", error=str(e))


async def _mark_to_market_job(ledger) -> None:
    from src.common.logging import get_logger
    from src.performance.metrics import ClosedTrade, compute_rolling_metrics
    from src.performance.reporting import save_rolling_metrics

    log = get_logger("scheduler")
    try:
        metrics = compute_rolling_metrics([], unrealized_cents=0.0, open_positions_count=0)
        save_rolling_metrics(metrics)
        log.info("mark_to_market_done")
    except Exception as e:
        log.error("mark_to_market_failed", error=str(e))


async def _daily_report_job(ledger, risk_cfg) -> None:
    from src.common.logging import get_logger
    from src.performance.metrics import compute_rolling_metrics
    from src.performance.reporting import generate_daily_report, save_rolling_metrics

    log = get_logger("scheduler")
    try:
        metrics = compute_rolling_metrics([])
        path = generate_daily_report(metrics, risk_cfg.bankroll_cents)
        save_rolling_metrics(metrics)
        log.info("daily_report_done", path=str(path))
    except Exception as e:
        log.error("daily_report_failed", error=str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi trading system scheduler")
    parser.add_argument(
        "--env", choices=["demo", "prod"], default="demo",
        help="Kalshi environment (default: demo)"
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Open the Rich terminal dashboard alongside the scheduler"
    )
    parser.add_argument(
        "--run-once", action="store_true",
        help="Run one research cycle immediately, then exit (useful for testing)"
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Show which markets are fetched and why each is filtered — no Claude call"
    )
    args = parser.parse_args()

    # Set env var so KalshiClient reads it at import time
    os.environ["KALSHI_ENV"] = args.env

    # Deferred imports so env var is set first
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from src.common.ledger import Ledger
    from src.orchestrator import Orchestrator, load_risk_config

    ledger = Ledger()
    risk_cfg = load_risk_config()
    orchestrator = Orchestrator()
    runtime_cfg = _load_runtime()
    universe_cfg = _load_universe()

    state = ledger.get_system_state()
    halted_msg = "YES — run /resume to clear" if ledger.is_halted() else "no"

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║    Kalshi Trading System — Scheduler  ║")
    print("  ╚══════════════════════════════════════╝")
    print(f"  Environment  : {args.env.upper()}")
    print(f"  Mode         : {state.mode.value.upper()}")
    print(f"  Halted       : {halted_msg}")
    print(f"  Research     : every {runtime_cfg['research']['interval_minutes']} min")
    print(f"  Mark-to-mkt  : every {runtime_cfg['mark_to_market']['interval_minutes']} min")
    print(f"  Daily report : UTC {runtime_cfg['reporting']['daily_report_utc_hour']:02d}:00")
    print()

    # --diagnose: show filter results without calling Claude
    if args.diagnose:
        async def _diagnose():
            from src.kalshi_client.client import KalshiClient
            from src.research.helpers import in_universe, passes_liquidity_floors, passes_time_to_resolution

            orch = orchestrator
            include = orch.universe_cfg["categories"]["include"]
            exclude = orch.universe_cfg["categories"].get("exclude") or []
            min_hours = orch.universe_cfg["time_filters"]["min_hours_to_resolution"]
            rcfg = orch.risk_cfg

            print(f"\nFetching open markets from Kalshi ({args.env.upper()})...")
            print(f"  Universe patterns : {include}")
            print(f"  Min OI            : {rcfg.min_open_interest}")
            print(f"  Min volume        : {rcfg.min_daily_volume}")
            print(f"  Max spread        : {rcfg.max_spread_bps} bps")
            print(f"  Min hours left    : {min_hours}\n")

            async with KalshiClient() as client:
                markets = await client.get_markets(status="open", limit=200)

            print(f"Total open markets returned: {len(markets)}\n")
            print(f"{'Ticker':<30} {'OI':>6} {'Vol':>6}  Status")
            print("-" * 65)

            passed = []
            for m in markets:
                if not in_universe(m.ticker, include, exclude):
                    status = "SKIP  (not in universe)"
                else:
                    ok, reason = passes_time_to_resolution(m, min_hours)
                    if not ok:
                        status = f"SKIP  (time: {reason})"
                    else:
                        ok, reason = passes_liquidity_floors(
                            m, rcfg.min_open_interest, rcfg.min_daily_volume, rcfg.max_spread_bps
                        )
                        if not ok:
                            status = f"SKIP  (liquidity: {reason})"
                        else:
                            status = "PASS ✓"
                            passed.append(m.ticker)

                print(f"  {m.ticker:<28} {m.open_interest:>6} {m.volume:>6}  {status}")

            print(f"\n{len(passed)} market(s) would reach the Research Agent: {passed}")

        asyncio.run(_diagnose())
        return

    # --run-once: single cycle for testing
    if args.run_once:
        async def _once():
            print("Running one research cycle...")
            recs = await orchestrator.run_research_cycle()
            print(f"Done — {len(recs)} recommendation(s) passed QA.")
        asyncio.run(_once())
        return

    async def _run():
        scheduler = AsyncIOScheduler(timezone="UTC")

        # Research job
        research_minutes = runtime_cfg["research"]["interval_minutes"]
        scheduler.add_job(
            _research_job,
            trigger=IntervalTrigger(minutes=research_minutes),
            args=[orchestrator],
            id="research",
            name="Research Agent cycle",
            max_instances=1,
            coalesce=True,
        )

        # Mark-to-market job
        mtm_minutes = runtime_cfg["mark_to_market"]["interval_minutes"]
        scheduler.add_job(
            _mark_to_market_job,
            trigger=IntervalTrigger(minutes=mtm_minutes),
            args=[ledger],
            id="mark_to_market",
            name="Mark-to-market snapshot",
            max_instances=1,
        )

        # Daily report job
        report_hour = runtime_cfg["reporting"]["daily_report_utc_hour"]
        scheduler.add_job(
            _daily_report_job,
            trigger=CronTrigger(hour=report_hour, minute=0),
            args=[ledger, risk_cfg],
            id="daily_report",
            name="Daily performance report",
        )

        scheduler.start()
        print("Scheduler running. Press Ctrl+C to stop.")

        try:
            if args.dashboard:
                from src.dashboard import run_dashboard
                await run_dashboard(ledger, scheduler)
            else:
                # Keep event loop alive until Ctrl+C
                while True:
                    await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            scheduler.shutdown(wait=False)
            print("\nScheduler stopped.")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
