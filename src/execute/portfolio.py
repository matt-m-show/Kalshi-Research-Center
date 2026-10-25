"""Builds the PortfolioContext the QA engine sizes and gates against.

Live Kalshi state (cash, positions) is the source of truth; local resting
orders are added on top because Kalshi positions don't include unfilled bids.
Falls back to local state alone if Kalshi can't be reached.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import OPEN_ORDER_STATUSES, OrderPurpose, utcnow
from src.kalshi_client.client import KalshiClient
from src.performance.tracker import calibration_by_category
from src.qa.rules import PortfolioContext, category_of

log = get_logger("portfolio")


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


async def build_portfolio_context(ledger: Ledger, client: KalshiClient | None) -> PortfolioContext:
    ctx = PortfolioContext()
    by_event: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    count_by_event: dict[str, int] = defaultdict(int)

    if client is not None:
        try:
            ctx.available_cash_cents = (await client.get_balance()).balance
            for p in await client.get_positions():
                if p.position == 0:
                    continue
                exposure = abs(p.market_exposure)
                ctx.current_deployed_cents += exposure
                by_event[event_of(p.market_ticker)] += exposure
                by_category[category_of(p.market_ticker)] += exposure
                count_by_event[event_of(p.market_ticker)] += 1
                ctx.held_tickers.add(p.market_ticker)
        except Exception as e:
            log.warning("portfolio_live_fetch_failed", error=str(e))
            ctx.available_cash_cents = None

    today = utcnow().date()
    todays_recs: set[str] = set()
    for o in ledger.get_orders_by_status(*OPEN_ORDER_STATUSES):
        if o.purpose != OrderPurpose.ENTRY:
            continue
        resting = o.remaining_contracts * o.limit_price_cents
        ctx.current_deployed_cents += resting
        by_event[event_of(o.market_ticker)] += resting
        by_category[category_of(o.market_ticker)] += resting
        if o.market_ticker not in ctx.held_tickers:
            count_by_event[event_of(o.market_ticker)] += 1
        ctx.held_tickers.add(o.market_ticker)

    for o in ledger.list_orders(limit=500):
        if (o.purpose == OrderPurpose.ENTRY and o.submitted_at is not None
                and o.submitted_at.date() == today):
            todays_recs.add(o.rec_id)
    ctx.today_new_trades = len(todays_recs)

    start_of_day = datetime(today.year, today.month, today.day)
    ctx.daily_pnl_cents = sum(s.pnl_cents for s in ledger.list_settlements(since=start_of_day))

    ctx.open_exposure_cents = ctx.current_deployed_cents
    ctx.positions_by_event = dict(by_event)
    ctx.positions_by_category = dict(by_category)
    ctx.positions_count_by_event = dict(count_by_event)

    calib = calibration_by_category(ledger.list_assessments(resolved_only=True, limit=5000))
    ctx.research_brier_by_category = {k: v["brier"] for k, v in calib.items()}
    ctx.research_trade_count_by_category = {k: v["n"] for k, v in calib.items()}
    return ctx
