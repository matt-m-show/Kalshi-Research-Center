"""Tests for the local-model pre-pass and page summarizer (fake Ollama, fake search)."""

from unittest import mock

from src.kalshi_client.models import Market
from src.llm.ollama import OllamaUnavailable
from src.research import local_assist as la
from src.research.agent import EventBrief, build_brief

EV = EventBrief(event_ticker="KXNBAGAME-26OCT26LALBOS",
                markets=[Market(ticker="KXNBAGAME-26OCT26LALBOS-LAL", title="Lakers win")])


class FakeOllama:
    model = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = self.prompt_tokens = self.output_tokens = 0

    async def health(self):
        return True, "ok"

    async def json_chat(self, system, user, schema, temperature=0.2):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def fake_search(q, max_results=5):
    return f"• result for {q}\n  URL: https://example.com"


async def test_gather_builds_evidence():
    fake = FakeOllama([
        {"queries": ["lakers injury report", "lakers celtics odds", "x", "y"]},
        {"facts": [{"fact": "LeBron questionable", "url": "https://espn.com/a"}],
         "priority": 4, "priority_reason": "fresh injury news"},
    ])
    assist = la.LocalAssist(fake, max_queries=3)
    with mock.patch.object(la.web, "web_search", fake_search):
        ev = await assist.gather(EV, "2026-10-25")
    assert ev.priority == 4 and len(ev.queries) == 3
    assert "LeBron questionable (https://espn.com/a)" in ev.as_text()


async def test_gather_returns_none_when_local_model_fails():
    assist = la.LocalAssist(FakeOllama([OllamaUnavailable("down")]))
    assert await assist.gather(EV, "2026-10-25") is None


async def test_page_hook_short_pages_pass_through():
    assist = la.LocalAssist(FakeOllama([]))
    assist.available = True
    assert await assist.page_hook("focus")("https://x", "short text") == "short text"


async def test_page_hook_summarizes_long_pages():
    assist = la.LocalAssist(FakeOllama([
        {"relevant": True, "published": "2026-10-24", "points": ["Line moved to -170"]}]))
    assist.available = True
    out = await assist.page_hook("Lakers win")("https://x", "word " * 1000)
    assert "Line moved to -170" in out and "published 2026-10-24" in out


async def test_page_hook_falls_back_to_raw_text():
    assist = la.LocalAssist(FakeOllama([OllamaUnavailable("down")]))
    assist.available = True
    out = await assist.page_hook("focus")("https://x", "a" * 9000)
    assert out == "a" * 6000


def test_evidence_appears_in_brief_as_unverified():
    ev = EventBrief(event_ticker=EV.event_ticker, markets=EV.markets, evidence="  - fact (url)")
    brief = build_brief([ev], show_prices=False)
    assert "unverified" in brief and "fact (url)" in brief
