# Kalshi Research Center

A research-and-trading system for [Kalshi](https://kalshi.com) prediction markets. A local model and Claude research markets and estimate fair probabilities; a deterministic engine prices, sizes and risk-checks every trade; orders are posted as maker bids, managed until they fill or expire, and exited on take-profit, stop-loss or thesis-breaking news. A web dashboard (works on a phone) shows everything and lets you approve trades, tune settings and see what each run cost.

> Experimental software that can place real-money orders. Start with the demo environment or Paper mode, and use small amounts.

---

## How it works

```
 every 30 min: any unassessed markets entering their research window?  (no LLM)
        │ yes                                       (also a full sweep on an interval)
        ▼
 Candidate selection   in-universe series · time/liquidity filters · not held ·
                       not assessed recently · ranked, capped per series/event
        ▼
 Local evidence pass   Ollama plans searches, runs them, digests facts with sources,
 (optional, free)      scores research priority — dead ends never reach Claude
        ▼
 Research (Claude)     records a fair YES probability per market with sources,
                       via strict tools; Kalshi prices are hidden by default so the
                       estimate is independent; fetched pages are pre-summarized locally
        ▼
 Pricing engine        picks the side with edge, computes the most it will pay
                       (fair − min edge − Kalshi fee), quotes a post-only bid inside
                       the spread                                   (no LLM)
        ▼
 QA / risk engine      fractional-Kelly size shrunk to fit every cap (per trade, per
                       day, deployed, per event/series, cash); hard gates on edge,
                       sources, drawdown, correlation, calibration    (no LLM)
        ▼
 Mode                  Paper: record only · Approval: wait for you · Autonomous: send
        ▼
 Order manager         every 5 min: sync orders/fills, reprice outbid bids up to their
                       max, cancel at TTL / before close, take-profit & stop-loss exits,
                       news watch, record settlements, score every estimate, kill switches
```

Every market's path through these stages is recorded on the run and visible in the dashboard's **Runs** tab, along with token usage and cost per model.

**Safety properties**

- The LLM never sets a price or size. It only supplies a probability; everything that touches money is deterministic code with tests.
- Orders are post-only bids that never cross the spread and never exceed their computed max price. Exits are immediate-or-cancel and reduce-only, so they can only shrink a position.
- The system can't deposit or withdraw. It can only trade with the cash already in your Kalshi account, and sizing never assumes more than that cash.
- Web pages, search results and model output are treated as untrusted: the research model is told to ignore instructions in them, the dashboard escapes all of it, and the page fetcher refuses private/local addresses.
- Halt from the dashboard, with `/halt`, or by creating `data/HALT`. Kill switches halt on consecutive losses, drawdown and API error bursts.
- Every recommendation, decision, order, fill, settlement, login and settings change is written to a hash-chained audit log.

---

## Quick start (Windows, native)

Requires Python 3.12+.

```powershell
git clone https://github.com/matt-m-show/kalshi-research-center.git
cd kalshi-research-center
py -m pip install -e ".[dev]"
copy .env.example .env      # then edit .env
.\start-web.ps1 -Env demo   # or -Env prod for real money
```

Open **http://127.0.0.1:8000**. The first time, go to **Settings → Markets** and pick the series you want to trade, choose a **Trading mode**, then use **☰ → Run research now** or **Start schedule**.

`start-web.ps1` reads `.env`, prompts for `ANTHROPIC_API_KEY` if it isn't set, and listens on 127.0.0.1 only.

### Kalshi credentials

1. On kalshi.com → **Settings → API Keys**, create a key. You get a key ID (UUID) and a private key (PEM).
2. Either put them in `.env` (`KALSHI_KEY_ID`, and `KALSHI_PEM_B64` = the PEM base64-encoded — see `.env.example`), or store them in Windows Credential Manager:

```powershell
py -m src.kalshi_client.auth setup --env prod --key-id <your-key-id>
```

`KALSHI_ENV=demo` uses Kalshi's demo exchange (separate demo keys); `KALSHI_ENV=prod` trades real money.

### Local model (optional, recommended)

Install [Ollama](https://ollama.com) and pull a model with tool support:

```powershell
ollama pull qwen3:8b
```

Set it under **Settings → Research, models & budget → Local Ollama model** (default `qwen3.5:4b`). The status line there shows whether it's reachable. Without a local model everything still works; Claude just does more of the searching, which costs more.

---

## Quick start (Docker)

```bash
cp .env.example .env    # set KALSHI_*, ANTHROPIC_API_KEY, and DASHBOARD_PASSWORD
docker compose up -d
```

The container publishes on 127.0.0.1:8000 only and reaches Ollama on the host at `host.docker.internal:11434`. Because requests into a container don't arrive from loopback, **Docker requires `DASHBOARD_PASSWORD`**.

---

## Phone access

The dashboard is a responsive web app you can add to your home screen. Don't expose it to the internet; use [Tailscale](https://tailscale.com) (free for personal use) so only your own devices can reach it:

1. Set a long random `DASHBOARD_PASSWORD` in `.env` and restart the dashboard.
2. Install Tailscale on the PC and your phone, signed in to the same account.
3. On the PC: `tailscale serve --bg 8000`. Tailscale prints an `https://<pc-name>.<tailnet>.ts.net` URL.
4. Open that URL on your phone, sign in with the password, and use **Share → Add to Home Screen**.

Access rules: direct requests from the PC itself are allowed without a password (set `DASHBOARD_TRUST_LOCALHOST=false` to require it everywhere). Anything else — including requests relayed by `tailscale serve` — needs a signed-in session. Sessions last 30 days; logins are rate-limited; state-changing API calls require a same-origin header, and responses carry a strict Content-Security-Policy.

---

## The dashboard

| Tab | What's there |
|---|---|
| **Overview** | Trading posture, cash, open value, realized P&L (after API costs), API spend vs budget, forecast accuracy, schedule and next runs, latest run, live activity |
| **Approvals** | In Approval mode: one card per trade — fair value vs the live bid/ask, edge after fees, size and max-price controls (can only be tightened), thesis, sources by trust tier |
| **Positions** | Open positions marked at the bid with their exit plan and a Sell-now button, resting orders with Cancel, P&L vs API spend, calibration chart, accuracy by series, closed positions, order history |
| **Runs** | Every research run: the funnel from considered to sent, per-model tokens and cost, each market's stage and the reason, and the full assessments with sources |
| **Settings** | Trading mode; plain-English changes (a local model proposes, you review the diff); market picker that browses Kalshi's series; risk presets and limits; execution and exit rules; research schedule, models and budget; change history with revert |

The **☰** menu has Run research now, Sync orders, Start/Stop schedule, Halt/Resume, Flatten everything, and the light/dark toggle.

### Claude Code commands

With the dashboard running, `/status`, `/run`, `/approve`, `/deny`, `/mode`, `/halt`, `/resume` and `/flatten` drive it from Claude Code through the same local API.

---

## Configuration

Everything is editable in **Settings**; the files live in `config/` and are validated on every save (a bad value is rejected, never reinterpreted). Percentages are fractions (`0.25` = 25%).

| File | Key settings |
|---|---|
| `risk.yaml` | bankroll and caps (per trade, per day, total deployed, per event/series), `per_trade.min_edge_cents`, Kelly fraction, `execution` (passive vs take, repricing, order TTL), `exits` (take-profit capture, stop-loss %, news watch), kill switches |
| `universe.yaml` | `categories.include` (Kalshi series tickers), hours/days-to-resolution filters |
| `runtime.yaml` | research schedule and `event_trigger` windows (with per-series overrides), candidates per run, `models` (Claude + local), `local_llm` switches, `budget` (monthly and per-run API caps) |

Series with `GAME` in the ticker (e.g. `KXNBAGAME`) are individual games; plain league series (e.g. `KXNBA`) are season-long futures.

## Cost

Claude only sees markets that passed the filters and the local pre-pass, with locally summarized pages and prompt caching on the growing conversation. The monthly budget (default $10) stops research for the rest of the month once reached; the per-run budget tells Claude to wrap up. Actual spend per run and per model is shown in **Runs**, and the Overview shows realized P&L net of API spend.

## Development

```powershell
py -m pytest            # 150+ tests; no network or API keys needed
py scheduler.py --diagnose --env prod     # which markets would be researched (no LLM, no orders)
py scheduler.py --sync-orders --env prod  # one order-manager pass
```

```
src/
  orchestrator.py        selection → local pre-pass → research → pricing → QA → act
  jobs.py                schedule: research sweep, window watch, order manager, report
  research/              Claude loop (agent.py), web tools, local assist, cost meter
  llm/ollama.py          local model client (schema-constrained JSON)
  qa/                    pricing.py (fees, side, max price, passive quote), rules.py, kelly.py
  execute/               orders.py (V2 order API), manager.py, exits.py, portfolio.py
  kalshi_client/         signed REST client and models for Kalshi's API v2
  common/                ledger.py (SQLite + audit chain), schemas.py, config.py
  performance/           metrics, settlement/calibration tracking, reports
  web/                   FastAPI app, auth, and the no-build dashboard in static/
```

## License

MIT
