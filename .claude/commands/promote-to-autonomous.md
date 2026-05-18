# /promote-to-autonomous — Promote from HITL to fully autonomous trading

Removes per-trade human approval. The Performance Agent becomes the sole watchdog.

## Prerequisites (all must be true)

- [ ] System must already be in `hitl` mode on live funds (must have run `/promote-to-live` first).
- [ ] Minimum HITL live trading duration (see `config/promotion.yaml`: `hitl_min_days`).
- [ ] Minimum HITL closed trades.
- [ ] No Sev-1 incidents in HITL window.
- [ ] Rolling Brier ≤ threshold and drawdown ≤ threshold (same as live gate, re-evaluated).
- [ ] Kill-switch thresholds confirmed correct in `config/risk.yaml` and `config/alerts.yaml`.

## What this does

1. Prints a full HITL-period performance summary.
2. Displays each gate.
3. **Requires you to type `CONFIRM AUTONOMOUS TRADING`** (exact string).
4. Sets `system_state.mode = "autonomous"`.
5. Writes audit entry `PROMOTED_TO_AUTONOMOUS`.
6. Confirms Performance Agent alert thresholds are armed.

## In autonomous mode

- No per-trade notifications.
- Performance Agent monitors continuously and can trigger `HALT` automatically.
- You can always run `/halt` or `/flatten` manually.
- You can always trade directly via the Kalshi web UI; the reconciliation loop will sync state.

## To revert to HITL

Set `system_state.mode = "hitl"` in the DB and restart the orchestrator.
