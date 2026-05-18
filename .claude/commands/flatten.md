# /flatten — Cancel all orders and close all positions

A more aggressive version of /halt that also attempts to close open positions at market-limit prices.

## What this does

1. Halts the system (same as /halt).
2. Calls `src.execute.orders.flatten_all()`:
   - Cancels all open resting orders via `DELETE /portfolio/orders/{id}`.
   - For each open position, submits a limit sell at the most aggressive available price (1¢ for YES positions, 99¢ for NO positions — effectively market-limit).
3. Logs all actions to `data/audit.log`.
4. Prints a summary: cancelled orders, positions closed, any errors.

## Usage

```
/flatten [optional reason text]
```

## Warning

This will attempt to exit at whatever the current market price is. In thin markets, fills may be at unfavorable prices. Review your open positions before running.

## After flattening

The system remains HALTED after /flatten. Run `/resume` to restart trading after you've reviewed.
