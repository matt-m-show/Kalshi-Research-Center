// Approvals: one card per trade waiting for sign-off in Approval (HITL) mode.

import * as api from "../api.js";
import { priceBar } from "../charts.js";
import { cents, confirmDialog, html, link, onAction, rel, render, toast, usd, when } from "../util.js";

let pending = [];

function card(p, live) {
  const sideBook = live?.ok ? live[p.side.toLowerCase()] : null;
  const fairSide = p.fair_yes_prob == null ? null
    : (p.side === "YES" ? p.fair_yes_prob : 1 - p.fair_yes_prob) * 100;
  const maxPrice = p.max_price ?? p.price;
  const cost = p.price * p.size;
  return html`
  <article class="card opp" id="opp-${p.rec_id}">
    <div class="row">
      <span class="chip side-${p.side.toLowerCase()}">${p.side}</span>
      <span class="title grow">${p.title || p.market}</span>
      ${live?.close_time ? html`<span class="muted small">closes ${rel(live.close_time)}</span>` : ""}
    </div>
    <div class="mono muted">${p.market}</div>

    ${priceBar({ bid: sideBook?.bid ?? null, ask: sideBook?.ask ?? null, price: p.price, max: maxPrice, fair: fairSide ?? p.price })}

    <div class="facts">
      <div class="fact"><div class="k">Fair value</div><div class="v num">${fairSide == null ? "—" : cents(fairSide)}</div></div>
      <div class="fact"><div class="k">Market bid / ask</div><div class="v num">${sideBook ? `${cents(sideBook.bid)} / ${cents(sideBook.ask)}` : live ? "—" : "…"}</div></div>
      <div class="fact"><div class="k">Edge after fees</div><div class="v num">${p.edge_cents == null ? "—" : `${p.edge_cents.toFixed(1)}¢`}</div></div>
      <div class="fact"><div class="k">Cost at ${p.price}¢</div><div class="v num">${usd(cost)}</div></div>
      <div class="fact"><div class="k">Pays if right</div><div class="v num">${usd(100 * p.size)}</div></div>
    </div>

    ${p.thesis ? html`<p class="thesis">${p.thesis}</p>` : ""}
    ${p.sources?.length ? html`<div class="sources small">${p.sources.map((s) => html`
      <div><span class="tier ${s.trust_tier === 1 ? "t1" : ""}">T${s.trust_tier}</span> ${link(s.url, s.title)}</div>`)}</div>` : ""}
    ${p.qa_notes ? html`<p class="small muted">QA: ${p.qa_notes}</p>` : ""}

    <div class="controls">
      <label class="field"><span>Contracts (QA approved ${p.size})</span>
        <span class="stepper"><button type="button" data-action="step" data-target="size-${p.rec_id}" data-d="-1">−</button>
        <input id="size-${p.rec_id}" type="number" min="1" max="${p.size}" value="${p.size}" inputmode="numeric">
        <button type="button" data-action="step" data-target="size-${p.rec_id}" data-d="1">+</button></span></label>
      <label class="field"><span>Max price (≤ ${maxPrice}¢)</span>
        <span class="stepper"><button type="button" data-action="step" data-target="max-${p.rec_id}" data-d="-1">−</button>
        <input id="max-${p.rec_id}" type="number" min="1" max="${maxPrice}" value="${maxPrice}" inputmode="numeric">
        <button type="button" data-action="step" data-target="max-${p.rec_id}" data-d="1">+</button></span></label>
      <span class="grow"></span>
      <button class="btn ghost" data-action="deny" data-rec="${p.rec_id}">Deny</button>
      <button class="btn primary" data-action="approve" data-rec="${p.rec_id}">Approve</button>
    </div>
    <p class="small muted">The order is posted as a bid inside the spread and only moves up toward your max price if
      outbid. Unfilled orders are cancelled ${p.expires_at ? `around ${when(p.expires_at)}` : "after the order TTL"}.</p>
  </article>`;
}

export async function show(el, app) {
  const s = app.status || await app.refreshStatus();
  ({ pending } = await api.get("/api/pending"));
  if (!pending.length) {
    render(el, html`<div class="card empty">
      ${s.mode === "hitl" ? "No trades are waiting for approval."
        : html`Approvals only appear in <b>Approval</b> mode. You're in <b>${s.mode}</b> mode — change it under Settings.`}
    </div>`);
    return;
  }
  render(el, html`<div class="grid">${pending.map((p) => card(p, null))}</div>`);
  // Fill in live prices card by card
  for (const p of pending) {
    api.get(`/api/market/${encodeURIComponent(p.market)}`).then((live) => {
      const node = document.getElementById(`opp-${p.rec_id}`);
      if (node) node.outerHTML = card(p, live).s;
    }).catch(() => {});
  }
}

onAction("step", (btn) => {
  const input = document.getElementById(btn.dataset.target);
  const v = Math.min(+input.max, Math.max(+input.min, (+input.value || 0) + +btn.dataset.d));
  input.value = v;
});

onAction("approve", async (btn) => {
  const rec = btn.dataset.rec;
  const p = pending.find((x) => x.rec_id === rec);
  const size = +document.getElementById(`size-${rec}`).value;
  const maxPrice = +document.getElementById(`max-${rec}`).value;
  const ok = await confirmDialog({
    title: `Buy ${p.side} on ${p.title || p.market}?`,
    body: `Up to ${size} contracts, never paying more than ${maxPrice}¢ each (at most ${usd(size * maxPrice)}).`,
    confirm: "Approve & send",
  });
  if (!ok) return;
  btn.disabled = true;
  try {
    const r = await api.post(`/api/approve/${rec}`, { size, max_price: maxPrice });
    if (!r.success) throw new Error(r.error);
    toast(`Sent: ${r.size} @ ${r.price}¢${r.note ? ` (${r.note})` : ""}`);
    document.getElementById(`opp-${rec}`)?.classList.add("done");
  } catch (e) { toast(e.message, { error: true }); btn.disabled = false; }
});

onAction("deny", async (btn) => {
  const rec = btn.dataset.rec;
  btn.disabled = true;
  try {
    const r = await api.post(`/api/deny/${rec}?reason=${encodeURIComponent("Denied in dashboard")}`);
    if (!r.success) throw new Error(r.error);
    document.getElementById(`opp-${rec}`)?.remove();
    toast("Denied.");
  } catch (e) { toast(e.message, { error: true }); btn.disabled = false; }
});
