#!/usr/bin/env python
"""Headless runner — the same jobs as the web dashboard, without the UI.

Usage:
    python scheduler.py                  # run the schedule (research, order manager, daily report)
    python scheduler.py --run-once       # one research cycle, then exit
    python scheduler.py --sync-orders    # one order-manager pass, then exit
    python scheduler.py --diagnose       # show which markets would reach research (no LLM call)

KALSHI_ENV (demo|prod) selects the Kalshi environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi trading system scheduler")
    parser.add_argument("--env", choices=["demo", "prod"], default=os.getenv("KALSHI_ENV", "demo"))
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--sync-orders", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    os.environ["KALSHI_ENV"] = args.env

    # Imported after KALSHI_ENV is set: the client picks its base URL at import time
    from src.common.config import load_runtime_config
    from src.common.ledger import Ledger
    from src.jobs import Jobs
    from src.orchestrator import Orchestrator

    ledger = Ledger()
    printer = lambda kind, data: print(f"[{kind}] {json.dumps(data, default=str)[:300]}")
    jobs = Jobs(ledger, Orchestrator(ledger, on_event=printer))
    state = ledger.get_system_state()
    runtime = load_runtime_config()

    print(f"\n  Kalshi Trading System — {args.env.upper()} · mode {state.mode.value.upper()}"
          f"{' · HALTED: ' + (state.halt_reason or '') if ledger.is_halted() else ''}")
    print(f"  research every {runtime['research']['interval_minutes']} min · "
          f"orders every {runtime['order_management']['interval_minutes']} min\n")

    if args.diagnose:
        asyncio.run(_diagnose(jobs))
        return
    if args.run_once:
        run = asyncio.run(jobs.research(trigger="manual"))
        if run:
            print(f"\nRun {run.status}: {run.assessments} assessed, {run.recommendations} priced, "
                  f"{run.approved} approved, {run.submitted} submitted, ${run.cost_usd:.3f}")
        return
    if args.sync_orders:
        print(asyncio.run(jobs.order_manager()))
        return

    async def _forever() -> None:
        sched = jobs.build_scheduler()
        sched.start()
        await jobs.order_manager()
        print("Scheduler running. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            sched.shutdown(wait=False)

    try:
        asyncio.run(_forever())
    except KeyboardInterrupt:
        print("\nScheduler stopped.")


async def _diagnose(jobs) -> None:
    """Run candidate selection only and print each market's outcome."""
    from src.common.config import load_risk_config, load_runtime_config, load_universe_config
    from src.common.schemas import ResearchRun
    from src.execute.portfolio import build_portfolio_context
    from src.kalshi_client.client import KalshiClient

    run = ResearchRun(trigger="diagnose")
    research = load_runtime_config()["research"]
    async with KalshiClient() as client:
        ctx = await build_portfolio_context(jobs.ledger, client)
        events = await jobs.orchestrator._select(
            client, run, ctx, load_universe_config(), load_risk_config(), research,
            int(research["max_candidates"]))
    events = jobs.orchestrator._take(events, run, int(research["max_candidates"]))
    for c in run.candidates:
        print(f"  {c['stage']:<13} {c['ticker']:<40} {c['reason']}")
    print(f"\n{sum(len(e.markets) for e in events)} market(s) in {len(events)} event(s) would be researched.")


if __name__ == "__main__":
    main()
