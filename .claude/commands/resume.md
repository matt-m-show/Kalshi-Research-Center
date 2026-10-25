---
description: Resume trading after a halt
---

With the dashboard running locally, first show why the system is halted:

```bash
curl -s http://127.0.0.1:8000/api/status
```

Tell the user the halt reason. If they still want to resume (and the cause, e.g. a kill switch, has been dealt with):

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" http://127.0.0.1:8000/api/resume
```

This also removes `data/HALT` if it exists.
