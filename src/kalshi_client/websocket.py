"""Kalshi WebSocket client for real-time order/fill updates.

Auth: same RSA-PSS headers as REST, signing the path GET/trade-api/ws/v2.
Reconnects automatically with exponential back-off.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Coroutine

import websockets
from websockets.exceptions import ConnectionClosed

from src.common.logging import get_logger
from src.kalshi_client import auth

log = get_logger("kalshi_websocket")

_ENV = os.getenv("KALSHI_ENV", "demo").lower()

WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://trading-api.kalshi.com/trade-api/ws/v2",
}
WS_URL = WS_URLS.get(_ENV, WS_URLS["demo"])
WS_PATH = "/trade-api/ws/v2"

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class KalshiWebSocket:
    """Subscribe to fill and order_group_updates channels."""

    def __init__(
        self,
        on_fill: MessageHandler | None = None,
        on_order_update: MessageHandler | None = None,
    ) -> None:
        self._on_fill = on_fill
        self._on_order_update = on_order_update
        self._sub_id = 1
        self._running = False

    def _auth_headers(self) -> dict[str, str]:
        return auth.sign_request("GET", WS_PATH)

    async def _subscribe(self, ws: Any) -> None:
        msg = {
            "id": self._sub_id,
            "cmd": "subscribe",
            "params": {"channels": ["fill", "order_group_updates"]},
        }
        await ws.send(json.dumps(msg))
        self._sub_id += 1

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("ws_invalid_json", raw=raw[:200])
            return

        msg_type = msg.get("type", "")
        log.debug("ws_message", type=msg_type)

        if msg_type == "fill" and self._on_fill:
            await self._on_fill(msg)
        elif msg_type == "order_group_updates" and self._on_order_update:
            await self._on_order_update(msg)

    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        while self._running:
            headers = self._auth_headers()
            try:
                async with websockets.connect(WS_URL, extra_headers=headers) as ws:
                    log.info("ws_connected", url=WS_URL)
                    backoff = 1.0
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle(raw)
            except ConnectionClosed as e:
                log.warning("ws_disconnected", code=e.code, reason=e.reason)
            except Exception as e:
                log.error("ws_error", error=str(e))

            if self._running:
                log.info("ws_reconnecting", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._running = False
