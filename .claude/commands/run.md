---
description: Start one research cycle now
---

With the dashboard running locally, check the mode first:

```bash
curl -s http://127.0.0.1:8000/api/status
```

If the mode is `autonomous` and env is `prod`, remind the user that approved trades will be sent with real money, and confirm. Then:

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" http://127.0.0.1:8000/api/agents/run-now
```

A run takes a few minutes. Afterwards, summarize it from:

```bash
curl -s "http://127.0.0.1:8000/api/runs?limit=1"
```
