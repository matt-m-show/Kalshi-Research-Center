"""Thin async httpx wrapper around the Kalshi REST API v2.

Every call is:
  - signed with RSA-PSS (auth.sign_request)
  - logged (request + response summary, secrets redacted)
  - retried on transient errors (5xx, network timeouts) with exponential back-off
  - rate-limited via a token-bucket (configurable r/w per second)
  - idempotent for order placement (client_order_id in body)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from src.common.logging import get_logger
from src.kalshi_client import auth
from src.kalshi_client.models import (
    Balance,
    CreateOrderRequest,
    KalshiFill,
    KalshiOrder,
    Market,
    Orderbook,
    Position,
)

log = get_logger("kalshi_client")

_ENV = os.getenv("KALSHI_ENV", "demo").lower()

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}

BASE_URL = BASE_URLS.get(_ENV, BASE_URLS["demo"])

# Conservative defaults — adjust once you know your rate-limit tier
_READ_RPS = 20
_WRITE_RPS = 10
_MAX_RETRIES = 4
_BACKOFF_BASE = 1.5  # seconds


# ---------------------------------------------------------------------------
# Simple token-bucket rate limiter
# ---------------------------------------------------------------------------


class _TokenBucket:
    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last = now
        if self._tokens < 1:
            await asyncio.sleep((1 - self._tokens) / self._rate)
            self._tokens = 0
        else:
            self._tokens -= 1


_read_bucket = _TokenBucket(_READ_RPS)
_write_bucket = _TokenBucket(_WRITE_RPS)


# ---------------------------------------------------------------------------
# Core client
# ---------------------------------------------------------------------------


class KalshiClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        authenticated: bool = True,
    ) -> dict:
        bucket = _write_bucket if method.upper() in ("POST", "DELETE", "PUT", "PATCH") else _read_bucket
        await bucket.acquire()

        headers: dict[str, str] = {}
        if authenticated:
            headers.update(auth.sign_request(method, f"/trade-api/v2{path}"))

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._http.request(
                    method, path, params=params, json=json, headers=headers
                )
                log.debug(
                    "api_call",
                    method=method,
                    path=path,
                    status=resp.status_code,
                    attempt=attempt,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", _BACKOFF_BASE ** attempt))
                    log.warning("rate_limited", retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise  # 4xx — don't retry
                last_exc = e
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e

            delay = _BACKOFF_BASE ** attempt
            log.warning("retrying", attempt=attempt, delay=delay, error=str(last_exc))
            await asyncio.sleep(delay)

        raise RuntimeError(f"Request failed after {_MAX_RETRIES} attempts: {last_exc}")

    # ------------------------------------------------------------------
    # Market data (public)
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
    ) -> list[Market]:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self._request("GET", "/markets", params=params, authenticated=True)
        return [Market.model_validate(m) for m in data.get("markets", [])]

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}", authenticated=True)
        return Market.model_validate(data.get("market", data))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth}, authenticated=True,
        )

        def _parse_dollar_levels(levels: list) -> list[dict]:
            """Convert [["0.0110", "242.00"], ...] → [{price: cents, delta: qty}]"""
            result = []
            for level in (levels or []):
                price_cents = int(round(float(level[0]) * 100))
                qty = max(int(float(level[1])), 1)
                result.append({"price": price_cents, "delta": qty})
            return result

        # New API: orderbook_fp with yes_dollars / no_dollars
        fp = data.get("orderbook_fp")
        if fp:
            return Orderbook(
                market_ticker=ticker,
                yes=_parse_dollar_levels(fp.get("yes_dollars", [])),
                no=_parse_dollar_levels(fp.get("no_dollars", [])),
            )

        # Legacy API: orderbook with yes / no (integer cents)
        ob = data.get("orderbook", data)
        return Orderbook(
            market_ticker=ticker,
            yes=[{"price": l[0], "delta": l[1]} for l in ob.get("yes", [])],
            no=[{"price": l[0], "delta": l[1]} for l in ob.get("no", [])],
        )

    # ------------------------------------------------------------------
    # Portfolio (authenticated)
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance")
        raw = data.get("balance", data)
        # Kalshi prod returns balance as a bare integer (cents); handle both shapes.
        if isinstance(raw, (int, float)):
            return Balance(balance=int(raw))
        return Balance.model_validate(raw)

    async def get_positions(self, limit: int = 200) -> list[Position]:
        data = await self._request("GET", "/portfolio/positions", params={"limit": limit})
        return [Position.model_validate(p) for p in data.get("market_positions", [])]

    async def get_orders(self, status: str | None = None, limit: int = 200) -> list[KalshiOrder]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [KalshiOrder.model_validate(o) for o in data.get("orders", [])]

    async def get_fills(self, limit: int = 200, ticker: str | None = None) -> list[KalshiFill]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return [KalshiFill.model_validate(f) for f in data.get("fills", [])]

    # ------------------------------------------------------------------
    # Order placement (authenticated + write rate-limited)
    # ------------------------------------------------------------------

    async def create_order(self, req: CreateOrderRequest) -> KalshiOrder:
        payload = req.model_dump(exclude_none=True)
        data = await self._request("POST", "/portfolio/orders", json=payload)
        order = data.get("order", data)
        log.info("order_created", ticker=req.ticker, client_order_id=req.client_order_id)
        return KalshiOrder.model_validate(order)

    async def cancel_order(self, order_id: str) -> KalshiOrder:
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        log.info("order_cancelled", order_id=order_id)
        return KalshiOrder.model_validate(data.get("order", data))

    async def cancel_all_orders(self) -> list[str]:
        """Cancel all open orders; returns list of cancelled order IDs."""
        orders = await self.get_orders(status="resting")
        ids: list[str] = []
        for o in orders:
            try:
                await self.cancel_order(o.order_id)
                ids.append(o.order_id)
            except Exception as e:
                log.error("cancel_failed", order_id=o.order_id, error=str(e))
        return ids
