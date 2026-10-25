"""Scheduled jobs shared by the web app and the headless scheduler.

  research       every runtime.research.interval_minutes (full sweep)
  watch          every research.event_trigger.check_every_minutes: if unassessed
                 markets have entered their research window, run research on them
  order_manager  every runtime.order_management.interval_minutes (fills, repricing,
                 exits, settlements)
  daily_report   once a day at runtime.reporting.daily_report_utc_hour
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.common.config import load_risk_config, load_runtime_config
from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import utcnow
from src.execute.manager import OrderManager
from src.orchestrator import Orchestrator, ResearchWindow

log = get_logger("scheduler")

StatusCallback = Callable[[str, str], None]


class Jobs:
    """Owns the orchestrator and order manager, and guards against overlapping runs."""

    def __init__(self, ledger: Ledger, orchestrator: Orchestrator,
                 on_status: StatusCallback | None = None) -> None:
        self.ledger = ledger
        self.orchestrator = orchestrator
        self.manager = OrderManager(ledger, notify=orchestrator.on_event)
        self.on_status = on_status or (lambda job, status: None)
        self._research_lock = asyncio.Lock()
        self._manager_lock = asyncio.Lock()
        self._last_event_run = None

    @property
    def research_running(self) -> bool:
        return self._research_lock.locked()

    async def research(self, trigger: str = "schedule", window: ResearchWindow | None = None):
        if self._research_lock.locked():
            return None
        async with self._research_lock:
            self.on_status("research", "running")
            try:
                run = await self.orchestrator.run_research_cycle(trigger=trigger, window=window)
                self.on_status("research", "error" if run.status == "error" else "idle")
                return run
            except Exception as e:
                log.error("research_job_failed", error=str(e))
                self.on_status("research", "error")
                return None

    async def watch(self) -> int | None:
        """Trigger research when new markets enter their research window. Returns the count found."""
        rt = load_runtime_config()
        cfg = rt["research"]["event_trigger"]
        if not cfg.get("enabled") or self.research_running or self.ledger.is_halted():
            return None
        if self._last_event_run and utcnow() - self._last_event_run < timedelta(
                minutes=float(cfg.get("min_gap_minutes", 60))):
            return None
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.ledger.api_spend_since(month_start) >= float((rt.get("budget") or {}).get("monthly_usd", 10)):
            return None
        window = ResearchWindow.from_config(cfg)
        try:
            n = await self.orchestrator.count_window_markets(window)
        except Exception as e:
            log.warning("watch_failed", error=str(e))
            return None
        if n >= int(cfg.get("min_new_markets", 1)):
            log.info("event_trigger", markets=n, window=window.describe())
            self._last_event_run = utcnow()
            await self.research(trigger="event", window=window)
        return n

    async def order_manager(self) -> dict | None:
        if self._manager_lock.locked():
            return None
        async with self._manager_lock:
            self.on_status("orders", "running")
            try:
                summary = await self.manager.run_once()
                self.on_status("orders", "idle")
                return summary
            except Exception as e:
                log.error("order_manager_failed", error=str(e))
                self.on_status("orders", "error")
                return None

    async def daily_report(self) -> None:
        from src.performance.reporting import generate_daily_report
        from src.performance.tracker import current_metrics
        try:
            path = generate_daily_report(current_metrics(self.ledger),
                                         load_risk_config().bankroll_cents)
            log.info("daily_report_written", path=str(path))
        except Exception as e:
            log.error("daily_report_failed", error=str(e))

    def build_scheduler(self) -> AsyncIOScheduler:
        cfg = load_runtime_config()
        sched = AsyncIOScheduler(timezone="UTC")
        sched.add_job(self.research, IntervalTrigger(minutes=cfg["research"]["interval_minutes"]),
                      id="research", max_instances=1, coalesce=True)
        trig = cfg["research"]["event_trigger"]
        if trig.get("enabled"):
            sched.add_job(self.watch, IntervalTrigger(minutes=float(trig.get("check_every_minutes", 30))),
                          id="watch", max_instances=1, coalesce=True)
        sched.add_job(self.order_manager,
                      IntervalTrigger(minutes=cfg["order_management"]["interval_minutes"]),
                      id="order_manager", max_instances=1, coalesce=True)
        sched.add_job(self.daily_report,
                      CronTrigger(hour=cfg["reporting"]["daily_report_utc_hour"], minute=0),
                      id="daily_report", max_instances=1, coalesce=True)
        return sched
