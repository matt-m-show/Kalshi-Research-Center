// Runs: every research cycle and what happened to each market in it.

import * as api from "../api.js";
import { html, link, onAction, pct, render, when } from "../util.js";

const STAGE_ORDER = ["submitted", "awaiting_approval", "paper_order", "preflight_blocked", "order_failed",
  "denied", "not_traded", "priced", "no_edge", "assessed", "skipped", "triaged_out", "selected",
  "shortlisted", "not_selected", "filtered", "eligible"];
const STAGE_LABEL = {
  submitted: "Order sent", awaiting_approval: "Awaiting approval", paper_order: "Paper order",
  preflight_blocked: "Blocked at pre-flight", order_failed: "Order failed", denied: "Denied by QA",
  not_traded: "Over per-run cap", priced: "Priced", no_edge: "No edge at market", assessed: "Assessed",
  skipped: "Skipped by model", triaged_out: "Dropped by local pre-pass", selected: "Selected",
  shortlisted: "Shortlisted", not_selected: "Ranked out", filtered: "Filtered", eligible: "Eligible",
};
const GOOD = new Set(["submitted", "awaiting_approval", "paper_order"]);
const BAD = new Set(["order_failed", "preflight_blocked"]);

let current = null;
let stageFilter = null;

export async function show(el) {
  const { runs } = await api.get("/api/runs?limit=40");
  if (!runs.length) {
    render(el, html`<div class="card empty">No research runs yet. Use <b>Run research now</b> from the menu.</div>`);
    return;
  }
  if (!current || !runs.some((r) => r.run_id === current)) current = runs[0].run_id;
  render(el, html`
    <div class="card">
      <div class="card-head"><h2>Research runs</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Started</th><th>Status</th><th class="r hide-sm">Considered</th><th class="r">Assessed</th>
          <th class="r">Priced</th><th class="r">Approved</th><th class="r">Sent</th><th class="r">Cost</th></tr></thead>
        <tbody>${runs.map((r) => html`<tr class="clickable ${r.run_id === current ? "selected" : ""}" data-action="run" data-id="${r.run_id}">
          <td>${when(r.started_at)}<div class="small muted">${r.trigger}</div></td>
          <td><span class="status ${r.status === "ok" ? "good" : r.status === "error" ? "bad" : r.status === "running" ? "info" : "warn"}">${r.status}</span></td>
          <td class="r num hide-sm">${r.candidate_count}</td><td class="r num">${r.assessments}</td>
          <td class="r num">${r.recommendations}</td><td class="r num">${r.approved}</td>
          <td class="r num">${r.submitted}</td><td class="r num">$${(r.cost_usd || 0).toFixed(3)}</td></tr>`)}
        </tbody></table></div>
    </div>
    <div id="run-detail" class="section"></div>`);
  await showDetail();
}

async function showDetail() {
  const el = document.getElementById("run-detail");
  const { run, assessments } = await api.get(`/api/runs/${current}`);
  const counts = {};
  run.candidates.forEach((c) => { counts[c.stage] = (counts[c.stage] || 0) + 1; });
  const stages = STAGE_ORDER.filter((s) => counts[s]);
  const rows = run.candidates
    .filter((c) => (stageFilter ? c.stage === stageFilter : c.stage !== "filtered" || stages.length === 1))
    .sort((a, b) => STAGE_ORDER.indexOf(a.stage) - STAGE_ORDER.indexOf(b.stage));
  const usage = Object.entries(run.usage || {});

  render(el, html`
    <div class="card">
      <div class="card-head"><h2 class="grow">Run ${when(run.started_at)}</h2>
        <span class="muted small">${run.model} · ${run.mode} mode</span></div>
      ${run.error ? html`<div class="banner">${run.error}</div>` : ""}
      ${run.notes.length ? html`<div class="stack small ink2">${run.notes.map((n) => html`<div>• ${n}</div>`)}</div>` : ""}
      ${usage.length ? html`<div class="row small muted section">${usage.map(([m, u]) => html`
        <span class="chip">${m}: ${u.calls} calls · ${(u.input_tokens + u.cache_read_tokens + u.cache_write_tokens).toLocaleString()} in /
          ${u.output_tokens.toLocaleString()} out${u.cost_usd ? ` · $${u.cost_usd.toFixed(3)}` : " · free"}</span>`)}</div>` : ""}
      <div class="row section">
        <button class="chip" data-action="stage" data-stage="">${stageFilter ? "All but filtered" : html`<b>All but filtered</b>`}</button>
        ${stages.map((s) => html`<button class="chip" data-action="stage" data-stage="${s}">
          ${stageFilter === s ? html`<b>${STAGE_LABEL[s] || s}</b>` : STAGE_LABEL[s] || s} · ${counts[s]}</button>`)}
      </div>
      <div class="table-wrap section"><table>
        <thead><tr><th>Market</th><th>Stage</th><th class="r">Fair YES</th><th class="hide-sm">Detail</th></tr></thead>
        <tbody>${rows.length ? rows.map((c) => html`<tr>
          <td>${c.title || c.ticker}<div class="mono muted">${c.ticker}</div></td>
          <td><span class="status ${GOOD.has(c.stage) ? "good" : BAD.has(c.stage) ? "bad" : c.stage === "denied" ? "warn" : ""}">${STAGE_LABEL[c.stage] || c.stage}</span>
            ${c.local_priority ? html`<div class="small muted">local priority ${c.local_priority}/5</div>` : ""}</td>
          <td class="r num">${c.fair_yes_prob != null ? pct(c.fair_yes_prob) : "—"}</td>
          <td class="hide-sm small ink2">${c.reason}</td></tr>`)
          : html`<tr><td colspan="4" class="empty">Nothing in this stage.</td></tr>`}</tbody>
      </table></div>
    </div>

    ${assessments.filter((a) => a.fair_yes_prob != null).length ? html`
    <div class="section"><h2>Assessments</h2></div>
    <div class="grid two">${assessments.filter((a) => a.fair_yes_prob != null).map((a) => html`
      <div class="card stack">
        <div class="row"><b class="grow">${a.title || a.market_ticker}</b>
          <span class="chip num">${pct(a.fair_yes_prob)} YES</span><span class="chip">${a.confidence}</span></div>
        <div class="mono muted">${a.market_ticker}${a.yes_bid != null ? ` · market ${a.yes_bid}/${a.yes_ask}¢` : ""}
          ${a.resolved_yes != null ? html` · <b>resolved ${a.resolved_yes ? "YES" : "NO"}</b>` : ""}</div>
        <p class="ink2">${a.thesis}</p>
        ${a.key_factors?.length ? html`<ul class="small ink2">${a.key_factors.map((k) => html`<li>${k}</li>`)}</ul>` : ""}
        ${a.invalidation_event ? html`<div class="small muted">Would be wrong if: ${a.invalidation_event}</div>` : ""}
        <div class="sources small">${(a.sources || []).map((s) => html`
          <div><span class="tier ${s.trust_tier === 1 ? "t1" : ""}">T${s.trust_tier}</span> ${link(s.url, s.title)}</div>`)}</div>
      </div>`)}</div>` : ""}`);
}

onAction("run", async (row) => {
  current = row.dataset.id;
  stageFilter = null;
  document.querySelectorAll("tr.clickable").forEach((r) => r.classList.toggle("selected", r.dataset.id === current));
  await showDetail();
});

onAction("stage", async (btn) => {
  stageFilter = btn.dataset.stage || null;
  await showDetail();
});
