# /halt — Emergency halt

Immediately stops all trading activity.

## What this does

1. Calls `src.common.ledger.Ledger().halt(reason, actor="user")`, which:
   - Sets `system_state.halted = 1` in `data/ledger.db`
   - Touches `data/HALT` (filesystem signal for out-of-process checks)
   - Appends a `HALT` entry to `data/audit.log`
2. Calls `src.kalshi_client.client.KalshiClient().cancel_all_orders()` to cancel all open resting orders.
3. Fires a desktop notification: "Kalshi: TRADING HALTED".
4. Prints a summary: how many orders were cancelled, current open positions (not auto-closed).

## Usage

```
/halt [optional reason text]
```

## To resume

Run `/resume` or manually delete `data/HALT` and set `system_state.halted = 0` in the DB.

## Note

Open positions are **not** automatically closed by `/halt`. Use `/flatten` if you also want to close positions.
