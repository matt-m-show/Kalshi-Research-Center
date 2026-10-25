---
description: Summarize system status, account, open positions and the latest research run
---

With the dashboard running locally, fetch:

```bash
curl -s http://127.0.0.1:8000/api/status
curl -s http://127.0.0.1:8000/api/portfolio
curl -s "http://127.0.0.1:8000/api/runs?limit=3"
curl -s http://127.0.0.1:8000/api/performance
```

Give a short summary: environment and mode (and whether halted), schedule state and next runs, cash and open positions with unrealized P&L and exit levels, the latest run's funnel (considered → assessed → priced → approved → sent) and cost, realized P&L, API spend this month vs budget, and the Brier score. Flag anything that needs attention (halt, pending approvals, failed orders, budget nearly used).
