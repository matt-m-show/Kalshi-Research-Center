---
description: Approve a trade waiting in Approval mode (optionally fewer contracts or a lower max price)
argument-hint: <rec_id> [size] [max_price_cents]
---

The dashboard must be running locally (`.\start-web.ps1`). First show the pending trade so the user can confirm what they're approving:

```bash
curl -s http://127.0.0.1:8000/api/pending
```

Summarize the matching trade (market, side, QA size, our bid and max price, edge after fees, thesis, sources). Then, only after the user confirms, approve it with the arguments `$ARGUMENTS` — the first is the rec_id; an optional size and max price may only be lower than what QA approved:

```bash
curl -s -X POST -H "X-Requested-With: kalshi-dashboard" -H "Content-Type: application/json" \
  -d '{"size": <size or omit>, "max_price": <cents or omit>}' \
  http://127.0.0.1:8000/api/approve/<rec_id>
```

Report the result. The order is re-quoted against the live book at submission and is never sent above its max price; if pre-flight blocks it, say why.
