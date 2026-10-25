---
description: Halt, cancel every resting order and sell every position at market
---

This is destructive: it sells every position immediately at whatever the market gives, which can lock in losses. Show the current positions first:

```bash
curl -s http://127.0.0.1:8000/api/portfolio
```

Summarize them and ask the user to confirm explicitly. Only then:

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" http://127.0.0.1:8000/api/flatten
```

Report which orders were cancelled, which positions were closed, and any errors. The system is left halted; /resume re-enables it.
