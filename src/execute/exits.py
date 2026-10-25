"""Exit rules for open positions.

Only positions this system opened are managed (they have an entry order and a
recommendation in the ledger); anything bought by hand on kalshi.com is left
alone.

  take profit  the bid has captured `take_profit_capture` of the move from our
               entry price to our fair value — most of the edge is realized
  stop loss    the bid is `stop_loss_pct` below entry (not in the final
               `stop_loss_min_hours_to_close` hours, when prices are noisy)
  news         the local model finds the thesis's invalidation event in the news
               (alert only, or exit, per config)
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime

from src.common.config import ExitConfig
from src.common.ledger import Ledger
from src.common.schemas import OrderPurpose, Side, utcnow


@dataclasses.dataclass
class ExitPlan:
    ticker: str
    side: Side
    contracts: int
    rec_id: str
    title: str
    entry_cents: float
    fair_cents: float | None
    take_profit_cents: int | None
    stop_loss_cents: int | None
    close_time: datetime | None
    invalidation_event: str = ""

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["side"] = self.side.value
        d["close_time"] = self.close_time.isoformat() if self.close_time else None
        return d


def plan_for(ledger: Ledger, ticker: str, side: Side, contracts: int, exposure_cents: int,
             cfg: ExitConfig) -> ExitPlan | None:
    entries = [o for o in ledger.get_orders_for_ticker(ticker)
               if o.purpose == OrderPurpose.ENTRY and o.side == side and o.filled_contracts > 0]
    if not entries:
        return None
    rec_id = entries[-1].rec_id
    rec = ledger.get_recommendation(rec_id)
    buys = [f for f in ledger.get_fills_for_ticker(ticker) if f.action == "buy" and f.side == side]
    n = sum(f.count for f in buys)
    entry = (sum(f.price_cents * f.count for f in buys) / n) if n else exposure_cents / max(contracts, 1)

    fair = rec.estimated_fair_prob * 100 if rec and rec.side == side else None
    tp = None
    if fair is not None and fair > entry:
        tp = min(99, math.ceil(entry + cfg.take_profit_capture * (fair - entry)))
    sl = max(1, math.floor(entry * (1 - cfg.stop_loss_pct)))
    invalidation = ""
    if rec and rec.assessment_id:
        a = ledger.get_assessment(rec.assessment_id)
        invalidation = a.invalidation_event if a else ""
    return ExitPlan(
        ticker=ticker, side=side, contracts=contracts, rec_id=rec_id,
        title=rec.title if rec else "", entry_cents=round(entry, 2), fair_cents=fair,
        take_profit_cents=tp, stop_loss_cents=sl,
        close_time=rec.market_close_time if rec else None, invalidation_event=invalidation,
    )


def decide(plan: ExitPlan, bid: int | None, cfg: ExitConfig, now: datetime | None = None) -> str | None:
    """Return the reason to exit now, or None to hold."""
    if bid is None:
        return None
    if plan.take_profit_cents is not None and bid >= plan.take_profit_cents:
        return f"take profit: bid {bid}¢ ≥ target {plan.take_profit_cents}¢ (entry {plan.entry_cents:.0f}¢)"
    if plan.stop_loss_cents is not None and bid <= plan.stop_loss_cents:
        now = now or utcnow()
        if plan.close_time is not None:
            hours_left = (plan.close_time - now).total_seconds() / 3600
            if hours_left < cfg.stop_loss_min_hours_to_close:
                return None
        return f"stop loss: bid {bid}¢ ≤ {plan.stop_loss_cents}¢ (entry {plan.entry_cents:.0f}¢)"
    return None
