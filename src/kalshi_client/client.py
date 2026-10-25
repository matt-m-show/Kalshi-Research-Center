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
    CreateOrderV2Request,
    CreateOrderV2Response,
    KalshiFill,
    KalshiOrder,
    Market,
    Orderbook,
    Position,
    Series,
    Settlement,
)


class KalshiAPIError(RuntimeError):
    """A 4xx from Kalshi, carrying the API's error code and message."""

    def __init__(self, status: int, body: str, path: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Kalshi {status} on {path}: {body[:300]}")

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
                    # 4xx — don't retry; surface Kalshi's error body
                    raise KalshiAPIError(e.response.status_code, e.response.text, path) from e
                last_exc = e
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e

            delay = _BACKOFF_BASE ** attempt
            log.warning("retrying", attempt=attempt, delay=delay, error=str(last_exc))
            await asyncio.sleep(delay)

        raise RuntimeError(f"Request failed after {_MAX_RETRIES} attempts: {last_exc}")

    async def _paged(
        self, path: str, key: str, params: dict[str, Any], max_items: int
    ) -> list[dict]:
        """Follow ``cursor`` pagination until ``max_items`` rows or the last page."""
        rows: list[dict] = []
        params = {k: v for k, v in params.items() if v is not None}
        while True:
            data = await self._request("GET", path, params=params)
            rows.extend(data.get(key, []) or [])
            cursor = data.get("cursor")
            if not cursor or len(rows) >= max_items:
                return rows[:max_items]
            params["cursor"] = cursor

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        status: str | None = "open",
        limit: int = 200,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: list[str] | None = None,
        max_items: int | None = None,
    ) -> list[Market]:
        params: dict[str, Any] = {
            "status": status,
            "limit": min(limit, 1000),
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "tickers": ",".join(tickers) if tickers else None,
        }
        rows = await self._paged("/markets", "markets", params, max_items or limit)
        return [Market.model_validate(m) for m in rows]

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data.get("market", data))

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{series_ticker}")
        return Series.model_validate(data.get("series", data))

    async def get_series_list(
        self, category: str | None = None, include_volume: bool = True
    ) -> list[Series]:
        params: dict[str, Any] = {"include_volume": str(include_volume).lower()}
        if category:
            params["category"] = category
        data = await self._request("GET", "/series", params=params)
        return [Series.model_validate(s) for s in data.get("series", []) or []]

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
        return Balance(
            balance=int(data.get("balance") or 0),
            portfolio_value=int(data.get("portfolio_value") or 0),
        )

    async def get_positions(
        self, settlement_status: str = "unsettled", max_items: int = 1000
    ) -> list[Position]:
        rows = await self._paged(
            "/portfolio/positions", "market_positions",
            {"limit": 1000, "settlement_status": settlement_status, "count_filter": "position"},
            max_items,
        )
        return [Position.model_validate(p) for p in rows]

    async def get_orders(
        self,
        status: str | None = None,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_items: int = 1000,
    ) -> list[KalshiOrder]:
        rows = await self._paged(
            "/portfolio/orders", "orders",
            {"limit": 1000, "status": status, "ticker": ticker, "min_ts": min_ts},
            max_items,
        )
        return [KalshiOrder.model_validate(o) for o in rows]

    async def get_order(self, order_id: str) -> KalshiOrder:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return KalshiOrder.model_validate(data.get("order", data))

    async def get_fills(
        self, min_ts: int | None = None, ticker: str | None = None, max_items: int = 1000
    ) -> list[KalshiFill]:
        rows = await self._paged(
            "/portfolio/fills", "fills",
            {"limit": 1000, "min_ts": min_ts, "ticker": ticker},
            max_items,
        )
        return [KalshiFill.model_validate(f) for f in rows]

    async def get_settlements(
        self, min_ts: int | None = None, max_items: int = 1000
    ) -> list[Settlement]:
        rows = await self._paged(
            "/portfolio/settlements", "settlements",
            {"limit": 1000, "min_ts": min_ts},
            max_items,
        )
        return [Settlement.model_validate(s) for s in rows]

    # ------------------------------------------------------------------
    # Order placement (V2: YES-leg book sides, dollar-string prices)
    # ------------------------------------------------------------------

    async def create_order(self, req: CreateOrderV2Request) -> CreateOrderV2Response:
        payload = req.model_dump(exclude_none=True)
        data = await self._request("POST", "/portfolio/events/orders", json=payload)
        resp = CreateOrderV2Response.model_validate(data.get("order", data))
        log.info("order_created", ticker=req.ticker, side=req.side, price=req.price,
                 count=req.count, order_id=resp.order_id, filled=resp.fill_count)
        return resp

    async def cancel_order(self, order_id: str, ticker: str | None = None) -> int:
        """Cancel a resting order; returns the number of contracts cancelled."""
        params = {"market_ticker": ticker} if ticker else None
        data = await self._request(
            "DELETE", f"/portfolio/events/orders/{order_id}", params=params
        )
        log.info("order_cancelled", order_id=order_id, ticker=ticker)
        reduced = data.get("reduced_by", "0")
        return int(float(reduced)) if reduced not in (None, "") else 0

    async def cancel_all_orders(self) -> list[str]:
        """Cancel every resting order; returns the cancelled order IDs."""
        ids: list[str] = []
        for o in await self.get_orders(status="resting"):
            try:
                await self.cancel_order(o.order_id, o.ticker)
                ids.append(o.order_id)
            except Exception as e:
                log.error("cancel_failed", order_id=o.order_id, error=str(e))
        return ids
