"""Order manager: the periodic job that keeps local state in step with Kalshi.

Each pass:
  1. sync_orders       — status / fill counts for our resting orders
  2. sync_fills        — record new fills (idempotent on Kalshi's fill id), and
                         the realized P&L of any exit sells
  3. manage_resting    — cancel expired / near-close bids, reprice outbid ones
  4. manage_positions  — take-profit / stop-loss / news exits on open positions
  5. sync_settlements  — record settled markets and our P&L on them
  6. resolve_assessments — score every estimate whose market has resolved
  7. housekeeping      — expire orders that were approved but never sent
  8. metrics           — recompute performance, enforce kill switches
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from src.common.config import (
    ExecutionConfig,
    ExitConfig,
    load_execution_config,
    load_exit_config,
    load_runtime_config,
    load_yaml,
)
from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import (
    OPEN_ORDER_STATUSES,
    Fill,
    MarketSettlement,
    Order,
    OrderPurpose,
    OrderStatus,
    Side,
    utcnow,
)
from src.execute.exits import decide, plan_for
from src.execute.orders import cancel, replace_at, sell_now
from src.kalshi_client.client import KalshiClient
from src.qa.pricing import reprice

log = get_logger("order_manager")

EXIT_RETRY_MINUTES = 15

_STATUS_MAP = {
    "resting": OrderStatus.SUBMITTED,
    "executed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
}


def _naive(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


class OrderManager:
    def __init__(self, ledger: Ledger, notify=None) -> None:
        self.ledger = ledger
        self.notify = notify or (lambda kind, data: None)
        self._api_errors: list[float] = []
        self._exit_attempts: dict[str, datetime] = {}
        self._news_checked: dict[str, datetime] = {}
        self._news_flagged: set[str] = set()
        self._assist = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run_once(self, client: KalshiClient | None = None) -> dict:
        cfg = load_execution_config()
        exit_cfg = load_exit_config()
        summary: dict = {}
        own_client = client is None
        client = client or KalshiClient()
        try:
            for name, step in [
                ("orders_updated", lambda: self.sync_orders(client)),
                ("fills_recorded", lambda: self.sync_fills(client)),
                ("orders_managed", lambda: self.manage_resting(client, cfg)),
                ("exits", lambda: self.manage_positions(client, exit_cfg)),
                ("settlements_recorded", lambda: self.sync_settlements(client)),
                ("assessments_resolved", lambda: self.resolve_assessments(client)),
            ]:
                try:
                    summary[name] = await step()
                except Exception as e:
                    summary[name] = f"error: {e}"
                    log.error("manager_step_failed", step=name, error=str(e))
                    self._record_api_error()
            summary["orders_expired_unsent"] = self.ledger.expire_unsent_orders(older_than_hours=24)
            summary["kill_switch"] = self.check_kill_switches()
        finally:
            if own_client:
                await client.__aexit__()
        log.info("manager_pass", **{k: v for k, v in summary.items() if v})
        return summary

    # ------------------------------------------------------------------
    # 1. Orders
    # ------------------------------------------------------------------

    async def sync_orders(self, client: KalshiClient) -> int:
        local = [o for o in self.ledger.get_orders_by_status(*OPEN_ORDER_STATUSES) if o.kalshi_order_id]
        if not local:
            return 0
        oldest = min(o.submitted_at or o.last_updated_at for o in local)
        remote = {o.order_id: o for o in await client.get_orders(
            min_ts=int(oldest.replace(tzinfo=timezone.utc).timestamp()) - 3600)}
        updated = 0
        for o in local:
            r = remote.get(o.kalshi_order_id)
            if r is None:
                try:
                    r = await client.get_order(o.kalshi_order_id)
                except Exception as e:
                    log.warning("order_lookup_failed", order_id=o.order_id, error=str(e))
                    continue
            new_status = _STATUS_MAP.get(r.status, o.status)
            if new_status == OrderStatus.SUBMITTED and r.filled_count > 0:
                new_status = OrderStatus.PARTIALLY_FILLED
            if new_status == OrderStatus.CANCELLED and not o.status_reason:
                o.status_reason = "cancelled or expired on Kalshi"
            if new_status != o.status or r.filled_count != o.filled_contracts or r.fees != o.fees_cents:
                o.status, o.filled_contracts, o.fees_cents = new_status, r.filled_count, r.fees
                o.last_updated_at = utcnow()
                self.ledger.save_order(o)
                updated += 1
        return updated

    # ------------------------------------------------------------------
    # 2. Fills
    # ------------------------------------------------------------------

    async def sync_fills(self, client: KalshiClient) -> int:
        last = self.ledger.latest_fill_time()
        since = (last - timedelta(hours=1)) if last else utcnow() - timedelta(days=30)
        recorded = 0
        for kf in await client.get_fills(min_ts=int(since.replace(tzinfo=timezone.utc).timestamp())):
            if not kf.trade_id or self.ledger.has_fill(kf.trade_id):
                continue
            order = self.ledger.get_order_by_kalshi_id(kf.order_id)
            if order is None:
                # A trade placed outside this system (e.g. manually on kalshi.com)
                side = Side.YES if kf.outcome_side == "yes" else Side.NO
                rec_id, order_id, action = "external", "external", kf.action or "buy"
            else:
                side, rec_id, order_id, action = order.side, order.rec_id, order.order_id, order.action
            self.ledger.save_fill(Fill(
                kalshi_trade_id=kf.trade_id, order_id=order_id, rec_id=rec_id,
                market_ticker=kf.ticker, side=side, action=action,
                price_cents=kf.price_for(side), count=kf.count, is_taker=kf.is_taker,
                fee_cents=kf.fee, filled_at=_naive(kf.created_time) or utcnow(),
            ))
            recorded += 1
            if order is not None:
                self._update_avg_price(order)
                if order.action == "sell":
                    self._record_exit_pnl(order, kf.trade_id, kf.price_for(side), kf.count, kf.fee,
                                          _naive(kf.created_time) or utcnow())
        return recorded

    def _record_exit_pnl(self, order: Order, trade_id: str, price: int, count: int, fee: int,
                         at: datetime) -> None:
        """Realized P&L for contracts sold before settlement (Kalshi's settlement report
        won't include them)."""
        buys = [f for f in self.ledger.get_fills_for_ticker(order.market_ticker)
                if f.action == "buy" and f.side == order.side]
        n = sum(f.count for f in buys)
        if not n:
            return
        entry = sum(f.price_cents * f.count for f in buys) / n
        entry_fees = sum(f.fee_cents for f in buys) * count / n
        pnl = round((price - entry) * count - fee - entry_fees)
        self.ledger.save_settlement(MarketSettlement(
            market_ticker=f"{order.market_ticker}#exit#{trade_id}", result="exit",
            cost_cents=round(entry * count), revenue_cents=price * count,
            fee_cents=round(fee + entry_fees), pnl_cents=pnl, settled_at=at,
        ))

    def _update_avg_price(self, order: Order) -> None:
        fills = self.ledger.get_fills_for_order(order.order_id)
        n = sum(f.count for f in fills)
        if n:
            order.avg_fill_price_cents = sum(f.price_cents * f.count for f in fills) / n
            order.filled_contracts = max(order.filled_contracts, n)
            order.fees_cents = max(order.fees_cents, sum(f.fee_cents for f in fills))
            if order.filled_contracts >= order.size_contracts:
                order.status = OrderStatus.FILLED
            order.last_updated_at = utcnow()
            self.ledger.save_order(order)

    # ------------------------------------------------------------------
    # 3. Resting order management
    # ------------------------------------------------------------------

    async def manage_resting(self, client: KalshiClient, cfg: ExecutionConfig) -> int:
        actions = 0
        now = utcnow()
        for o in self.ledger.get_orders_by_status(*OPEN_ORDER_STATUSES):
            if o.purpose != OrderPurpose.ENTRY or not o.kalshi_order_id:
                continue
            close_cutoff = (o.market_close_time - timedelta(minutes=cfg.cancel_before_close_minutes)
                            if o.market_close_time else None)
            if o.expires_at and now >= o.expires_at:
                await cancel(o, client, self.ledger, reason="order TTL reached")
                actions += 1
                continue
            if close_cutoff and now >= close_cutoff:
                await cancel(o, client, self.ledger, reason="market closing soon")
                actions += 1
                continue

            due = now - o.last_updated_at >= timedelta(minutes=cfg.reprice_interval_minutes)
            if not due or o.reprice_count >= cfg.max_reprices or o.max_price_cents is None:
                continue
            ob = await client.get_orderbook(o.market_ticker)
            new_price = reprice(ob, o.side, o.limit_price_cents, o.max_price_cents)
            if new_price is not None:
                log.info("repricing", ticker=o.market_ticker, side=o.side.value,
                         old=o.limit_price_cents, new=new_price, max=o.max_price_cents)
                await replace_at(o, new_price, client, self.ledger)
                actions += 1
        return actions

    # ------------------------------------------------------------------
    # 4. Exits on open positions
    # ------------------------------------------------------------------

    async def _local_assist(self):
        """Lazily start the local model for the news watch (None if unavailable)."""
        if self._assist is None:
            from src.llm.ollama import OllamaClient
            from src.research.local_assist import LocalAssist
            rt = load_runtime_config()
            cfg = rt.get("local_llm") or {}
            if not cfg.get("enabled"):
                return None
            assist = LocalAssist(OllamaClient(rt["models"].get("local", "qwen3.5:4b"),
                                              host=cfg.get("host"),
                                              timeout=float(cfg.get("timeout_seconds", 180))))
            self._assist = assist if await assist.start() else False
        return self._assist or None

    async def manage_positions(self, client: KalshiClient, cfg: ExitConfig) -> int:
        if not cfg.enabled:
            return 0
        now = utcnow()
        actions = 0
        for pos in await client.get_positions():
            side = pos.side
            if side is None:
                continue
            ticker = pos.market_ticker
            last = self._exit_attempts.get(ticker)
            if last and now - last < timedelta(minutes=EXIT_RETRY_MINUTES):
                continue
            plan = plan_for(self.ledger, ticker, side, abs(pos.position), abs(pos.market_exposure), cfg)
            if plan is None:
                continue   # not opened by this system
            ob = await client.get_orderbook(ticker)
            bid = ob.bid(side)
            reason = decide(plan, bid, cfg, now)

            if reason is None and cfg.news_check_minutes and plan.invalidation_event:
                reason = await self._news_check(plan, cfg, now)

            if reason is None or bid is None:
                continue
            self._exit_attempts[ticker] = now
            floor = max(1, bid - cfg.max_slippage_cents)
            log.info("exit_triggered", ticker=ticker, side=side.value, contracts=plan.contracts,
                     floor=floor, reason=reason)
            self.ledger.append_audit("EXIT_TRIGGERED", "order_manager",
                                     {"ticker": ticker, "side": side.value, "contracts": plan.contracts,
                                      "floor": floor, "reason": reason})
            try:
                order = await sell_now(ticker, side, plan.contracts, floor, plan.rec_id,
                                       client, self.ledger, reason=reason)
                self.notify("exit", {"ticker": ticker, "title": plan.title, "reason": reason,
                                     "filled": order.filled_contracts, "contracts": plan.contracts})
                actions += 1
            except Exception as e:
                log.error("exit_failed", ticker=ticker, error=str(e))
        return actions

    async def _news_check(self, plan, cfg: ExitConfig, now: datetime) -> str | None:
        checked = self._news_checked.get(plan.ticker)
        if checked and now - checked < timedelta(minutes=cfg.news_check_minutes):
            return None
        assist = await self._local_assist()
        if assist is None:
            return None
        self._news_checked[plan.ticker] = now
        entries = [o for o in self.ledger.get_orders_for_ticker(plan.ticker) if o.submitted_at]
        entered = entries[0].submitted_at.strftime("%Y-%m-%d") if entries else "unknown"
        result = await assist.check_invalidation(plan.title or plan.ticker, plan.invalidation_event,
                                                 entered, now.strftime("%Y-%m-%d"))
        if not result or not result["invalidated"] or result["confidence"] != "high":
            return None
        if plan.ticker not in self._news_flagged:
            self._news_flagged.add(plan.ticker)
            self.ledger.append_audit("THESIS_INVALIDATED", "news_watch",
                                     {"ticker": plan.ticker, **result})
            self.notify("thesis_invalidated", {"ticker": plan.ticker, "title": plan.title, **result})
            try:
                from plyer import notification
                notification.notify(title=f"Kalshi: thesis broken on {plan.title or plan.ticker}",
                                    message=result["evidence"][:200], timeout=30)
            except Exception:
                pass
        if cfg.on_invalidation == "exit":
            return f"news: {result['evidence'][:160]}"
        return None

    # ------------------------------------------------------------------
    # 5. Settlements
    # ------------------------------------------------------------------

    async def sync_settlements(self, client: KalshiClient) -> int:
        since = utcnow() - timedelta(days=30)
        recorded = 0
        for s in await client.get_settlements(min_ts=int(since.replace(tzinfo=timezone.utc).timestamp())):
            st = MarketSettlement(
                market_ticker=s.ticker, event_ticker=s.event_ticker, result=s.market_result,
                yes_count=s.yes_count, no_count=s.no_count,
                cost_cents=s.yes_total_cost + s.no_total_cost, revenue_cents=s.revenue,
                fee_cents=s.fee_cost, pnl_cents=s.pnl_cents, settled_at=_naive(s.settled_time),
            )
            if self.ledger.save_settlement(st):
                recorded += 1
                if s.market_result in ("yes", "no"):
                    self.ledger.resolve_assessments(s.ticker, s.market_result == "yes",
                                                    st.settled_at or utcnow())
        return recorded

    # ------------------------------------------------------------------
    # 5. Score estimates on markets we didn't trade
    # ------------------------------------------------------------------

    async def resolve_assessments(self, client: KalshiClient) -> int:
        tickers = sorted(self.ledger.unresolved_assessment_tickers())
        resolved = 0
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            for m in await client.get_markets(status=None, tickers=chunk, limit=len(chunk)):
                if m.result in ("yes", "no"):
                    resolved += self.ledger.resolve_assessments(m.ticker, m.result == "yes", utcnow())
        return resolved

    # ------------------------------------------------------------------
    # 7. Kill switches
    # ------------------------------------------------------------------

    def _record_api_error(self) -> None:
        now = time.monotonic()
        self._api_errors = [t for t in self._api_errors if now - t < 60] + [now]
        limit = (load_yaml("risk").get("kill_switches") or {}).get("api_error_rate_halt_per_min", 10)
        if len(self._api_errors) >= limit and not self.ledger.is_halted():
            self.ledger.halt(f"Circuit breaker: {len(self._api_errors)} API errors in 60s",
                             actor="order_manager")

    def check_kill_switches(self) -> str | None:
        """Halt on consecutive losses or portfolio drawdown. Returns the reason if halted."""
        from src.common.config import load_risk_config
        from src.performance.reporting import save_rolling_metrics
        from src.performance.tracker import current_metrics

        cfg = load_risk_config()
        metrics = current_metrics(self.ledger)
        save_rolling_metrics(metrics)
        if self.ledger.is_halted():
            return None
        reason = None
        if metrics.consecutive_losses >= cfg.consecutive_losses_halt:
            reason = f"{metrics.consecutive_losses} consecutive losing trades"
        elif metrics.max_drawdown_cents >= cfg.portfolio_drawdown_halt_pct * cfg.bankroll_cents > 0:
            reason = (f"Drawdown ${metrics.max_drawdown_cents / 100:.2f} ≥ "
                      f"{cfg.portfolio_drawdown_halt_pct:.0%} of bankroll")
        if reason:
            self.ledger.halt(reason, actor="order_manager")
            log.critical("kill_switch_halt", reason=reason)
        return reason
