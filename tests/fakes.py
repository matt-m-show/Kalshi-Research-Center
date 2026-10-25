"""In-memory stand-in for KalshiClient used by execution and manager tests."""

from __future__ import annotations

from src.kalshi_client.client import KalshiAPIError
from src.kalshi_client.models import (
    Balance,
    CreateOrderV2Request,
    CreateOrderV2Response,
    KalshiFill,
    KalshiOrder,
    Market,
    Orderbook,
    OrderbookLevel,
    Position,
    Settlement,
)


def book(ticker: str, yes_bid: int | None, yes_ask: int | None, depth: int = 100) -> Orderbook:
    return Orderbook(
        market_ticker=ticker,
        yes=[OrderbookLevel(price=yes_bid, delta=depth)] if yes_bid is not None else [],
        no=[OrderbookLevel(price=100 - yes_ask, delta=depth)] if yes_ask is not None else [],
    )


class FakeKalshi:
    def __init__(self) -> None:
        self.books: dict[str, Orderbook] = {}
        self.balance_cents = 5_000
        self.positions: list[Position] = []
        self.orders: dict[str, KalshiOrder] = {}
        self.fills: list[KalshiFill] = []
        self.settlements: list[Settlement] = []
        self.markets: dict[str, Market] = {}
        self.created: list[CreateOrderV2Request] = []
        self.cancelled: list[str] = []
        self.fail_create: str | None = None
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        return self.books[ticker]

    async def get_balance(self) -> Balance:
        return Balance(balance=self.balance_cents)

    async def get_positions(self, *a, **k) -> list[Position]:
        return self.positions

    async def create_order(self, req: CreateOrderV2Request) -> CreateOrderV2Response:
        if self.fail_create:
            raise KalshiAPIError(400, self.fail_create, "/portfolio/events/orders")
        self.created.append(req)
        self._n += 1
        oid = f"k{self._n}"
        self.orders[oid] = KalshiOrder(order_id=oid, client_order_id=req.client_order_id,
                                       ticker=req.ticker, status="resting",
                                       count=int(req.count), remaining_count=int(req.count))
        return CreateOrderV2Response(order_id=oid, client_order_id=req.client_order_id,
                                     fill_count=0, remaining_count=int(req.count))

    async def cancel_order(self, order_id: str, ticker: str | None = None) -> int:
        self.cancelled.append(order_id)
        if order_id in self.orders:
            self.orders[order_id].status = "canceled"
        return 1

    async def cancel_all_orders(self) -> list[str]:
        ids = [o.order_id for o in self.orders.values() if o.status == "resting"]
        for i in ids:
            await self.cancel_order(i)
        return ids

    async def get_orders(self, *a, **k) -> list[KalshiOrder]:
        return list(self.orders.values())

    async def get_order(self, order_id: str) -> KalshiOrder:
        return self.orders[order_id]

    async def get_fills(self, *a, **k) -> list[KalshiFill]:
        return self.fills

    async def get_settlements(self, *a, **k) -> list[Settlement]:
        return self.settlements

    async def get_markets(self, status=None, tickers=None, series_ticker=None, **k) -> list[Market]:
        if series_ticker:
            return [m for m in self.markets.values() if m.series_ticker == series_ticker]
        return [self.markets[t] for t in (tickers or []) if t in self.markets]

    async def get_series(self, series_ticker: str):
        raise KalshiAPIError(404, "not found", f"/series/{series_ticker}")
