"""Tests for the research agent's tool handling and prompt construction (no API calls)."""

from datetime import datetime, timezone

from src.kalshi_client.models import Market
from src.research.agent import TOOLS, EventBrief, ResearchAgent, build_brief
from src.research.costs import UsageMeter
from tests.fakes import book

M = Market(ticker="KXNBA-26OCT25LALBOS-LAL", event_ticker="KXNBA-26OCT25LALBOS",
           title="Lakers win", close_time=datetime(2026, 10, 26, 3, tzinfo=timezone.utc),
           rules_primary="Resolves YES if the Lakers win.")
ALLOWED = {M.ticker: M}


def agent() -> ResearchAgent:
    return ResearchAgent(model="claude-sonnet-5-5", meter=UsageMeter())


async def call(name, args):
    results, skips = {}, {}
    out = await agent()._run_tool(name, args, ALLOWED, results, skips, "run1")
    return out, results, skips


async def test_record_assessment():
    (msg, err), results, _ = await call("record_assessment", {
        "market_ticker": M.ticker, "fair_yes_prob": 0.62, "confidence": "medium",
        "thesis": "Line is -170.", "key_factors": ["home"], "invalidation_event": "LeBron out",
        "sources": [{"title": "ESPN", "url": "https://espn.com/x", "trust_tier": 1}],
    })
    a = results[M.ticker]
    assert not err and a.fair_yes_prob == 0.62 and a.run_id == "run1"
    assert a.series_ticker == "KXNBA" and a.sources[0].trust_tier == 1
    assert a.market_close_time.tzinfo is None


async def test_rejects_unknown_ticker_and_bad_probability():
    (msg, err), results, _ = await call("record_assessment", {"market_ticker": "FAKE", "fair_yes_prob": 0.5})
    assert err and not results
    (msg, err), results, _ = await call("record_assessment", {"market_ticker": M.ticker, "fair_yes_prob": 1.4})
    assert err and not results


async def test_skip():
    (msg, err), results, skips = await call("skip_market", {"market_ticker": M.ticker, "reason": "no data"})
    assert not err and skips == {M.ticker: "no data"}


def test_brief_hides_prices_by_default():
    ev = EventBrief(event_ticker=M.event_ticker, markets=[M], orderbooks={M.ticker: book(M.ticker, 40, 45)})
    assert "bid/ask" not in build_brief([ev], show_prices=False)
    assert "40¢ / 45¢" in build_brief([ev], show_prices=True)


def test_strict_tools_disallow_extra_fields():
    strict = [t for t in TOOLS if t.get("strict")]
    assert {t["name"] for t in strict} == {"record_assessment", "skip_market"}
    for t in strict:
        assert t["input_schema"]["additionalProperties"] is False


def test_usage_meter_prices_cache():
    class U:
        input_tokens, output_tokens = 1_000_000, 100_000
        cache_creation_input_tokens, cache_read_input_tokens = 0, 1_000_000
    m = UsageMeter()
    cost = m.add_anthropic("claude-sonnet-5-5", U())
    assert abs(cost - (2.0 + 1.0 + 0.2)) < 1e-9
