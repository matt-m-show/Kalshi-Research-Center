// Settings: mode, plain-English changes, market picker, risk presets, history.

import * as api from "../api.js";
import { confirmDialog, html, onAction, render, toast, when } from "../util.js";

let cfg = null;           // {risk, universe, runtime}
let include = [];         // working copy of universe.categories.include
let browse = { category: "", q: "", frequency: "" };
let proposal = null;

// ── Field definitions ─────────────────────────────────────────────────────

const RISK_HEADLINE = [
  ["per_trade.max_loss_usd", "Max $ per trade", "Most a single trade can lose", "usd"],
  ["per_day.max_drawdown_usd", "Max $ lost per day", "New trades stop once today's losses reach this", "usd"],
  ["bankroll.max_total_deployed_usd", "Max $ at risk in total", "Open positions + resting bids", "usd"],
  ["per_trade.min_edge_cents", "Minimum edge (¢ per contract)", "After Kalshi fees, vs our fair value", "num"],
];
const RISK_ADVANCED = [
  ["bankroll.total_capital_usd", "Bankroll ($)", "Sizing never exceeds the cash actually in the account", "usd"],
  ["per_trade.max_position_pct_of_bankroll", "Max position (fraction of bankroll)", "0.10 = 10%", "frac"],
  ["per_trade.max_size_contracts", "Max contracts per trade", "", "int"],
  ["position_sizing.kelly_fraction", "Kelly fraction", "0.25 = quarter Kelly", "frac"],
  ["per_day.max_new_trades", "Max new trades per day", "", "int"],
  ["per_day.max_open_exposure_usd", "Max open exposure ($)", "", "usd"],
  ["concentration.max_pct_per_event", "Max per event (fraction)", "e.g. one game or one Fed meeting", "frac"],
  ["concentration.max_pct_per_category", "Max per series (fraction)", "", "frac"],
  ["concentration.max_correlated_positions", "Max positions per event", "", "int"],
  ["liquidity_floors.max_spread_cents", "Skip spreads wider than (¢)", "", "int"],
  ["liquidity_floors.min_open_interest", "Min open interest", "", "int"],
  ["liquidity_floors.min_daily_volume_contracts", "Min volume", "", "int"],
  ["kill_switches.consecutive_losses_halt", "Halt after N losses in a row", "", "int"],
  ["kill_switches.portfolio_drawdown_halt_pct", "Halt at drawdown (fraction of bankroll)", "", "frac"],
  ["research_quality.max_brier_score_for_category", "Stop trading a series above Brier", "0.25 = coin flip", "frac"],
];
const EXECUTION = [
  ["execution.entry_style", "Entry style", "", ["passive", "take"], { passive: "Bid inside the spread (maker)", take: "Buy at the ask (taker, fills now)" }],
  ["execution.reprice_interval_minutes", "Reprice an outbid order every (min)", "", "int"],
  ["execution.max_reprices", "Max reprices per order", "", "int"],
  ["execution.order_ttl_hours", "Cancel unfilled orders after (hours)", "", "num"],
  ["execution.cancel_before_close_minutes", "Cancel before market close (min)", "", "int"],
];
const EXITS = [
  ["exits.enabled", "Automatic exits", "Only for positions this system opened", "bool"],
  ["exits.take_profit_capture", "Take profit at (share of entry→fair)", "0.8 = sell once 80% of the expected move has happened", "frac"],
  ["exits.stop_loss_pct", "Stop loss at (drop from entry)", "0.5 = sell if the bid falls 50% below entry", "frac"],
  ["exits.stop_loss_min_hours_to_close", "No stop loss in the final (hours)", "", "num"],
  ["exits.max_slippage_cents", "Accept up to (¢) below the bid", "", "int"],
  ["exits.news_check_minutes", "Check news on positions every (min)", "0 = off; uses the local model", "int"],
  ["exits.on_invalidation", "When news breaks the thesis", "", ["alert", "exit"], { alert: "Alert me", exit: "Sell the position" }],
];
const RESEARCH = [
  ["research.event_trigger.enabled", "Research when markets enter their window", "Cheap check, no LLM until there's something new", "bool"],
  ["research.event_trigger.window_hours", "Research window (hours before close)", "e.g. 2 to 12 for games", "pair"],
  ["research.event_trigger.check_every_minutes", "Check every (minutes)", "", "int"],
  ["research.interval_minutes", "Full sweep every (minutes)", "When the schedule is running", "int"],
  ["research.max_candidates", "Markets per run sent to Claude", "", "int"],
  ["research.max_recommendations_per_run", "Max trades per run", "", "int"],
  ["research.reassess_after_hours", "Re-research a market after (hours)", "", "num"],
  ["research.effort", "Claude effort", "", ["low", "medium", "high"]],
  ["budget.monthly_usd", "Monthly API budget ($)", "Research stops for the month at this spend", "usd"],
  ["budget.per_run_usd", "Per-run API budget ($)", "Claude is told to wrap up at this spend", "usd"],
  ["models.research", "Claude model", "", "text"],
  ["models.local", "Local Ollama model", "e.g. qwen3.5:4b or qwen3:8b", "text"],
  ["local_llm.enabled", "Use the local model", "", "bool"],
  ["local_llm.evidence_prepass", "Local evidence pre-pass", "Searches and digests evidence before Claude", "bool"],
  ["local_llm.summarize_pages", "Local page summaries", "Shrinks pages Claude fetches", "bool"],
];

const PRESETS = {
  Conservative: { trade: 0.05, day: 0.10, deployed: 0.30, edge: 5, kelly: 0.15, trades: 3, pos: 0.05 },
  Balanced: { trade: 0.10, day: 0.20, deployed: 0.50, edge: 3, kelly: 0.25, trades: 5, pos: 0.10 },
  Aggressive: { trade: 0.20, day: 0.35, deployed: 0.80, edge: 2, kelly: 0.35, trades: 10, pos: 0.20 },
};

// ── Helpers ───────────────────────────────────────────────────────────────

const getPath = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function setPath(obj, path, v) {
  const keys = path.split(".");
  let o = obj;
  keys.slice(0, -1).forEach((k) => { o = o[k] ??= {}; });
  o[keys[keys.length - 1]] = v;
}
const fid = (section, path) => `f-${section}-${path.replace(/\./g, "_")}`;

function field(section, [path, label, hint, type, optLabels]) {
  const id = fid(section, path);
  const v = getPath(cfg[section], path);
  let input;
  if (Array.isArray(type)) {
    input = html`<select id="${id}">${type.map((o) => html`<option value="${o}" ${o === v ? "selected" : ""}>${optLabels?.[o] || o}</option>`)}</select>`;
  } else if (type === "bool") {
    input = html`<select id="${id}"><option value="true" ${v ? "selected" : ""}>On</option><option value="false" ${!v ? "selected" : ""}>Off</option></select>`;
  } else if (type === "text") {
    input = html`<input id="${id}" type="text" value="${v ?? ""}">`;
  } else if (type === "pair") {
    const [a, b] = Array.isArray(v) ? v : ["", ""];
    input = html`<span class="row"><input id="${id}-0" type="number" step="any" value="${a}" class="grow">
      <span class="muted">to</span><input id="${id}-1" type="number" step="any" value="${b}" class="grow"></span>`;
  } else {
    const step = type === "int" ? "1" : type === "frac" ? "0.01" : "any";
    input = html`<input id="${id}" type="number" step="${step}" value="${v ?? ""}" inputmode="decimal">`;
  }
  return html`<label class="field"><span>${label}</span>${input}${hint ? html`<small>${hint}</small>` : ""}</label>`;
}

function collect(section, defs) {
  const out = {};
  for (const [path, , , type] of defs) {
    if (type === "pair") {
      const a = document.getElementById(`${fid(section, path)}-0`), b = document.getElementById(`${fid(section, path)}-1`);
      if (!a || a.value === "" || b.value === "") continue;
      const v = [parseFloat(a.value), parseFloat(b.value)];
      if (JSON.stringify(v) !== JSON.stringify(getPath(cfg[section], path))) setPath(out, path, v);
      continue;
    }
    const el = document.getElementById(fid(section, path));
    if (!el) continue;
    let v = el.value;
    if (type === "bool") v = v === "true";
    else if (type === "text" || Array.isArray(type)) v = v.trim();
    else if (v === "") continue;
    else v = type === "int" ? parseInt(v, 10) : parseFloat(v);
    if (JSON.stringify(v) !== JSON.stringify(getPath(cfg[section], path))) setPath(out, path, v);
  }
  return out;
}

async function save(section, changes, label) {
  if (!Object.keys(changes).length) { toast("Nothing changed."); return; }
  const r = await api.post("/api/settings", { [section]: changes });
  if (!r.success) { toast(r.errors.join("; "), { error: true }); return; }
  toast(`${label} saved — applies from the next cycle.`);
  await load();
}

async function load() {
  cfg = await api.get("/api/settings");
  include = [...(cfg.universe?.categories?.include || [])];
  rerender();
}

let host = null;
let appRef = null;

// ── Render ────────────────────────────────────────────────────────────────

function rerender() {
  const s = appRef.status || {};
  render(host, html`
    <div class="card">
      <div class="card-head"><h2 class="grow">Trading mode</h2>
        <span class="muted small">Kalshi environment: <b>${(s.env || "demo").toUpperCase()}</b> (set by KALSHI_ENV at startup)</span></div>
      <div class="seg">
        ${[["paper", "Paper", "Research and QA run; orders are recorded but never sent."],
           ["hitl", "Approval", "Each trade waits in Approvals for you to send it."],
           ["autonomous", "Autonomous", "Trades that pass QA are sent immediately."]].map(([m, name, desc]) => html`
          <label><input type="radio" name="mode" value="${m}" data-action="mode" ${s.mode === m ? "checked" : ""}>
            <b>${name}</b><div class="small ink2">${desc}</div></label>`)}
      </div>
    </div>

    <div class="card section stack">
      <h2>Describe a change</h2>
      <p class="small ink2">Plain English, e.g. “only trade NBA games and Fed decisions, at most $5 per trade, and stop for
        the day after losing $10.” A local model proposes the change; nothing is saved until you apply it.</p>
      <textarea id="nl-text" placeholder="What should change?"></textarea>
      <div class="row"><button class="btn" data-action="nl-propose">Propose changes</button>
        <span id="nl-status" class="small muted"></span></div>
      <div id="nl-result"></div>
    </div>

    <div class="card section">
      <div class="card-head"><h2 class="grow">Markets</h2>
        <button class="btn primary sm" data-action="save-universe">Save markets</button></div>
      <h3>Trading these series</h3>
      <div class="row section" id="included">${includedChips()}</div>
      <div class="fields section">
        ${field("universe", ["time_filters.min_hours_to_resolution", "Skip markets resolving within (hours)", "", "num"])}
        ${field("universe", ["time_filters.max_days_to_resolution", "Skip markets resolving after (days)", "Long-dated markets tie up capital", "num"])}
      </div>
      <h3 class="section">Browse Kalshi series</h3>
      <div class="fields section">
        <label class="field"><span>Category</span><select id="br-cat"><option value="">All categories</option></select></label>
        <label class="field"><span>Frequency</span><select id="br-freq">
          ${["", "custom", "daily", "weekly", "monthly", "hourly", "one_off", "annual"].map((f) => html`<option value="${f}" ${browse.frequency === f ? "selected" : ""}>${f || "Any"}</option>`)}
        </select></label>
        <label class="field"><span>Search</span><input id="br-q" type="search" placeholder="NBA, Fed, CPI…" value="${browse.q}"></label>
      </div>
      <div id="br-results" class="section"><div class="empty">Loading series…</div></div>
    </div>

    <div class="card section">
      <div class="card-head"><h2 class="grow">Risk limits</h2>
        ${Object.keys(PRESETS).map((p) => html`<button class="btn sm" data-action="preset" data-preset="${p}">${p}</button>`)}
        <button class="btn primary sm" data-action="save-risk">Save risk</button></div>
      <p class="small muted">Presets fill the form from your bankroll (${`$${getPath(cfg.risk, "bankroll.total_capital_usd") ?? "?"}`}); review, then save.</p>
      <div class="fields">${RISK_HEADLINE.map((d) => field("risk", d))}</div>
      <details><summary>Advanced risk settings</summary><div class="fields">${RISK_ADVANCED.map((d) => field("risk", d))}</div></details>
      <h3 class="section">Order execution</h3>
      <div class="fields section">${EXECUTION.map((d) => field("risk", d))}</div>
      <h3 class="section">Exits</h3>
      <div class="fields section">${EXITS.map((d) => field("risk", d))}</div>
    </div>

    <div class="card section">
      <div class="card-head"><h2 class="grow">Research, models &amp; budget</h2>
        <button class="btn primary sm" data-action="save-runtime">Save</button></div>
      <div id="llm-status" class="small muted">Checking local model…</div>
      <div class="fields section">${RESEARCH.map((d) => field("runtime", d))}</div>
    </div>

    <div class="card section">
      <div class="card-head"><h2>Change history</h2></div>
      <div id="history"><div class="empty">Loading…</div></div>
    </div>`);

  loadSeries();
  loadHistory();
  api.get("/api/local-llm").then((l) => {
    const el = document.getElementById("llm-status");
    if (el) render(el, html`<span class="status ${l.ok ? "good" : "bad"}">${l.model}: ${l.ok ? "ready" : l.detail}</span>`);
  }).catch(() => {});
  ["br-cat", "br-freq"].forEach((id) => document.getElementById(id).addEventListener("change", (e) => {
    browse[id === "br-cat" ? "category" : "frequency"] = e.target.value; loadSeries();
  }));
  let t;
  document.getElementById("br-q").addEventListener("input", (e) => {
    clearTimeout(t); t = setTimeout(() => { browse.q = e.target.value.trim(); loadSeries(); }, 300);
  });
}

function includedChips() {
  if (!include.length) return html`<span class="muted">No series yet — add some below.</span>`;
  return html`${include.map((s) => html`<span class="chip mono">${s}<button aria-label="Remove ${s}" data-action="series-remove" data-s="${s}">✕</button></span>`)}`;
}

async function loadSeries() {
  const el = document.getElementById("br-results");
  if (!el) return;
  const qs = new URLSearchParams({ limit: "40" });
  Object.entries(browse).forEach(([k, v]) => { if (v) qs.set(k, v); });
  const r = await api.get(`/api/series?${qs}`);
  if (!r.ok) { render(el, html`<div class="banner">Couldn't load series: ${r.error}</div>`); return; }
  const sel = document.getElementById("br-cat");
  if (sel && sel.options.length <= 1) {
    r.categories.filter(([c]) => c).forEach(([c, n]) =>
      sel.add(new Option(`${c} (${n})`, c, false, c === browse.category)));
  }
  render(el, r.series.length ? html`<div class="table-wrap"><table>
    <thead><tr><th>Series</th><th class="hide-sm">Category</th><th class="hide-sm">Frequency</th><th class="r">Volume</th><th></th></tr></thead>
    <tbody>${r.series.map((s) => {
      const on = include.includes(s.ticker);
      return html`<tr><td>${s.title}<div class="mono muted">${s.ticker}</div></td>
        <td class="hide-sm">${s.category}</td><td class="hide-sm">${s.frequency}</td>
        <td class="r num">${Number(s.volume).toLocaleString()}</td>
        <td class="r"><button class="btn sm ${on ? "" : "primary"}" data-action="${on ? "series-remove" : "series-add"}" data-s="${s.ticker}">${on ? "Remove" : "Add"}</button></td></tr>`;
    })}</tbody></table></div>
    <div class="small muted">Showing ${r.series.length} of ${r.total}, busiest first. “custom”-frequency sports series are individual games.</div>`
    : html`<div class="empty">No series match.</div>`);
}

async function loadHistory() {
  const el = document.getElementById("history");
  const { history } = await api.get("/api/settings/history");
  render(el, history.length ? html`<div class="table-wrap"><table>
    <thead><tr><th>When</th><th>Change</th><th></th></tr></thead>
    <tbody>${history.map((h) => html`<tr><td class="muted">${when(h.ts)}<div class="small">${h.actor}${h.payload.note ? ` · ${h.payload.note}` : ""}</div></td>
      <td class="small">${Object.entries(h.payload.changes || {}).map(([k, c]) => html`<div><span class="mono">${h.payload.section}.${k}</span>:
        ${c && typeof c === "object" && "before" in c ? html`${JSON.stringify(c.before)} → <b>${JSON.stringify(c.after)}</b>` : JSON.stringify(c)}</div>`)}</td>
      <td class="r"><button class="btn sm" data-action="revert" data-seq="${h.seq}">Revert</button></td></tr>`)}</tbody></table></div>`
    : html`<div class="empty">No changes recorded yet.</div>`);
}

export async function show(el, app) {
  host = el;
  appRef = app;
  proposal = null;
  render(el, html`<div class="empty">Loading settings…</div>`);
  await load();
}

// ── Actions ───────────────────────────────────────────────────────────────

onAction("mode", async (input) => {
  const mode = input.value;
  const env = (appRef.status?.env || "demo").toUpperCase();
  const prev = appRef.status?.mode;
  const live = env === "PROD";
  const ok = mode !== "autonomous" ? true : await confirmDialog({
    title: "Switch to Autonomous?",
    body: `Trades that pass QA will be sent to Kalshi ${live ? "with real money " : ""}without asking you first.`,
    confirm: "Switch", danger: live, typed: live ? "LIVE" : null,
  });
  if (!ok) { document.querySelector(`input[name=mode][value=${prev}]`).checked = true; return; }
  const r = await api.post(`/api/mode/${mode}`);
  if (!r.success) { toast(r.error, { error: true }); return; }
  toast(`Mode: ${mode}`);
  await appRef.refreshStatus();
});

onAction("series-add", (btn) => {
  if (!include.includes(btn.dataset.s)) include.push(btn.dataset.s);
  render(document.getElementById("included"), includedChips());
  loadSeries();
});
onAction("series-remove", (btn) => {
  include = include.filter((s) => s !== btn.dataset.s);
  render(document.getElementById("included"), includedChips());
  loadSeries();
});
onAction("save-universe", async () => {
  const changes = collect("universe", [
    ["time_filters.min_hours_to_resolution", "", "", "num"], ["time_filters.max_days_to_resolution", "", "", "num"]]);
  changes.categories = { include };
  await save("universe", changes, "Markets");
});

onAction("preset", (btn) => {
  const p = PRESETS[btn.dataset.preset];
  const bank = Number(getPath(cfg.risk, "bankroll.total_capital_usd")) || 100;
  const put = (path, v) => { const el = document.getElementById(fid("risk", path)); if (el) el.value = v; };
  put("per_trade.max_loss_usd", +(bank * p.trade).toFixed(2));
  put("per_day.max_drawdown_usd", +(bank * p.day).toFixed(2));
  put("bankroll.max_total_deployed_usd", +(bank * p.deployed).toFixed(2));
  put("per_day.max_open_exposure_usd", +(bank * p.deployed).toFixed(2));
  put("per_trade.min_edge_cents", p.edge);
  put("position_sizing.kelly_fraction", p.kelly);
  put("per_day.max_new_trades", p.trades);
  put("per_trade.max_position_pct_of_bankroll", p.pos);
  toast(`${btn.dataset.preset} values filled in — review, then Save risk.`);
});
onAction("save-risk", () => save("risk", collect("risk", [...RISK_HEADLINE, ...RISK_ADVANCED, ...EXECUTION, ...EXITS]), "Risk"));
onAction("save-runtime", () => save("runtime", collect("runtime", RESEARCH), "Research settings"));

onAction("revert", async (btn) => {
  if (!await confirmDialog({ title: "Revert this change?", body: "The settings go back to their values before it.", confirm: "Revert" })) return;
  const r = await api.post(`/api/settings/revert/${btn.dataset.seq}`);
  if (!r.success) { toast(r.errors.join("; "), { error: true }); return; }
  toast("Reverted.");
  await load();
});

onAction("nl-propose", async (btn) => {
  const text = document.getElementById("nl-text").value;
  const status = document.getElementById("nl-status");
  btn.disabled = true;
  status.textContent = "Asking the local model…";
  try {
    const r = await api.post("/api/settings/interpret", { text });
    if (!r.ok) throw new Error(r.error);
    proposal = r;
    status.textContent = "";
    render(document.getElementById("nl-result"), html`
      ${r.changes.length ? html`<div class="table-wrap section"><table>
        <thead><tr><th>Setting</th><th>Now</th><th>Proposed</th><th class="hide-sm">Why</th></tr></thead>
        <tbody>${r.changes.map((c) => html`<tr><td class="mono">${c.path}</td><td>${JSON.stringify(c.before)}</td>
          <td><b>${JSON.stringify(c.after)}</b></td><td class="hide-sm small ink2">${c.reason}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">No changes proposed.</p>`}
      ${r.unsupported ? html`<p class="small ink2">Not possible with current settings: ${r.unsupported}</p>` : ""}
      ${r.rejected.length ? html`<p class="small muted">Ignored: ${r.rejected.join("; ")}</p>` : ""}
      ${r.errors.length ? html`<p class="small neg">Can't apply: ${r.errors.join("; ")}</p>` : ""}
      ${r.changes.length && !r.errors.length ? html`<button class="btn primary section" data-action="nl-apply">Apply these changes</button>` : ""}`);
  } catch (e) { status.textContent = ""; toast(e.message, { error: true }); }
  btn.disabled = false;
});
onAction("nl-apply", async () => {
  if (!proposal) return;
  const r = await api.post("/api/settings", proposal.apply);
  if (!r.success) { toast(r.errors.join("; "), { error: true }); return; }
  toast("Applied.");
  await load();
});
