# /approve — Approve a pending HITL trade

Used in HITL mode to approve a trade that the Execute Agent has queued for your review.

## Usage

```
/approve <rec_id>
```

To see all pending trades:
```
/pending
```

## What this does

1. Calls `src.execute.hitl.approve(rec_id, ledger)`:
   - Finds the Order with `status = PENDING_APPROVAL` matching the `rec_id`.
   - Updates status to `APPROVED`.
   - Writes audit entry `HITL_APPROVED`.
2. The Execute Agent's next scheduled pass will pick up the `APPROVED` order and submit it to Kalshi.

## Trade summary

Before approving, Claude will display:
- Market ticker and side
- Size and limit price
- QA notes and any modifications the QA Agent made
- Worst-case loss
- Research Agent's thesis (condensed)

## To deny instead

```
/deny <rec_id> [optional reason]
```

This cancels the pending order without submitting it.
