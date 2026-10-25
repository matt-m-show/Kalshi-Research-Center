---
description: Deny a trade waiting in Approval mode
argument-hint: <rec_id> [reason]
---

With the dashboard running locally, deny the pending trade given in `$ARGUMENTS` (first word is the rec_id, the rest is an optional reason):

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" \
  "http://127.0.0.1:8000/api/deny/<rec_id>?reason=<url-encoded reason>"
```

Report whether it succeeded. Nothing is sent to Kalshi.
