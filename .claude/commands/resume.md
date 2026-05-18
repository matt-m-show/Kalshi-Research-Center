# /resume — Resume trading after a halt

Clears the HALT state and re-enables trading.

## Usage

```
/resume
```

## What this does

1. Calls `src.common.ledger.Ledger().resume(actor="user")`:
   - Sets `system_state.halted = 0` in `data/ledger.db`.
   - Deletes `data/HALT` if it exists.
   - Writes audit entry `RESUME`.
2. Fires desktop notification: "Kalshi: Trading Resumed".
3. Prints current system state summary.

## Before resuming

Consider why the system halted:
- If auto-halted by Performance Agent, read the latest alert in `data/audit.log`.
- If halted by circuit-breaker (API errors), verify API connectivity.
- If halted manually, confirm the issue that prompted the halt is resolved.
