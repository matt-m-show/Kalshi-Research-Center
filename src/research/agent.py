"""The research model loop: Claude + web tools → Assessments.

The model sees market descriptions (by default without Kalshi prices, to keep
its estimate independent) and records one fair-YES probability per market via
a strict tool. IDs and timestamps are assigned here, never by the model.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.common.logging import get_logger
from src.common.schemas import Assessment, Confidence, Source, TrustTier, utcnow
from src.kalshi_client.models import Market, Orderbook
from src.research import tools as web
from src.research.costs import UsageMeter

log = get_logger("research_agent")

AGENT_SPEC_PATH = Path(__file__).parents[2] / ".claude" / "agents" / "research.md"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclasses.dataclass
class EventBrief:
    event_ticker: str
    markets: list[Market]
    orderbooks: dict[str, Orderbook] = dataclasses.field(default_factory=dict)
    evidence: str = ""                 # pre-gathered by the local model, unverified
    priority: int | None = None        # local model's research priority, 1-5


# ---------------------------------------------------------------------------
# Tool definitions (deterministic order → stable prompt cache prefix)
# ---------------------------------------------------------------------------

_SOURCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "trust_tier": {"type": "integer", "enum": [1, 2, 3]},
    },
    "required": ["title", "url", "trust_tier"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": "Search the web. Returns titles, snippets and URLs.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch the readable text of a public web page.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "record_assessment",
        "description": (
            "Record your fair probability that one market resolves YES, with the evidence "
            "behind it. Call once per market you estimate."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "market_ticker": {"type": "string"},
                "fair_yes_prob": {"type": "number", "description": "Probability of YES, 0 to 1."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "thesis": {"type": "string", "description": "2-4 sentences."},
                "key_factors": {"type": "array", "items": {"type": "string"}},
                "invalidation_event": {
                    "type": "string",
                    "description": "News that would make this estimate wrong (e.g. 'starter ruled out').",
                },
                "sources": {"type": "array", "items": _SOURCE_SCHEMA},
            },
            "required": ["market_ticker", "fair_yes_prob", "confidence", "thesis",
                         "key_factors", "invalidation_event", "sources"],
        },
    },
    {
        "name": "skip_market",
        "description": "Decline to estimate a listed market, with a one-line reason.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "market_ticker": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["market_ticker", "reason"],
        },
    },
]


def load_system_prompt() -> str:
    text = AGENT_SPEC_PATH.read_text(encoding="utf-8") if AGENT_SPEC_PATH.exists() else ""
    return re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL).strip()


def naive_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def _fmt_time(ts: datetime | None) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "unknown"


def build_brief(events: list[EventBrief], show_prices: bool) -> str:
    lines = [f"Current time: {utcnow():%Y-%m-%d %H:%M} UTC.", "",
             "Estimate the YES probability for each market below.", ""]
    for ev in events:
        lines.append(f"## Event {ev.event_ticker}")
        for m in ev.markets:
            lines.append(f"- **{m.ticker}** — {m.title or m.yes_sub_title}")
            lines.append(f"  closes {_fmt_time(m.close_time)}; expected resolution "
                         f"{_fmt_time(m.expected_expiration_time or m.close_time)}")
            if m.rules_primary:
                lines.append(f"  rules: {m.rules_primary[:400]}")
            ob = ev.orderbooks.get(m.ticker)
            if show_prices and ob is not None:
                lines.append(f"  Kalshi YES bid/ask: {ob.best_yes_bid()}¢ / {ob.best_yes_ask()}¢")
        if ev.evidence:
            lines.append("Pre-gathered evidence (search snippets digested by a small local model — "
                         "unverified; confirm anything decisive before relying on it):")
            lines.append(ev.evidence)
        lines.append("")
    lines.append("Record an assessment or a skip for every market listed.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


class ResearchAgent:
    def __init__(
        self,
        model: str,
        meter: UsageMeter,
        max_iterations: int = 12,
        max_cost_usd: float = 0.50,
        effort: str = "medium",
        fetch_hook: Any = None,
        refusal_fallbacks: bool = True,
    ) -> None:
        self.model = model
        self.refusal_fallbacks = refusal_fallbacks
        self.meter = meter
        self.max_iterations = max_iterations
        self.max_cost_usd = max_cost_usd
        self.effort = effort
        # Optional async callable(url, text) -> str applied to fetched pages
        self.fetch_hook = fetch_hook
        self.notes: list[str] = []

    async def _run_tool(self, name: str, args: dict, allowed: dict[str, Market],
                        results: dict[str, Assessment], skips: dict[str, str],
                        run_id: str | None) -> tuple[str, bool]:
        if name == "web_search":
            return await web.web_search(str(args.get("query", ""))), False
        if name == "fetch_url":
            url = str(args.get("url", ""))
            text = await web.fetch_text(url)
            if self.fetch_hook and not text.startswith(("Fetch refused", "Fetch error")):
                text = await self.fetch_hook(url, text)
            return text[:8000], False

        ticker = str(args.get("market_ticker", ""))
        market = allowed.get(ticker)
        if market is None:
            return f"Unknown market_ticker {ticker!r}; use one of the listed tickers.", True

        if name == "skip_market":
            skips[ticker] = str(args.get("reason", ""))[:300]
            results.pop(ticker, None)
            return "Skipped.", False

        if name == "record_assessment":
            try:
                p = float(args["fair_yes_prob"])
            except (KeyError, TypeError, ValueError):
                return "fair_yes_prob must be a number between 0 and 1.", True
            if not 0.0 <= p <= 1.0:
                return f"fair_yes_prob must be between 0 and 1, got {p}.", True
            sources = []
            for s in args.get("sources") or []:
                try:
                    sources.append(Source(title=str(s["title"])[:200], url=str(s["url"])[:500],
                                          trust_tier=TrustTier(int(s["trust_tier"]))))
                except Exception:
                    continue
            results[ticker] = Assessment(
                run_id=run_id,
                market_ticker=ticker,
                event_ticker=market.event_ticker,
                series_ticker=market.series_ticker,
                title=market.title,
                fair_yes_prob=p,
                confidence=Confidence(args.get("confidence", "medium")),
                thesis=str(args.get("thesis", "")),
                key_factors=[str(k)[:200] for k in (args.get("key_factors") or [])][:8],
                sources=sources,
                invalidation_event=str(args.get("invalidation_event", ""))[:300],
                model=self.model,
                market_close_time=naive_utc(market.close_time),
            )
            skips.pop(ticker, None)
            return "Recorded.", False

        return f"Unknown tool {name}.", True

    async def run(
        self, events: list[EventBrief], run_id: str | None, show_prices: bool = False
    ) -> tuple[list[Assessment], dict[str, str]]:
        """Returns (assessments, skipped {ticker: reason})."""
        import anthropic

        allowed = {m.ticker: m for ev in events for m in ev.markets}
        results: dict[str, Assessment] = {}
        skips: dict[str, str] = {}
        client = anthropic.AsyncAnthropic()
        system = load_system_prompt()
        messages: list[dict] = [{"role": "user", "content": build_brief(events, show_prices)}]
        budget_warned = False

        # Server-side refusal fallback: a declined request is re-run on another model
        fallback = ({"betas": [FALLBACK_BETA], "extra_body": {"fallbacks": "default"}}
                    if self.refusal_fallbacks else {})
        for iteration in range(self.max_iterations):
            async with client.beta.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                messages=messages,
                cache_control={"type": "ephemeral"},
                output_config={"effort": self.effort},
                **fallback,
            ) as stream:
                resp = await stream.get_final_message()

            cost = self.meter.add_anthropic(resp.model, resp.usage)
            messages.append({"role": "assistant", "content": resp.content})
            log.info("research_turn", iteration=iteration, stop=resp.stop_reason,
                     model=resp.model, cost=round(cost, 4),
                     run_total=round(self.meter.total_cost, 4))

            if resp.stop_reason == "refusal":
                details = getattr(resp, "stop_details", None)
                self.notes.append(f"Model declined: {getattr(details, 'category', None)}")
                break
            if resp.stop_reason == "end_turn":
                break

            content: list[dict] = []
            if resp.stop_reason == "tool_use":
                calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
                outputs = await asyncio.gather(*[
                    self._run_tool(b.name, dict(b.input or {}), allowed, results, skips, run_id)
                    for b in calls
                ])
                for b, (text, is_err) in zip(calls, outputs):
                    block = {"type": "tool_result", "tool_use_id": b.id, "content": text}
                    if is_err:
                        block["is_error"] = True
                    content.append(block)

            over_budget = self.meter.total_cost >= self.max_cost_usd
            if over_budget and budget_warned:
                self.notes.append(f"Stopped at the ${self.max_cost_usd:.2f} per-run budget.")
                if content:
                    messages.append({"role": "user", "content": content})
                break
            if over_budget:
                budget_warned = True
                content.append({"type": "text", "text": (
                    "Research budget for this run is used up. Without further searches, record "
                    "assessments or skips now for every market, then stop.")})
            elif resp.stop_reason == "max_tokens":
                content.append({"type": "text", "text": "Continue: record any remaining assessments."})

            if not content:
                break
            messages.append({"role": "user", "content": content})
        else:
            self.notes.append(f"Hit the {self.max_iterations}-turn limit.")

        missing = [t for t in allowed if t not in results and t not in skips]
        for t in missing:
            skips[t] = "not assessed (turn or budget limit)"
        return list(results.values()), skips
