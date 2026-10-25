"""Research → Pricing → QA → Execute pipeline for one cycle.

  1. Select candidates: in-universe markets that pass time/liquidity filters,
     aren't already held, and weren't assessed recently. Ranked, capped per
     series and per event, grouped by event.
  2. Research: the model records a fair-YES probability per market.
  3. Price: re-read each orderbook and quote a side/price with edge after fees.
  4. QA: size to fit every risk cap, run hard gates.
  5. Act by mode: paper → record only; hitl → queue for approval;
     autonomous → submit a post-only order.

Every market's path through these stages is recorded on the ResearchRun.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import timedelta
from typing import Any, Callable

from src.common.config import (
    ConfigError,
    load_execution_config,
    load_risk_config,
    load_runtime_config,
    load_universe_config,
)
from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import (
    Assessment,
    DecisionVerdict,
    KalshiEnv,
    OperationalMode,
    Order,
    OrderStatus,
    Recommendation,
    ResearchRun,
    utcnow,
)
from src.execute.hitl import notify_pending_approval
from src.execute.orders import PreflightError, place_order
from src.execute.portfolio import build_portfolio_context, event_of
from src.kalshi_client.client import KalshiClient
from src.kalshi_client.models import Market
from src.qa.pricing import FeeModel, NoQuote, Quote, quote
from src.qa.rules import PortfolioContext, category_of, evaluate
from src.research.agent import EventBrief, ResearchAgent, naive_utc
from src.research.costs import UsageMeter
from src.research.helpers import in_universe, passes_liquidity_floors, passes_time_to_resolution

log = get_logger("orchestrator")

MAX_MARKETS_PER_EVENT = 4

EventCallback = Callable[[str, dict], None]


class ResearchWindow:
    """Hours-to-close window for event-triggered research, with per-series overrides."""

    def __init__(self, hours: tuple[float, float], overrides: dict[str, Any] | None = None) -> None:
        self.hours = (float(hours[0]), float(hours[1]))
        self.overrides = {k: (float(v[0]), float(v[1])) for k, v in (overrides or {}).items()}

    @classmethod
    def from_config(cls, trigger_cfg: dict) -> "ResearchWindow":
        return cls(tuple(trigger_cfg.get("window_hours", [2, 12])), trigger_cfg.get("window_overrides"))

    def bounds(self, series: str) -> tuple[float, float]:
        return self.overrides.get(series, self.hours)

    def contains(self, m: Market) -> bool:
        if m.close_time is None:
            return False
        hours = (naive_utc(m.close_time) - utcnow()).total_seconds() / 3600
        lo, hi = self.bounds(m.series_ticker)
        return lo <= hours <= hi

    def describe(self) -> str:
        extra = f" ({len(self.overrides)} series overridden)" if self.overrides else ""
        return f"{self.hours[0]:g}–{self.hours[1]:g}h{extra}"


class Orchestrator:
    def __init__(self, ledger: Ledger | None = None, on_event: EventCallback | None = None) -> None:
        self.ledger = ledger or Ledger()
        self.on_event = on_event or (lambda kind, data: None)
        self._fee_cache: dict[str, FeeModel] = {}
        self._window: ResearchWindow | None = None

    async def count_window_markets(self, window: ResearchWindow) -> int:
        """Eligible, not-recently-assessed markets inside ``window`` (no LLM, no run record)."""
        risk_cfg, universe = load_risk_config(), load_universe_config()
        research_cfg = load_runtime_config()["research"]
        scratch = ResearchRun(trigger="window-check")
        self._window = window
        try:
            async with KalshiClient() as client:
                ctx = await build_portfolio_context(self.ledger, client)
                events = await self._select(client, scratch, ctx, universe, risk_cfg, research_cfg,
                                            pool_limit=10_000, probe=True)
        finally:
            self._window = None
        return sum(len(ev.markets) for ev in events)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run_research_cycle(self, trigger: str = "manual",
                                 window: "ResearchWindow | None" = None) -> ResearchRun:
        """Run one cycle. ``window`` restricts candidates to markets closing within it."""
        state = self.ledger.get_system_state()
        run = ResearchRun(trigger=trigger, mode=state.mode.value)
        if window:
            run.notes.append(f"Event trigger: markets closing in {window.describe()}")
        self.ledger.save_run(run)
        self.on_event("run_started", {"run_id": run.run_id, "trigger": trigger})
        self._window = window
        try:
            await self._run(run, state.mode)
            if run.status == "running":
                run.status = "ok"
        except ConfigError as e:
            run.status, run.error = "error", f"Config: {e}"
        except Exception as e:
            log.error("research_cycle_failed", run_id=run.run_id, error=str(e))
            run.status, run.error = "error", str(e)[:1000]
        finally:
            self._window = None
            run.finished_at = utcnow()
            self.ledger.save_run(run)
            self.on_event("run_finished", {
                "run_id": run.run_id, "status": run.status, "error": run.error,
                "assessments": run.assessments, "recommendations": run.recommendations,
                "approved": run.approved, "submitted": run.submitted,
                "cost_usd": round(run.cost_usd, 4),
            })
        return run

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _run(self, run: ResearchRun, mode: OperationalMode) -> None:
        if self.ledger.is_halted():
            run.status, run.notes = "skipped", ["System is halted"]
            return

        risk_cfg = load_risk_config()
        exec_cfg = load_execution_config()
        universe = load_universe_config()
        runtime = load_runtime_config()
        research_cfg = runtime["research"]
        budget = runtime.get("budget") or {}

        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = self.ledger.api_spend_since(month_start)
        monthly_cap = float(budget.get("monthly_usd", 10.0))
        if spent >= monthly_cap:
            run.status = "skipped"
            run.notes.append(f"Monthly API budget used: ${spent:.2f} of ${monthly_cap:.2f}")
            return

        model = runtime["models"]["research"]
        run.model = model
        meter = UsageMeter(runtime.get("model_pricing"))

        local_cfg = runtime.get("local_llm") or {}
        assist = await self._local_assist(runtime, run) if local_cfg.get("enabled") else None
        limit = int(research_cfg["max_candidates"])
        pool = limit * int(local_cfg.get("pool_multiplier", 2)) \
            if assist and local_cfg.get("evidence_prepass") else limit

        async with KalshiClient() as client:
            ctx = await build_portfolio_context(self.ledger, client)
            events = await self._select(client, run, ctx, universe, risk_cfg, research_cfg, pool)
            self.ledger.save_run(run)
            if events and assist and local_cfg.get("evidence_prepass"):
                events = await self._prepass(assist, events, run, limit)
            else:
                events = self._take(events, run, limit)
            self.ledger.save_run(run)
            if not events:
                run.notes.append("No candidates passed the filters")
                self._add_local_usage(meter, assist, run)
                return

            per_run_cap = min(float(budget.get("per_run_usd", 0.40)), monthly_cap - spent)
            focus = "\n".join(f"- {m.title}" for ev in events for m in ev.markets)
            agent = ResearchAgent(
                model=model, meter=meter,
                max_iterations=int(research_cfg["max_tool_iterations"]),
                max_cost_usd=per_run_cap,
                effort=research_cfg.get("effort", "medium"),
                fetch_hook=assist.page_hook(focus) if assist and local_cfg.get("summarize_pages") else None,
                refusal_fallbacks=bool(research_cfg.get("refusal_fallbacks", True)),
            )
            try:
                assessments, skips = await agent.run(
                    events, run.run_id, show_prices=bool(research_cfg.get("show_market_prices"))
                )
            finally:
                self._add_local_usage(meter, assist, run)
                run.notes.extend(agent.notes)
            markets = {m.ticker: m for ev in events for m in ev.markets}
            self._record_assessments(run, assessments, skips, markets)

            recs = await self._price(client, run, assessments, markets, exec_cfg)
            recs.sort(key=lambda r: r.edge_cents_after_fees or 0, reverse=True)
            cap = int(research_cfg["max_recommendations_per_run"])
            for extra in recs[cap:]:
                self._stage(run, extra.market_ticker, "not_traded", f"beyond the {cap}-per-run cap")
            for rec in recs[:cap]:
                await self._qa_and_act(client, run, rec, markets[rec.market_ticker],
                                       ctx, risk_cfg, exec_cfg, mode)

    # ------------------------------------------------------------------
    # 1. Candidate selection
    # ------------------------------------------------------------------

    async def _select(self, client: KalshiClient, run: ResearchRun, ctx: PortfolioContext,
                      universe: dict, risk_cfg: Any, research_cfg: dict,
                      pool_limit: int, probe: bool = False) -> list[EventBrief]:
        """Filter and rank markets; return up to ``pool_limit`` of them grouped by event,
        best-ranked event first. ``probe`` skips orderbooks and feed events."""
        include = universe["categories"]["include"]
        exclude = universe["categories"].get("exclude") or []
        tf = universe.get("time_filters") or {}
        min_hours = float(tf.get("min_hours_to_resolution", 1))
        max_days = tf.get("max_days_to_resolution")
        recent = self.ledger.recently_assessed_tickers(float(research_cfg["reassess_after_hours"]))

        markets: list[Market] = []
        for series in include:
            try:
                markets.extend(await client.get_markets(series_ticker=series, status="open", limit=200))
            except Exception as e:
                run.notes.append(f"Couldn't fetch series {series}: {e}")

        eligible: list[Market] = []
        for m in markets:
            reason = None
            if not in_universe(m.ticker, include, exclude):
                reason = "excluded by universe"
            elif not (ok := passes_time_to_resolution(m, min_hours))[0]:
                reason = ok[1]
            elif max_days is not None and m.close_time and \
                    naive_utc(m.close_time) > utcnow() + timedelta(days=float(max_days)):
                reason = f"resolves more than {max_days} days out"
            elif not (ok := passes_liquidity_floors(m, risk_cfg.min_open_interest,
                                                     risk_cfg.min_daily_volume, 10**9))[0]:
                reason = ok[1]
            elif m.ticker in ctx.held_tickers:
                reason = "already holding or bidding"
            elif m.ticker in recent:
                reason = f"assessed in the last {research_cfg['reassess_after_hours']}h"
            elif self._window is not None and not self._window.contains(m):
                lo, hi = self._window.bounds(m.series_ticker)
                reason = f"outside the {lo:g}–{hi:g}h research window"
            run.candidates.append(self._entry(m, "filtered" if reason else "eligible", reason or ""))
            if not reason:
                eligible.append(m)

        def score(m: Market) -> float:
            mid = (m.yes_bid + m.yes_ask) / 2 if m.yes_ask else 50
            return (m.volume_24h or m.volume) * (1 + abs(mid - 50) / 50)

        per_series_cap = max(2, -(-pool_limit // max(len(include), 1)))
        per_series: dict[str, int] = defaultdict(int)
        per_event: dict[str, int] = defaultdict(int)
        chosen: list[Market] = []
        for m in sorted(eligible, key=score, reverse=True):
            series, event = m.series_ticker, m.event_ticker or event_of(m.ticker)
            if (len(chosen) < pool_limit and per_series[series] < per_series_cap
                    and per_event[event] < MAX_MARKETS_PER_EVENT):
                chosen.append(m)
                per_series[series] += 1
                per_event[event] += 1
                self._stage(run, m.ticker, "shortlisted", "")
            else:
                self._stage(run, m.ticker, "not_selected", "ranked below the candidate cap")

        by_event: dict[str, list[Market]] = defaultdict(list)   # insertion order = rank order
        for m in chosen:
            by_event[m.event_ticker or event_of(m.ticker)].append(m)
        events = [EventBrief(event_ticker=e, markets=ms) for e, ms in by_event.items()]

        if probe:
            return events
        if research_cfg.get("show_market_prices"):
            for ev in events:
                for m in ev.markets:
                    try:
                        ev.orderbooks[m.ticker] = await client.get_orderbook(m.ticker)
                    except Exception:
                        pass

        self.on_event("candidates_selected", {
            "run_id": run.run_id, "fetched": len(markets), "eligible": len(eligible),
            "selected": len(chosen), "events": len(events),
        })
        return events

    # ------------------------------------------------------------------
    # 1b. Final pick (optionally after the local evidence pre-pass)
    # ------------------------------------------------------------------

    def _take(self, events: list[EventBrief], run: ResearchRun, limit: int) -> list[EventBrief]:
        """Keep whole events in order until ``limit`` markets are chosen."""
        out: list[EventBrief] = []
        count = 0
        for ev in events:
            if count + len(ev.markets) > limit and out:
                for m in ev.markets:
                    self._stage(run, m.ticker, "not_selected", "ranked below the candidate cap")
                continue
            out.append(ev)
            count += len(ev.markets)
            for m in ev.markets:
                self._stage(run, m.ticker, "selected",
                            f"local priority {ev.priority}" if ev.priority else "")
        return out

    async def _local_assist(self, runtime: dict, run: ResearchRun):
        from src.llm.ollama import OllamaClient
        from src.research.local_assist import LocalAssist

        cfg = runtime.get("local_llm") or {}
        model = runtime["models"].get("local", "qwen3.5:4b")
        assist = LocalAssist(OllamaClient(model, host=cfg.get("host"),
                                          timeout=float(cfg.get("timeout_seconds", 180)),
                                          num_ctx=int(cfg.get("num_ctx", 16384))))
        if await assist.start():
            run.notes.append(f"Local model {model} assisting")
            return assist
        run.notes.append(f"Local model unavailable — Claude-only run ({assist.detail})")
        return None

    async def _prepass(self, assist: Any, events: list[EventBrief], run: ResearchRun,
                       limit: int) -> list[EventBrief]:
        """Gather evidence for each shortlisted event locally, then keep the most promising."""
        today = utcnow().strftime("%Y-%m-%d")
        for ev in events:   # sequential: one GPU, and it keeps the model warm
            evidence = await assist.gather(ev, today)
            if evidence is None:
                continue
            ev.evidence, ev.priority = evidence.as_text(), evidence.priority
            for m in ev.markets:
                self._stage(run, m.ticker, "shortlisted", evidence.reason,
                            local_priority=evidence.priority, local_queries=evidence.queries)
            self.on_event("evidence", {"run_id": run.run_id, "event": ev.event_ticker,
                                       "priority": evidence.priority, "facts": len(evidence.facts)})
        dropped = [ev for ev in events if ev.priority == 1]
        for ev in dropped:
            for m in ev.markets:
                self._stage(run, m.ticker, "triaged_out", "local pre-pass found nothing useful")
        keep = [ev for ev in events if ev.priority != 1]
        # Stable sort: priority first, original rank second (unknown priority ranks as 3)
        keep.sort(key=lambda ev: -(ev.priority or 3))
        return self._take(keep, run, limit)

    @staticmethod
    def _add_local_usage(meter: UsageMeter, assist: Any, run: ResearchRun) -> None:
        if assist is not None and assist.client.calls:
            meter.add_local(assist.client.model, assist.client.prompt_tokens,
                            assist.client.output_tokens)
        run.usage, run.cost_usd = meter.as_dict(), meter.total_cost

    @staticmethod
    def _entry(m: Market, stage: str, reason: str) -> dict:
        return {"ticker": m.ticker, "title": m.title, "event": m.event_ticker,
                "series": m.series_ticker, "stage": stage, "reason": reason}

    @staticmethod
    def _stage(run: ResearchRun, ticker: str, stage: str, reason: str, **extra: Any) -> None:
        for c in run.candidates:
            if c["ticker"] == ticker:
                c.update(stage=stage, reason=reason, **extra)
                return

    # ------------------------------------------------------------------
    # 2. Record assessments
    # ------------------------------------------------------------------

    def _record_assessments(self, run: ResearchRun, assessments: list[Assessment],
                            skips: dict[str, str], markets: dict[str, Market]) -> None:
        for a in assessments:
            self.ledger.save_assessment(a)
            self._stage(run, a.market_ticker, "assessed", "", fair_yes_prob=a.fair_yes_prob,
                        confidence=a.confidence.value)
            self.on_event("assessment", {
                "run_id": run.run_id, "ticker": a.market_ticker, "title": a.title,
                "fair_yes_prob": a.fair_yes_prob, "confidence": a.confidence.value,
                "thesis": a.thesis[:240],
            })
        for ticker, reason in skips.items():
            self._stage(run, ticker, "skipped", reason)
            if reason.startswith("not assessed"):
                continue  # ran out of turns/budget — eligible again next run
            m = markets[ticker]
            self.ledger.save_assessment(Assessment(
                run_id=run.run_id, market_ticker=ticker, event_ticker=m.event_ticker,
                series_ticker=m.series_ticker, title=m.title, skip_reason=reason,
                model=run.model, market_close_time=naive_utc(m.close_time),
            ))
        run.assessments = len(assessments)

    # ------------------------------------------------------------------
    # 3. Pricing
    # ------------------------------------------------------------------

    async def _fee_model(self, client: KalshiClient, series: str) -> FeeModel:
        if series not in self._fee_cache:
            try:
                s = await client.get_series(series)
                self._fee_cache[series] = FeeModel(s.fee_type, s.fee_multiplier)
            except Exception:
                self._fee_cache[series] = FeeModel()   # assume standard quadratic fees
        return self._fee_cache[series]

    async def _price(self, client: KalshiClient, run: ResearchRun, assessments: list[Assessment],
                     markets: dict[str, Market], exec_cfg: Any) -> list[Recommendation]:
        recs: list[Recommendation] = []
        for a in assessments:
            m = markets[a.market_ticker]
            try:
                ob = await client.get_orderbook(a.market_ticker)
            except Exception as e:
                self._stage(run, a.market_ticker, "no_edge", f"orderbook unavailable: {e}")
                continue
            a.yes_bid, a.yes_ask = ob.best_yes_bid(), ob.best_yes_ask()
            self.ledger.save_assessment(a)
            fees = await self._fee_model(client, m.series_ticker)
            q = quote(a.fair_yes_prob, ob, exec_cfg.min_edge_cents, fees,
                      style=exec_cfg.entry_style, max_spread_cents=exec_cfg.max_spread_cents)
            if isinstance(q, NoQuote):
                self._stage(run, a.market_ticker, "no_edge", q.detail or q.reason,
                            yes_bid=a.yes_bid, yes_ask=a.yes_ask)
                continue
            rec = self._recommendation(a, q, m, run)
            self.ledger.save_recommendation(rec)
            recs.append(rec)
            self._stage(run, a.market_ticker, "priced",
                        f"{q.side.value} @ {q.price_cents}¢ (max {q.max_price_cents}¢), "
                        f"edge {q.edge_cents:.1f}¢", rec_id=rec.rec_id)
        run.recommendations = len(recs)
        return recs

    @staticmethod
    def _recommendation(a: Assessment, q: Quote, m: Market, run: ResearchRun) -> Recommendation:
        mid = None
        if q.bid_cents is not None and q.ask_cents is not None:
            mid = (q.bid_cents + q.ask_cents) / 200.0
        return Recommendation(
            assessment_id=a.assessment_id,
            run_id=run.run_id,
            market_ticker=m.ticker,
            event_ticker=m.event_ticker or event_of(m.ticker),
            title=m.title,
            side=q.side,
            limit_price_cents=q.price_cents,
            max_price_cents=q.max_price_cents,
            target_size_contracts=0,
            estimated_fair_prob=q.fair_prob,
            fair_yes_prob=a.fair_yes_prob,
            market_implied_prob=mid if mid is not None else q.price_cents / 100.0,
            edge_bps=int(round(q.edge_cents * 100)),
            edge_cents_after_fees=round(q.edge_cents, 2),
            fee_cents_per_contract=round(q.fee_cents, 3),
            confidence=a.confidence,
            thesis=a.thesis,
            sources=a.sources,
            market_close_time=naive_utc(m.close_time),
        )

    # ------------------------------------------------------------------
    # 4-5. QA and act
    # ------------------------------------------------------------------

    async def _qa_and_act(self, client: KalshiClient, run: ResearchRun, rec: Recommendation,
                          market: Market, ctx: PortfolioContext, risk_cfg: Any, exec_cfg: Any,
                          mode: OperationalMode) -> None:
        decision = evaluate(
            rec, risk_cfg, ctx,
            {"open_interest": market.open_interest, "volume": market.volume},
            order_ttl_hours=exec_cfg.order_ttl_hours,
            cancel_before_close_minutes=exec_cfg.cancel_before_close_minutes,
        )
        self.ledger.save_decision(decision)
        self.on_event("decision", {
            "run_id": run.run_id, "rec_id": rec.rec_id, "ticker": rec.market_ticker,
            "side": rec.side.value, "price": rec.limit_price_cents,
            "size": decision.approved_order.size_contracts if decision.approved_order else 0,
            "edge_cents": rec.edge_cents_after_fees, "verdict": decision.decision.value,
            "notes": decision.qa_notes, "thesis": rec.thesis[:240],
        })
        if decision.decision == DecisionVerdict.DENY:
            self._stage(run, rec.market_ticker, "denied", decision.qa_notes)
            return

        run.approved += 1
        ao = decision.approved_order
        assert ao is not None
        self._reserve(ctx, rec, ao.size_contracts)

        if mode == OperationalMode.AUTONOMOUS:
            try:
                fees = await self._fee_model(client, market.series_ticker)
                order = await place_order(ao, rec.rec_id, client, self.ledger, fees=fees,
                                          market_close_time=rec.market_close_time)
                run.submitted += 1
                self._stage(run, rec.market_ticker, "submitted",
                            f"{order.side.value} {order.size_contracts} @ {order.limit_price_cents}¢",
                            order_id=order.order_id)
                self.on_event("order_submitted", {"rec_id": rec.rec_id, "ticker": rec.market_ticker,
                                                  "price": order.limit_price_cents,
                                                  "size": order.size_contracts,
                                                  "kalshi_order_id": order.kalshi_order_id})
            except PreflightError as e:
                self._stage(run, rec.market_ticker, "preflight_blocked", str(e))
            except Exception as e:
                self._stage(run, rec.market_ticker, "order_failed", str(e)[:300])
            return

        order = Order(
            client_order_id=ao.client_order_id,
            rec_id=rec.rec_id,
            market_ticker=rec.market_ticker,
            side=rec.side,
            limit_price_cents=ao.limit_price_cents,
            max_price_cents=ao.max_price_cents,
            size_contracts=ao.size_contracts,
            expires_at=ao.expiry_ts,
            market_close_time=rec.market_close_time,
            env=KalshiEnv(os.getenv("KALSHI_ENV", "demo").lower()),
            status=(OrderStatus.PENDING_APPROVAL if mode == OperationalMode.HITL
                    else OrderStatus.APPROVED),
            status_reason="" if mode == OperationalMode.HITL else "paper mode — not sent",
        )
        self.ledger.save_order(order)
        if mode == OperationalMode.HITL:
            self._stage(run, rec.market_ticker, "awaiting_approval",
                        f"{order.side.value} {order.size_contracts} @ {order.limit_price_cents}¢")
            notify_pending_approval(order, decision)
            self.on_event("pending_approval", {"rec_id": rec.rec_id, "ticker": rec.market_ticker})
        else:
            self._stage(run, rec.market_ticker, "paper_order",
                        f"{order.side.value} {order.size_contracts} @ {order.limit_price_cents}¢")

    @staticmethod
    def _reserve(ctx: PortfolioContext, rec: Recommendation, size: int) -> None:
        """Count an approved order against the caps for the rest of this cycle."""
        cost = size * rec.limit_price_cents
        ctx.current_deployed_cents += cost
        ctx.open_exposure_cents += cost
        ev, cat = rec.event_ticker, category_of(rec.market_ticker)
        ctx.positions_by_event[ev] = ctx.positions_by_event.get(ev, 0) + cost
        ctx.positions_by_category[cat] = ctx.positions_by_category.get(cat, 0) + cost
        ctx.positions_count_by_event[ev] = ctx.positions_count_by_event.get(ev, 0) + 1
        ctx.held_tickers.add(rec.market_ticker)
        ctx.today_new_trades += 1
        if ctx.available_cash_cents is not None:
            ctx.available_cash_cents -= cost
