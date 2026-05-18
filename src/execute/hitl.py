"""HITL (Human-In-The-Loop) approval flow.

When the system is in HITL mode, the Execute Agent writes orders with status
PENDING_APPROVAL and sends a desktop notification. Execution waits until
the user runs `/approve <rec_id>` or `/deny <rec_id>`.

This module provides:
  - notify_pending_approval(order)  — fires the desktop notification
  - approve(rec_id, ledger)         — called by /approve command
  - deny(rec_id, ledger)            — called by /deny command
  - list_pending(ledger)            — list all awaiting HITL approval
"""

from __future__ import annotations

import json
from datetime import datetime

from src.common.ledger import Ledger
from src.common.logging import get_logger
from src.common.schemas import Decision, Order, OrderStatus

log = get_logger("hitl")


def _notify(title: str, message: str) -> None:
    """Fire a desktop notification (best-effort; silent on failure)."""
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=30)
    except Exception as e:
        log.warning("notification_failed", error=str(e))
    # Always echo to stdout as a fallback
    print(f"\n{'='*60}\n[HITL] {title}\n{message}\n{'='*60}\n")


def notify_pending_approval(order: Order, decision: Decision) -> None:
    """Send a desktop notification when a trade is awaiting HITL approval."""
    rec = f"{order.market_ticker} {order.side.value} {order.size_contracts}@ {order.limit_price_cents}¢"
    msg = (
        f"Market: {order.market_ticker}\n"
        f"Side: {order.side.value}  Size: {order.size_contracts} contracts @ {order.limit_price_cents}¢\n"
        f"QA notes: {decision.qa_notes or 'none'}\n"
        f"To approve: /approve {order.rec_id}\n"
        f"To deny:    /deny {order.rec_id}"
    )
    log.info("hitl_pending", rec_id=order.rec_id, trade=rec)
    _notify("Kalshi: Trade Awaiting Approval", msg)


def list_pending(ledger: Ledger) -> list[Order]:
    """Return all orders currently in PENDING_APPROVAL status."""
    return ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)


def approve(rec_id: str, ledger: Ledger) -> Order | None:
    """Mark a pending order as APPROVED.  Returns the updated Order, or None if not found."""
    orders = ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
    target = next((o for o in orders if o.rec_id == rec_id), None)
    if target is None:
        log.warning("approve_not_found", rec_id=rec_id)
        return None
    target.status = OrderStatus.APPROVED
    target.last_updated_at = datetime.utcnow()
    ledger.save_order(target)
    ledger.append_audit("HITL_APPROVED", "user", {"rec_id": rec_id, "order_id": target.order_id})
    log.info("hitl_approved", rec_id=rec_id, order_id=target.order_id)
    _notify("Kalshi: Trade Approved", f"rec_id={rec_id} will be submitted shortly.")
    return target


def deny(rec_id: str, ledger: Ledger, reason: str = "User denied via /deny") -> Order | None:
    """Mark a pending order as CANCELLED (user denied via HITL)."""
    orders = ledger.get_orders_by_status(OrderStatus.PENDING_APPROVAL.value)
    target = next((o for o in orders if o.rec_id == rec_id), None)
    if target is None:
        log.warning("deny_not_found", rec_id=rec_id)
        return None
    target.status = OrderStatus.CANCELLED
    target.last_updated_at = datetime.utcnow()
    ledger.save_order(target)
    ledger.append_audit("HITL_DENIED", "user", {"rec_id": rec_id, "order_id": target.order_id, "reason": reason})
    log.info("hitl_denied", rec_id=rec_id, reason=reason)
    return target
