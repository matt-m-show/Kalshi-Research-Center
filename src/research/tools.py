"""Web tools available to the research model.

Everything returned here is untrusted text from the internet. It is passed to
the model as tool output only; it never reaches the pricing or QA engines.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

import httpx

_UA = "Mozilla/5.0 (compatible; kalshi-research/1.0)"


async def web_search(query: str, max_results: int = 5) -> str:
    """Tavily if TAVILY_API_KEY is set, otherwise DuckDuckGo's HTML endpoint."""
    key = os.getenv("TAVILY_API_KEY")
    return await (_tavily(query, max_results, key) if key else _ddg(query, max_results))


async def _tavily(query: str, max_results: int, api_key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.tavily.com/search", json={
                "api_key": api_key, "query": query, "max_results": max_results,
                "search_depth": "basic", "include_answer": True,
            })
            resp.raise_for_status()
            data = resp.json()
        lines = [f"Summary: {data['answer']}\n"] if data.get("answer") else []
        for r in data.get("results", [])[:max_results]:
            lines.append(f"• {r.get('title', '')}: {r.get('content', '')[:300]}\n  URL: {r.get('url', '')}")
        return "\n".join(lines) or "No results found."
    except Exception as e:
        return f"Search error: {e}"


async def _ddg(query: str, max_results: int) -> str:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get("https://html.duckduckgo.com/html/", params={"q": query},
                                    headers={"User-Agent": _UA})
        results = re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</',
            resp.text, re.DOTALL,
        )
        if not results:
            return "Search returned no results (DuckDuckGo may be rate-limiting). Set TAVILY_API_KEY for reliable search."
        clean = lambda s: re.sub(r"<[^>]+>", "", s).strip()
        return "\n".join(f"• {clean(t)}: {clean(s)}\n  URL: {u}" for u, t, s in results[:max_results])
    except Exception as e:
        return f"Search error: {e}"


def _is_public_host(host: str) -> bool:
    """Reject loopback, private, link-local and reserved addresses (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def fetch_text(url: str, max_chars: int = 12000) -> str:
    """Fetch a public http(s) URL and return its visible text."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "Fetch refused: only public http(s) URLs are allowed."
    if not await asyncio.to_thread(_is_public_host, parsed.hostname):
        return "Fetch refused: host resolves to a private or local address."
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            current = httpx.URL(url)
            resp = await client.get(current, headers={"User-Agent": _UA})
            hops = 0
            while resp.is_redirect and hops < 5:
                current = current.join(resp.headers.get("location", ""))
                if current.scheme not in ("http", "https") or not await asyncio.to_thread(_is_public_host, current.host):
                    return "Fetch refused: redirect to a private or local address."
                resp = await client.get(current, headers={"User-Agent": _UA})
                hops += 1
        return html_to_text(resp.text)[:max_chars]
    except Exception as e:
        return f"Fetch error: {e}"
