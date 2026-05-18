"""Pipeline orchestrator: Research → QA → Execute.

Responsibilities:
  - Load all YAML configs and build runtime objects
  - Call Research Agent (claude-opus-4-7 with tool use) to generate recommendations
  - Run the deterministic QA/risk rule engine on each recommendation
  - Persist artifacts to the ledger
  - In HITL mode: create PENDING_APPROVAL orders and fire desktop notifications
  - In paper mode: auto-approve orders without submitting to Kalshi
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import (
    Decision,
    DecisionVerdict,
    KalshiEnv,
    OperationalMode,
    Order,
    OrderStatus,
    Recommendation,
)
from src.execute.hitl import notify_pending_approval
from src.kalshi_client.client import KalshiClient
from src.kalshi_client.models import Market, Orderbook
from src.qa.rules import PortfolioContext, RiskConfig, evaluate
from src.research.helpers import (
    in_universe,
    passes_liquidity_floors,
    passes_time_to_resolution,
)

log = get_logger("orchestrator")

CONFIG_DIR = Path(__file__).parents[1] / "config"
AGENT_SPEC_PATH = Path(__file__).parents[1] / ".claude" / "agents" / "research.md"


# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------


def load_risk_config() -> RiskConfig:
    cfg = yaml.safe_load((CONFIG_DIR / "risk.yaml").read_text())
    b = cfg["bankroll"]
    pt = cfg["per_trade"]
    pd = cfg["per_day"]
    conc = cfg["concentration"]
    liq = cfg["liquidity_floors"]
    rq = cfg.get("research_quality", {})
    return RiskConfig(
        bankroll_cents=int(b["total_capital_usd"] * 100),
        max_total_deployed_cents=int(b["max_total_deployed_usd"] * 100),
        max_loss_cents=int(pt["max_loss_usd"] * 100),
        max_position_pct=pt["max_position_pct_of_bankroll"],
        max_size_contracts=pt["max_size_contracts"],
        max_drawdown_cents=int(pd["max_drawdown_usd"] * 100),
        max_new_trades_per_day=pd["max_new_trades"],
        max_open_exposure_cents=int(pd["max_open_exposure_usd"] * 100),
        max_pct_per_event=conc["max_pct_per_event"],
        max_pct_per_category=conc["max_pct_per_category"],
        max_correlated_positions=conc["max_correlated_positions"],
        min_open_interest=liq["min_open_interest"],
        min_daily_volume=liq["min_daily_volume_contracts"],
        max_spread_bps=liq["max_spread_bps"],
        max_price_staleness_bps=cfg.get("preflight", {}).get("max_slippage_bps", 100),
        max_brier_score=rq.get("max_brier_score_for_category"),
        min_trades_for_brier_gate=rq.get("min_trades_before_brier_gate", 20),
    )


def load_universe_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "universe.yaml").read_text())


def load_runtime_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "runtime.yaml").read_text())


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------


def _build_system_prompt(universe_cfg: dict, risk_cfg: RiskConfig) -> str:
    """Embed live config into the Research Agent system prompt."""
    base = AGENT_SPEC_PATH.read_text(encoding="utf-8") if AGENT_SPEC_PATH.exists() else ""
    # Strip YAML frontmatter (--- ... ---)
    base = re.sub(r"^---.*?---\n", "", base, flags=re.DOTALL).strip()

    context = f"""

## Runtime Configuration (injected by orchestrator)

- **Universe categories**: {universe_cfg["categories"]["include"]}
- **Min edge threshold**: {universe_cfg["min_edge_bps"]} bps
- **Min open interest**: {risk_cfg.min_open_interest} contracts
- **Min daily volume**: {risk_cfg.min_daily_volume} contracts
- **Max spread**: {risk_cfg.max_spread_bps} bps
- **Min hours to resolution**: {universe_cfg["time_filters"]["min_hours_to_resolution"]}
- **Bankroll**: ${risk_cfg.bankroll_cents / 100:.2f}
- **Max position pct**: {risk_cfg.max_position_pct * 100:.0f}% of bankroll
- **Max size**: {risk_cfg.max_size_contracts} contracts
- **Max loss per trade**: ${risk_cfg.max_loss_cents / 100:.2f}
"""
    return base + context


# ---------------------------------------------------------------------------
# Portfolio context builder
# ---------------------------------------------------------------------------


def _build_portfolio_context(ledger: Ledger) -> PortfolioContext:
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    open_orders = ledger.get_orders_by_status(
        "SUBMITTED", "PARTIALLY_FILLED", "APPROVED", "PENDING_APPROVAL"
    )
    deployed = sum(o.limit_price_cents * o.size_contracts for o in open_orders)
    today_new = sum(
        1 for o in open_orders
        if o.submitted_at and o.submitted_at.strftime("%Y-%m-%d") == today_str
    )

    # Build per-event and per-category exposure maps
    positions_by_event: dict[str, int] = {}
    positions_by_category: dict[str, int] = {}
    for o in open_orders:
        exposure = o.limit_price_cents * o.size_contracts
        event = o.market_ticker.rsplit("-", 1)[0] if "-" in o.market_ticker else o.market_ticker
        category = o.market_ticker.split("-")[0].upper()
        positions_by_event[event] = positions_by_event.get(event, 0) + exposure
        positions_by_category[category] = positions_by_category.get(category, 0) + exposure

    return PortfolioContext(
        current_deployed_cents=deployed,
        daily_pnl_cents=0,
        today_new_trades=today_new,
        open_exposure_cents=deployed,
        positions_by_event=positions_by_event,
        positions_by_category=positions_by_category,
        correlated_count=0,
    )


# ---------------------------------------------------------------------------
# Tool implementations for the Research Agent
# ---------------------------------------------------------------------------


async def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Uses Tavily API if TAVILY_API_KEY is set (recommended),
    otherwise falls back to DuckDuckGo HTML scraping."""
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        return await _tavily_search(query, max_results, tavily_key)
    return await _ddg_search(query, max_results)


async def _tavily_search(query: str, max_results: int, api_key: str) -> str:
    """Tavily AI search — handles Cloudflare, JS-rendered pages, paywalls."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            lines = []
            if data.get("answer"):
                lines.append(f"Summary: {data['answer']}\n")
            for r in data.get("results", [])[:max_results]:
                lines.append(f"• {r.get('title', '')}: {r.get('content', '')[:300]}")
                lines.append(f"  URL: {r.get('url', '')}")
            return "\n".join(lines) if lines else "No results found."
    except Exception as e:
        return f"Tavily search error: {e}"


async def _ddg_search(query: str, max_results: int) -> str:
    """DuckDuckGo HTML fallback — may be blocked by anti-bot measures."""
    try:
        url = "https://html.duckduckgo.com/html/"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                url,
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
            )
            text = resp.text
            results = re.findall(
                r'class="result__title"[^>]*>.*?<a[^>]*>(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</span>',
                text,
                re.DOTALL,
            )
            if not results:
                return "Search returned no results (may be blocked). Set TAVILY_API_KEY for reliable search."
            lines = []
            for title, snippet in results[:max_results]:
                title = re.sub(r"<[^>]+>", "", title).strip()
                snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                lines.append(f"• {title}: {snippet}")
            return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


async def _fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch URL and return plain text (HTML tags stripped)."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                url, headers={"User-Agent": "Mozilla/5.0 (Kalshi research bot)"}
            )
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except Exception as e:
        return f"Fetch error: {e}"


async def _execute_tool(name: str, tool_input: dict) -> str:
    if name == "web_search":
        return await _web_search(
            tool_input["query"], tool_input.get("max_results", 5)
        )
    if name == "fetch_url":
        return await _fetch_url(
            tool_input["url"], tool_input.get("max_chars", 8000)
        )
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Recommendation extractor
# ---------------------------------------------------------------------------


def _extract_recommendations(text: str) -> list[Recommendation]:
    """Parse ```json ... ``` blocks from Claude's response into Recommendation objects."""
    recs: list[Recommendation] = []
    for match in re.finditer(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            data = json.loads(match.group(1))
            rec = Recommendation.model_validate(data)
            recs.append(rec)
            log.info("rec_parsed", rec_id=rec.rec_id, market=rec.market_ticker)
        except Exception as e:
            log.warning("rec_parse_failed", error=str(e), snippet=match.group(1)[:120])
    return recs


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Coordinates the Research → QA → Execute pipeline for one cycle."""

    def __init__(self) -> None:
        self.ledger = Ledger()
        self.risk_cfg = load_risk_config()
        self.universe_cfg = load_universe_config()
        self.runtime_cfg = load_runtime_config()

    async def run_research_cycle(self) -> list[Recommendation]:
        """Full Research → QA cycle. Returns recommendations that passed QA."""
        if self.ledger.is_halted():
            log.warning("research_skipped_halted")
            return []

        # Reload configs each cycle so edits to YAML take effect without restart
        self.risk_cfg = load_risk_config()
        self.universe_cfg = load_universe_config()
        self.runtime_cfg = load_runtime_config()

        state = self.ledger.get_system_state()
        log.info("research_cycle_start", mode=state.mode.value)

        candidates, orderbooks = await self._fetch_candidates()
        if not candidates:
            log.info("no_candidates_after_filter")
            return []

        raw_recs = await self._run_research_agent(candidates, orderbooks)
        log.info("research_agent_output", raw_count=len(raw_recs))

        approved = await self._run_qa(raw_recs, candidates, orderbooks, state)
        log.info("research_cycle_complete",
                 candidates=len(candidates), raw=len(raw_recs), approved=len(approved))
        return approved

    # ------------------------------------------------------------------
    # Internal: candidate fetching
    # ------------------------------------------------------------------

    async def _fetch_candidates(
        self, verbose: bool = False
    ) -> tuple[list[Market], dict[str, Orderbook]]:
        include = self.universe_cfg["categories"]["include"]
        exclude = (self.universe_cfg["categories"].get("exclude") or [])
        min_hours = self.universe_cfg["time_filters"]["min_hours_to_resolution"]
        max_per_run = self.runtime_cfg["research"].get("max_recommendations_per_run", 5)

        async with KalshiClient() as client:
            try:
                # Fetch markets per series — bulk fetch only returns combo markets
                markets: list = []
                for series in include:
                    try:
                        batch = await client.get_markets(series_ticker=series, limit=50)
                        markets.extend(batch)
                    except Exception as e:
                        log.warning("series_fetch_failed", series=series, error=str(e))
            except Exception as e:
                log.error("fetch_markets_failed", error=str(e))
                return [], {}

            log.info("markets_fetched", total=len(markets))

            # Rejection tally for diagnostics
            rejected: dict[str, int] = {
                "universe": 0, "time_to_resolution": 0, "liquidity": 0
            }

            candidates: list[Market] = []
            for m in markets:
                if not in_universe(m.ticker, include, exclude):
                    rejected["universe"] += 1
                    if verbose:
                        log.debug("rejected_universe", ticker=m.ticker)
                    continue

                ok, reason = passes_time_to_resolution(m, min_hours)
                if not ok:
                    rejected["time_to_resolution"] += 1
                    if verbose:
                        log.debug("rejected_time", ticker=m.ticker, reason=reason)
                    continue

                ok, reason = passes_liquidity_floors(
                    m,
                    self.risk_cfg.min_open_interest,
                    self.risk_cfg.min_daily_volume,
                    self.risk_cfg.max_spread_bps,
                )
                if not ok:
                    rejected["liquidity"] += 1
                    if verbose:
                        log.debug("rejected_liquidity", ticker=m.ticker, reason=reason,
                                  oi=m.open_interest, vol=m.volume)
                    continue

                candidates.append(m)

            log.info(
                "filter_summary",
                total_fetched=len(markets),
                passed=len(candidates),
                rejected_universe=rejected["universe"],
                rejected_time=rejected["time_to_resolution"],
                rejected_liquidity=rejected["liquidity"],
            )

            # Skip tickers already analyzed in the last 6 hours — no need to
            # re-spend credits on a market Claude already evaluated this cycle.
            recent = self.ledger.get_recently_analyzed_tickers(hours=6.0)
            if recent:
                before = len(candidates)
                candidates = [c for c in candidates if c.ticker not in recent]
                skipped = before - len(candidates)
                if skipped:
                    log.info("skipped_recently_analyzed", count=skipped, ttl_hours=6.0)

            # Rank by a simple edge-potential score:
            #   volume × (1 + price_extremity)
            # High volume = actively traded / newsworthy.
            # Extreme price (far from 50¢) = more room for mispricing.
            def _edge_score(m: Market) -> float:
                mid = (m.yes_bid + m.yes_ask) / 2 if m.yes_ask > 0 else 50
                extremity = abs(mid - 50) / 50  # 0 at 50¢, 1 at 0¢ or 100¢
                return m.volume * (1 + extremity)

            # Stratified cap: each configured series gets at most an equal share
            # of the candidate slots.  Without this, high-volume sports markets
            # (NBA/NHL playoffs) crowd out lower-volume but valid categories like
            # KXBTC or KXFED that the user explicitly included.
            total_slots = max_per_run * 2
            num_series = max(len(include), 1)
            per_series_cap = max(2, -(-total_slots // num_series))  # ceiling division

            by_series: dict[str, list[Market]] = {}
            for m in sorted(candidates, key=_edge_score, reverse=True):
                series_key = next(
                    (s for s in include if m.ticker.upper().startswith(s.upper())),
                    "OTHER",
                )
                bucket = by_series.setdefault(series_key, [])
                if len(bucket) < per_series_cap:
                    bucket.append(m)

            # Re-sort the stratified set globally, then cap at total_slots
            candidates = sorted(
                [m for bucket in by_series.values() for m in bucket],
                key=_edge_score,
                reverse=True,
            )[:total_slots]

            log.info(
                "candidate_breakdown",
                **{s: len(b) for s, b in by_series.items()},
            )

            orderbooks: dict[str, Orderbook] = {}
            for m in candidates[:25]:
                try:
                    orderbooks[m.ticker] = await client.get_orderbook(m.ticker)
                except Exception:
                    pass

        return candidates, orderbooks

    # ------------------------------------------------------------------
    # Internal: Research Agent call
    # ------------------------------------------------------------------

    async def _run_research_agent(
        self, candidates: list[Market], orderbooks: dict[str, Orderbook]
    ) -> list[Recommendation]:
        import anthropic

        system_prompt = _build_system_prompt(self.universe_cfg, self.risk_cfg)
        max_recs = self.runtime_cfg["research"].get("max_recommendations_per_run", 5)

        def _market_price_data(m: Market) -> dict:
            ob = orderbooks.get(m.ticker)
            best_bid = ob.best_yes_bid() if ob else None
            best_ask = ob.best_yes_ask() if ob else None
            mid = ob.mid_price() if ob else None
            # Compute no_ask for convenience (price to buy NO contracts)
            no_ask = (100 - best_bid) if best_bid is not None else None
            return {
                "ticker": m.ticker,
                "title": m.title,
                "yes_bid": best_bid,       # best price to sell YES (buy NO)
                "yes_ask": best_ask,       # best price to buy YES
                "no_ask": no_ask,          # best price to buy NO = 100 - yes_bid
                "mid_price": round(mid, 1) if mid is not None else None,
                "open_interest": m.open_interest,
                "volume": m.volume,
                "close_time": m.close_time.isoformat() if m.close_time else None,
            }

        market_summary = json.dumps(
            [_market_price_data(m) for m in candidates],
            indent=2,
        )

        user_msg = (
            f"Today is {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
            f"In-universe markets passing liquidity floors (includes live bid/ask/mid prices):\n\n"
            f"```json\n{market_summary}\n```\n\n"
            f"Research the most promising markets, form independent probability estimates "
            f"(before looking at the bid/ask), and output up to {max_recs} recommendations "
            f"as JSON objects wrapped in ```json ... ``` code fences. "
            f"Use web_search and fetch_url to gather supporting evidence. "
            f"Skip any market with edge < {self.universe_cfg['min_edge_bps']} bps.\n\n"
            f"IMPORTANT — Set limit_price_cents to yes_ask + 1 (for YES buys) or "
            f"no_ask + 1 (for NO buys). Do NOT set it to your estimated fair probability × 100. "
            f"The QA system will reject any limit price that differs from the current mid by more than 5%."
        )

        tools = [
            {
                "name": "web_search",
                "description": "Search the web for news and information about a topic.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_url",
                "description": "Fetch the text content of a URL.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 8000},
                    },
                    "required": ["url"],
                },
            },
        ]

        client = anthropic.AsyncAnthropic()
        messages: list[dict] = [{"role": "user", "content": user_msg}]
        recommendations: list[Recommendation] = []

        for iteration in range(25):  # hard cap on tool-use iterations
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=tools,
                messages=messages,
            ) as stream:
                response = await stream.get_final_message()

            messages.append({"role": "assistant", "content": response.content})

            # Log Claude's raw text response for debugging
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    log.info("claude_response_text", iteration=iteration,
                             stop_reason=response.stop_reason,
                             text_preview=block.text[:500])

            # Parse any recommendations from text blocks
            for block in response.content:
                if hasattr(block, "text"):
                    recommendations.extend(_extract_recommendations(block.text))

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "max_tokens":
                # Response was truncated — ask Claude to finish and output recommendations
                log.warning("claude_max_tokens", iteration=iteration)
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response was cut off. Please output any remaining "
                        "recommendations now as ```json ... ``` code fences, "
                        "then finish with end_turn."
                    ),
                })
                continue

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await _execute_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        return recommendations[:max_recs]

    # ------------------------------------------------------------------
    # Internal: QA pass
    # ------------------------------------------------------------------

    async def _run_qa(
        self,
        recs: list[Recommendation],
        candidates: list[Market],
        orderbooks: dict[str, Orderbook],
        state,
    ) -> list[Recommendation]:
        ctx = _build_portfolio_context(self.ledger)
        approved: list[Recommendation] = []

        async with KalshiClient() as client:
            for rec in recs:
                try:
                    ob = orderbooks.get(rec.market_ticker)
                    if ob is None:
                        try:
                            ob = await client.get_orderbook(rec.market_ticker)
                        except Exception as e:
                            log.warning("orderbook_fetch_failed",
                                        ticker=rec.market_ticker, error=str(e))
                            continue

                    market_data = {
                        "open_interest": next(
                            (m.open_interest for m in candidates
                             if m.ticker == rec.market_ticker), 0
                        ),
                        "volume": next(
                            (m.volume for m in candidates
                             if m.ticker == rec.market_ticker), 0
                        ),
                    }

                    decision = evaluate(rec, self.risk_cfg, ctx, ob, market_data)
                    self.ledger.save_recommendation(rec)
                    self.ledger.save_decision(decision)

                    log.info(
                        "qa_decision",
                        rec_id=rec.rec_id,
                        verdict=decision.decision.value,
                        market=rec.market_ticker,
                        notes=decision.qa_notes,
                    )

                    if decision.decision in (DecisionVerdict.APPROVE, DecisionVerdict.MODIFY):
                        approved.append(rec)
                        if state.mode == OperationalMode.AUTONOMOUS:
                            # place_order handles the full ledger lifecycle (APPROVED → SUBMITTED/FAILED)
                            # so _create_order is not called — it would create a duplicate stuck APPROVED record
                            await self._submit_autonomous(rec, decision, client)
                        else:
                            # HITL: creates PENDING_APPROVAL + desktop notification
                            # PAPER: creates APPROVED record without touching Kalshi
                            self._create_order(rec, decision, state.mode)

                except Exception as e:
                    log.error("qa_failed", rec_id=rec.rec_id, error=str(e))

        return approved

    async def _submit_autonomous(
        self, rec: Recommendation, decision: Decision, client: KalshiClient
    ) -> None:
        """Submit an order directly to Kalshi in AUTONOMOUS mode (no HITL gate).

        place_order() handles the full ledger lifecycle:
          APPROVED → SUBMITTED on success, FAILED on error.
        """
        from datetime import timedelta

        from src.common.schemas import ApprovedOrder
        from src.execute.orders import place_order

        if decision.approved_order is None:
            log.warning("autonomous_skip_no_order", rec_id=rec.rec_id)
            return
        ao = decision.approved_order
        try:
            expiry = datetime.now(timezone.utc) + timedelta(hours=4)
            approved_order = ApprovedOrder(
                market_ticker=ao.market_ticker,
                side=ao.side,
                order_type=ao.order_type,
                limit_price_cents=ao.limit_price_cents,
                size_contracts=ao.size_contracts,
                client_order_id=ao.client_order_id,
                expiry_ts=expiry,
            )
            submitted = await place_order(approved_order, rec.rec_id, client, self.ledger)
            log.info(
                "autonomous_order_submitted",
                rec_id=rec.rec_id,
                market=rec.market_ticker,
                side=rec.side.value,
                price=ao.limit_price_cents,
                size=ao.size_contracts,
                kalshi_order_id=submitted.kalshi_order_id,
            )
        except ValueError as e:
            # Pre-flight rejection — expected, not a system error
            log.warning("autonomous_preflight_blocked", rec_id=rec.rec_id,
                        market=rec.market_ticker, reason=str(e))
        except Exception as e:
            log.error("autonomous_order_failed", rec_id=rec.rec_id,
                      market=rec.market_ticker, error=str(e))

    def _create_order(
        self, rec: Recommendation, decision: Decision, mode: OperationalMode
    ) -> None:
        if decision.approved_order is None:
            return
        ao = decision.approved_order

        if mode == OperationalMode.HITL:
            status = OrderStatus.PENDING_APPROVAL
        else:
            # Paper mode: record approval without touching Kalshi
            status = OrderStatus.APPROVED

        order = Order(
            client_order_id=ao.client_order_id,
            rec_id=rec.rec_id,
            market_ticker=ao.market_ticker,
            side=ao.side,
            order_type=ao.order_type,
            limit_price_cents=ao.limit_price_cents,
            size_contracts=ao.size_contracts,
            status=status,
            env=KalshiEnv(os.getenv("KALSHI_ENV", "demo")),
        )
        self.ledger.save_order(order)

        if mode == OperationalMode.HITL:
            notify_pending_approval(order, decision)
