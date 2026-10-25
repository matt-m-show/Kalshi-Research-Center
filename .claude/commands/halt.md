---
description: Stop all new research and orders
argument-hint: [reason]
---

With the dashboard running locally, halt the system (`$ARGUMENTS` is an optional reason):

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" \
  "http://127.0.0.1:8000/api/halt?reason=<url-encoded reason>"
```

Halting stops research, new orders and automatic exits. Orders already resting on Kalshi stay there — use /flatten to cancel them and close positions.

If the dashboard isn't running, create the file `data/HALT` instead; every order path checks for it.
