---
description: Switch between paper, approval (hitl) and autonomous mode
argument-hint: paper | hitl | autonomous
---

Modes:
- **paper** — research and QA run; orders are recorded but never sent.
- **hitl** — each trade waits in the dashboard's Approvals tab.
- **autonomous** — trades that pass QA are sent immediately.

The Kalshi environment (demo or prod) is separate: it's set by `KALSHI_ENV` when the dashboard starts.

Show the current state first:

```bash
curl -s http://127.0.0.1:8000/api/status
```

If switching to `autonomous` while `env` is `prod`, state plainly that real-money orders will be placed without asking, and get explicit confirmation. Then:

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" http://127.0.0.1:8000/api/mode/$ARGUMENTS
```
