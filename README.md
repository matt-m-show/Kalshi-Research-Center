# Kalshi Multi-Agent Trading System

An autonomous prediction market trading system for [Kalshi](https://kalshi.com) powered by Claude. A research agent identifies markets with mispriced probabilities, a deterministic QA/risk engine validates every recommendation, and the execution layer submits orders automatically — all surfaced through a real-time web dashboard.

> **Status:** Active development / experimental. Use the demo environment first.

---

## How it works

```
Research Agent (Claude Sonnet)
  → finds markets where estimated probability ≠ market price
  → cites tier-1 sources, sets anti-anchored probability estimates

QA / Risk Engine (deterministic)
  → 11 hard gates (position size, drawdown, liquidity, price staleness, …)
  → Kelly position sizing (¼-Kelly by default)
  → soft checks (volatility, spread, category concentration)

Execute Layer
  → pre-flight price check immediately before submission
  → idempotent order placement via client_order_id
  → full audit trail in SQLite ledger

Dashboard (FastAPI + vanilla JS)
  → live WebSocket feed, pending approvals, portfolio view, settings editor
```

Operational modes: **Paper** (demo API, no real money) → **HITL** (live, manual approval per trade) → **Autonomous** (live, fully automated).

---

## Prerequisites

- Python 3.12+ **or** Docker
- A [Kalshi](https://kalshi.com) account with API access enabled
- An [Anthropic API key](https://console.anthropic.com)
- *(Optional but recommended)* A [Tavily](https://tavily.com) API key for reliable web search

---

## Quick start — Docker (recommended)

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/kalshi-agents.git
cd kalshi-agents
cp .env.example .env
```

Edit `.env` with your credentials (see [Credentials setup](#credentials-setup) below).

### 2. Start the dashboard

```bash
docker compose up -d
```

Open **http://localhost:8000** in your browser.

### 3. Run a research cycle

Click **▶ Run Now** in the dashboard header, or click **Start Agents** to begin running automatically every 30 minutes.

---

## Quick start — Native Python

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/kalshi-agents.git
cd kalshi-agents
pip install -e ".[dev]"
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY and Tavily key
```

For Kalshi credentials on Windows/Mac/Linux, the native path uses your OS keychain:

```bash
python -m src.kalshi_client.auth setup --env demo --key-id YOUR-API-KEY-UUID
# Paste your RSA private key PEM at the prompt, then press Enter twice
```

Alternatively, set `KALSHI_KEY_ID` and `KALSHI_PEM_B64` in `.env` (see below).

### 3. Load environment and run

**Windows (PowerShell):**
```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^([^#][^=]*)=(.*)$') { [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2]) }
}
uvicorn src.web.app:app --reload --port 8000
```

**Mac/Linux:**
```bash
export $(grep -v '^#' .env | xargs)
uvicorn src.web.app:app --reload --port 8000
```

Open **http://localhost:8000**.

---

## Credentials setup

### Getting your Kalshi API credentials

1. Log into [Kalshi](https://kalshi.com) → **Settings → API Keys**
2. Generate a new RSA keypair — Kalshi gives you a UUID (Key ID) and a PEM private key
3. Start with `KALSHI_ENV=demo` (paper trading against the demo API)

### Storing credentials for Docker

The easiest Docker approach is base64-encoding the PEM into an env var:

**Linux/Mac:**
```bash
KALSHI_PEM_B64=$(base64 -w 0 ~/.kalshi/kalshi-demo.pem)
echo "KALSHI_PEM_B64=$KALSHI_PEM_B64" >> .env
```

**Windows (PowerShell):**
```powershell
$pem = [Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.kalshi\kalshi-demo.pem"))
Add-Content .env "KALSHI_PEM_B64=$pem"
```

Then set `KALSHI_KEY_ID=your-uuid` in `.env`.

### Storing credentials natively (Windows Credential Manager / macOS Keychain)

```bash
python -m src.kalshi_client.auth setup --env demo --key-id YOUR-UUID
# Run again with --env prod when ready for live trading
```

---

## Configuration

All configuration lives in `config/`. Changes take effect on the next research cycle — no restart required (the dashboard Settings tab can edit these directly).

### `config/risk.yaml` — Position sizing and risk limits

| Setting | Description | Default |
|---------|-------------|---------|
| `bankroll.total_capital_usd` | Total account capital | 100 |
| `per_trade.max_loss_usd` | Max loss per trade | 50 |
| `per_trade.max_size_contracts` | Max contracts per order | 50 |
| `per_day.max_new_trades` | Max new positions per day | 5 |
| `liquidity_floors.min_open_interest` | Skip markets with OI below this | 0 |
| `preflight.max_slippage_bps` | Abort order if price drifted this far since QA approval | 1500 |

### `config/universe.yaml` — Which markets to trade

```yaml
categories:
  include:
    - KXNBA      # NBA
    - KXNHL      # NHL
    - KXBTC      # Bitcoin price
    - KXFED      # Fed rate decisions
  exclude: []

time_filters:
  min_hours_to_resolution: 1

min_edge_bps: 200   # skip markets with < 200bps estimated edge
```

### `config/runtime.yaml` — Scheduling

```yaml
research:
  interval_minutes: 30
  max_recommendations_per_run: 5
```

---

## Dashboard

| Section | Description |
|---------|-------------|
| **Overview** | System stats, live Kalshi account balance and positions |
| **Pending Approval** | Trades queued for manual review (HITL mode) |
| **Recommendations** | Full history of all research agent output with QA verdicts |
| **Live Feed** | Real-time WebSocket event stream |
| **Settings** | Edit all config files without touching the filesystem |

### Operational modes

Switch modes from the **HITL / AUTO** button in the header:

| Mode | Behavior |
|------|----------|
| **Paper** | Trades recorded but never sent to Kalshi. Safe to experiment. |
| **HITL** | Each QA-approved trade appears in Pending Approval; you click ✓ or ✕. |
| **Autonomous** | Trades submit automatically after passing QA and pre-flight. |

---

## Security notes

- **The private key never enters an LLM prompt or log file.** `src/kalshi_client/auth.py` reads it once at call time and uses it only for RSA-PSS request signing.
- `KALSHI_ENV=demo` uses Kalshi's paper trading API — no real money is ever touched until you explicitly set `KALSHI_ENV=prod`.
- **The system cannot initiate deposits.** It can only place and cancel orders using the funds already in your Kalshi account. Deposits require explicit action on the Kalshi website.
- Research Agent tools are read-only (`WebFetch`, `WebSearch` only). It has no ability to place orders.
- The audit log in `data/audit.log` is append-only JSONL — every order action is recorded.
- Demo and production credentials are stored under separate keychain entries.

---

## Running tests

```bash
pytest tests/ -v
```

---

## Architecture

```
src/
  orchestrator.py         Main pipeline: Research → QA → Execute
  kalshi_client/          Kalshi REST API client (auth, markets, orders, orderbooks)
  qa/
    rules.py              Deterministic risk gate engine (11 hard gates + 3 soft checks)
    kelly.py              Quarter-Kelly position sizing
  execute/
    orders.py             Pre-flight check + idempotent order placement
    hitl.py               Human-in-the-loop approval flow
  common/
    ledger.py             SQLite persistence for all artifacts
    schemas.py            Pydantic models for all data types
  web/
    app.py                FastAPI backend (REST + WebSocket)
    static/index.html     Single-page dashboard

config/                   YAML config files (edit via Settings tab or directly)
.claude/agents/           Research agent system prompt
data/                     Runtime data (gitignored)
```

---

## Cost

Each research cycle costs roughly **$0.10–0.25** in Anthropic API credits (Claude Sonnet, ~10 candidate markets, ~5–10 tool calls). At a 30-minute interval that's ~$5–12/day if running continuously. Run on-demand with **▶ Run Now** to control costs.

---

## License

MIT
