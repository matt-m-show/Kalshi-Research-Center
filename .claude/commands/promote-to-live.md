# /promote-to-live — Promote from paper mode to HITL live trading

Switches the system from the Kalshi demo environment to the live production environment, with HITL approval per trade.

## Prerequisites (all must be true)

The Performance Agent will verify these before proceeding:

- [ ] Minimum paper-mode duration (see `config/promotion.yaml`)
- [ ] Minimum closed trades
- [ ] Rolling 30-day Brier score ≤ calibration threshold
- [ ] Rolling return ≥ minimum threshold
- [ ] Max drawdown ≤ threshold
- [ ] Zero unresolved Sev-1 incidents (failed reconciliations, auth outages)
- [ ] Production RSA keypair stored in OS keychain (`python -m src.kalshi_client.auth setup --env prod`)

## What this does

1. Prints a full performance summary (the Performance Agent generates it live).
2. Displays each gate and whether it passes.
3. **Requires you to type `CONFIRM PROMOTE TO LIVE`** (exact string) to proceed.
4. Sets `KALSHI_ENV=prod` in the runtime environment.
5. Sets `system_state.mode = "hitl"`.
6. Writes audit entry `PROMOTED_TO_LIVE`.
7. Fires desktop notification.

## After promotion

The system remains in HITL mode — every trade still requires your one-click approval. To move to fully autonomous, run `/promote-to-autonomous` (separate gate, separate command).

## To revert to paper mode

Manually set `KALSHI_ENV=demo` and update `system_state.mode = "paper"` in the DB. All pending orders will be halted.
