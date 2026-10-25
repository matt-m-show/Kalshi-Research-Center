// Overview: account, P&L vs API spend, schedule, latest run, live activity.

import * as api from "../api.js";
import { dollars, html, pct, rel, render, signCls, usd, when } from "../util.js";

const FEED_TEXT = {
  run_started: (e) => `Research run started (${e.trigger})`,
  candidates_selected: (e) => `Shortlisted ${e.selected} of ${e.eligible} eligible markets (${e.events} events)`,
  evidence: (e) => `Local pre-pass: ${e.event} — priority ${e.priority}, ${e.facts} facts`,
  assessment: (e) => `Estimated ${e.title || e.ticker}: ${Math.round((e.fair_yes_prob ?? 0) * 100)}% YES`,
  decision: (e) => `QA ${e.verdict} ${e.ticker} ${e.side} @ ${e.price}¢ ×${e.size}`,
  pending_approval: (e) => `Awaiting your approval: ${e.ticker}`,
  order_submitted: (e) => `Order sent: ${e.ticker || e.market} ${e.size ?? ""} @ ${e.price ?? "?"}¢`,
  trade_approved: (e) => `Approved ${e.market}`,
  trade_denied: (e) => `Denied (${e.reason || "no reason"})`,
  run_finished: (e) => `Run ${e.status}: ${e.assessments} assessed, ${e.approved} approved, ${e.submitted} sent · $${(e.cost_usd ?? 0).toFixed(3)}`,
  exit: (e) => `Exit ${e.title || e.ticker}: sold ${e.filled}/${e.contracts} — ${e.reason}`,
  thesis_invalidated: (e) => `⚠ News may break the thesis on ${e.title || e.ticker}: ${e.evidence}`,
  system_halted: (e) => `Halted: ${e.reason || ""}`,
  system_resumed: () => "Trading resumed",
  agents_started: () => "Schedule started",
  agents_stopped: () => "Schedule stopped",
  system: (e) => e.message,
};

function feedItems(feed) {
  const items = feed.filter((e) => FEED_TEXT[e.type]).slice(0, 40);
  if (!items.length) return html`<div class="empty">Activity from research runs and orders shows up here live.</div>`;
  return html`<ul class="feed">${items.map((e) => html`<li><time>${when(e.ts)}</time><span>${FEED_TEXT[e.type](e)}</span></li>`)}</ul>`;
}

export function onEvent(ev, app) {
  const el = document.getElementById("feed");
  if (el) render(el, feedItems(app.feed));
}

export async function show(el, app) {
  const [s, perf, port, runsResp] = await Promise.all([
    app.status ? Promise.resolve(app.status) : app.refreshStatus(),
    api.get("/api/performance"),
    api.get("/api/portfolio"),
    api.get("/api/runs?limit=1"),
  ]);
  const last = runsResp.runs[0];
  const spend = perf.api_spend_month_usd ?? 0, budget = perf.api_budget_month_usd;
  const realized = perf.realized_pnl_cents ?? 0;
  const net = realized - (perf.api_spend_total_usd ?? 0) * 100;

  render(el, html`
    ${s.halted ? html`<div class="banner"><b>Trading is halted.</b><span class="grow">${s.halt_reason || ""}</span>
      <button class="btn danger sm" data-action="resume">Resume</button></div>` : ""}
    ${!s.agents_running ? html`<div class="banner info"><span class="grow">The schedule is stopped — nothing runs automatically.
      Use <b>Run research now</b> for a single cycle, or start the schedule.</span>
      <button class="btn primary sm" data-action="start">Start schedule</button></div>` : ""}

    <div class="grid tiles">
      <div class="card tile"><div class="label">Cash available</div>
        <div class="value">${port.ok ? usd(port.balance_cents) : "—"}</div>
        <div class="sub" title="${port.ok ? "" : port.error}">${port.ok ? `${port.positions.length} positions · ${port.open_orders_count} resting orders` : "Kalshi unavailable — hover for details"}</div></div>
      <div class="card tile"><div class="label">Open position value</div>
        <div class="value">${port.ok ? usd(port.positions.reduce((a, p) => a + (p.value_cents ?? 0), 0)) : "—"}</div>
        <div class="sub ${signCls(port.unrealized_pnl_cents)}">${port.ok ? `${usd(port.unrealized_pnl_cents, { sign: true })} unrealized` : ""}</div></div>
      <div class="card tile"><div class="label">Realized P&amp;L</div>
        <div class="value ${signCls(realized)}">${usd(realized, { sign: true })}</div>
        <div class="sub">${perf.settled_markets} settled markets · after API costs ${usd(net, { sign: true })}</div></div>
      <div class="card tile"><div class="label">API spend this month</div>
        <div class="value">${dollars(spend)}</div>
        <div class="sub">${budget ? `of $${Number(budget).toFixed(2)} budget (${pct(spend / budget)})` : "no budget set"}</div></div>
      <div class="card tile"><div class="label">Forecast accuracy (Brier)</div>
        <div class="value">${perf.assessment_brier != null ? perf.assessment_brier.toFixed(3) : "—"}</div>
        <div class="sub">${perf.assessments_resolved} resolved estimates · 0.25 = coin flip</div></div>
    </div>

    <div class="grid two section">
      <div class="card">
        <div class="card-head"><h2 class="grow">Schedule</h2>
          <button class="btn sm" data-action="run-now" ${s.research_running ? "disabled" : ""}>${s.research_running ? "Research running…" : "Run research now"}</button></div>
        <table><tbody>
          <tr><td class="ink2">Status</td><td>${s.agents_running ? html`<span class="status good">Running</span>` : html`<span class="status">Stopped</span>`}</td></tr>
          <tr><td class="ink2">Next research run</td><td>${s.next_runs?.research ? `${when(s.next_runs.research)} (${rel(s.next_runs.research)})` : "—"}</td></tr>
          <tr><td class="ink2">Next order sync</td><td>${s.next_runs?.order_manager ? rel(s.next_runs.order_manager) : "—"}</td></tr>
          <tr><td class="ink2">Mode</td><td>${{ autonomous: "Autonomous — orders are placed automatically", hitl: "Approval — each trade waits for you", paper: "Paper — nothing is sent" }[s.mode]}</td></tr>
        </tbody></table>
      </div>
      <div class="card">
        <div class="card-head"><h2 class="grow">Latest research run</h2>
          ${last ? html`<button class="btn sm ghost" data-action="tab" data-tab="runs">Details</button>` : ""}</div>
        ${last ? html`
          <div class="row"><span class="status ${last.status === "ok" ? "good" : last.status === "error" ? "bad" : "warn"}">${last.status}</span>
            <span class="muted">${when(last.started_at)} · ${last.trigger}</span></div>
          <p class="num">${last.candidate_count} considered → ${last.assessments} assessed → ${last.recommendations} priced →
            ${last.approved} approved → ${last.submitted} sent · <b>$${(last.cost_usd || 0).toFixed(3)}</b></p>
          ${last.error ? html`<p class="neg small">${last.error}</p>` : ""}
          ${(last.notes || []).map((n) => html`<div class="small muted">${n}</div>`)}`
        : html`<div class="empty">No research runs yet.</div>`}
      </div>
    </div>

    <div class="card section">
      <div class="card-head"><h2>Activity</h2></div>
      <div id="feed">${feedItems(app.feed)}</div>
    </div>`);
}
