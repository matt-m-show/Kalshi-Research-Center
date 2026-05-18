# /deny — Deny a pending HITL trade

Cancels a trade awaiting HITL approval without submitting it to Kalshi.

## Usage

```
/deny <rec_id> [optional reason]
```

## What this does

1. Calls `src.execute.hitl.deny(rec_id, ledger, reason)`.
2. Updates the Order status to `CANCELLED`.
3. Writes audit entry `HITL_DENIED` with your reason.
4. The recommendation and QA decision remain in the ledger for performance tracking.

## Note

Denied trades count as "no trade taken" in performance analytics. If you deny trades frequently due to a specific pattern, check whether the QA Agent's rules need tightening (edit `config/risk.yaml`).
