// Positions: what's held, what's resting, how it's going.

import * as api from "../api.js";
import { barChart, lineChart, reliabilityChart } from "../charts.js";
import { cents, confirmDialog, html, onAction, parseTime, rel, render, signCls, toast, usd, when } from "../util.js";

const STATUS_CLS = { FILLED: "good", SUBMITTED: "info", PARTIALLY_FILLED: "info", CANCELLED: "", EXPIRED: "",
  FAILED: "bad", APPROVED: "warn", PENDING_APPROVAL: "warn" };

export async function show(el) {
  const [port, ordersResp, perf, settles] = await Promise.all([
    api.get("/api/portfolio"), api.get("/api/orders?limit=100"),
    api.get("/api/performance"), api.get("/api/settlements"),
  ]);
  const resting = ordersResp.orders.filter((o) => ["SUBMITTED", "PARTIALLY_FILLED"].includes(o.status));
  const history = ordersResp.orders.filter((o) => !["SUBMITTED", "PARTIALLY_FILLED"].includes(o.status)).slice(0, 30);
  const calib = Object.entries(perf.calibration_by_category || {})
    .map(([k, v]) => ({ label: k, value: v.brier, note: `${v.n} resolved` }))
    .sort((a, b) => a.value - b.value);

  render(el, html`
    ${!port.ok ? html`<div class="banner">Couldn't reach Kalshi: ${port.error}</div>` : ""}
    <div class="card">
      <div class="card-head"><h2 class="grow">Open positions</h2>
        <span class="muted small">marked at the current bid</span></div>
      ${port.positions?.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>Market</th><th>Side</th><th class="r">Contracts</th><th class="r">Entry / fair</th>
          <th class="r">Bid</th><th class="r">Unrealized</th><th class="hide-sm">Exit plan</th><th></th></tr></thead>
        <tbody>${port.positions.map((p) => html`<tr>
          <td>${p.title || p.ticker}<div class="mono muted">${p.ticker}</div>
            ${p.close_time ? html`<div class="small muted">closes ${rel(p.close_time)}</div>` : ""}</td>
          <td><span class="chip side-${p.side.toLowerCase()}">${p.side}</span></td>
          <td class="r num">${p.contracts}<div class="small muted">${usd(p.exposure_cents)}</div></td>
          <td class="r num">${p.entry_cents != null ? `${cents(p.entry_cents)} / ${cents(p.fair_cents)}` : "—"}</td>
          <td class="r num">${cents(p.mark_cents)}</td>
          <td class="r num ${signCls(p.unrealized_pnl_cents)}">${usd(p.unrealized_pnl_cents, { sign: true })}</td>
          <td class="hide-sm small">${p.managed
            ? html`${p.take_profit_cents ? html`<div>take profit ≥ ${cents(p.take_profit_cents)}</div>` : ""}
                ${p.stop_loss_cents ? html`<div>stop loss ≤ ${cents(p.stop_loss_cents)}</div>` : ""}
                ${p.invalidation_event ? html`<div class="muted">watching: ${p.invalidation_event}</div>` : ""}`
            : html`<span class="muted">not opened by this system</span>`}</td>
          <td class="r"><button class="btn sm" data-action="exit" data-ticker="${p.ticker}" data-title="${p.title || p.ticker}"
            data-bid="${p.mark_cents ?? ""}">Sell now</button></td></tr>`)}</tbody></table></div>`
        : html`<div class="empty">No open positions.</div>`}
    </div>

    <div class="card section">
      <div class="card-head"><h2 class="grow">Resting orders</h2>
        <span class="muted small">post-only bids; repriced up to their max when outbid</span></div>
      ${resting.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>Market</th><th>Side</th><th class="r">Bid / max</th><th class="r">Filled</th>
          <th class="hide-sm">Expires</th><th></th></tr></thead>
        <tbody>${resting.map((o) => html`<tr>
          <td>${o.title || o.market}<div class="mono muted">${o.market}</div></td>
          <td><span class="chip side-${o.side.toLowerCase()}">${o.action === "sell" ? "SELL " : ""}${o.side}</span></td>
          <td class="r num">${cents(o.price)} / ${cents(o.max_price)}</td>
          <td class="r num">${o.filled} / ${o.size}</td>
          <td class="hide-sm muted">${o.expires_at ? rel(o.expires_at) : "—"}</td>
          <td class="r"><button class="btn sm" data-action="cancel-order" data-id="${o.order_id}">Cancel</button></td></tr>`)}
        </tbody></table></div>`
        : html`<div class="empty">No resting orders.</div>`}
    </div>

    <div class="grid two section">
      <div class="card"><div class="card-head"><h2>Realized P&amp;L vs API spend</h2></div><div id="pnl-chart"></div></div>
      <div class="card"><div class="card-head"><h2>Calibration</h2></div><div id="rel-chart"></div></div>
    </div>
    <div class="card section"><div class="card-head"><h2 class="grow">Forecast accuracy by series</h2>
      <span class="muted small">Brier score, lower is better</span></div><div id="brier-chart"></div></div>

    <div class="card section">
      <div class="card-head"><h2>Closed positions</h2></div>
      ${settles.settlements.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>Market</th><th>Result</th><th class="r">Cost</th><th class="r">Payout</th><th class="r">Fees</th>
          <th class="r">P&amp;L</th><th class="hide-sm">Settled</th></tr></thead>
        <tbody>${settles.settlements.slice().reverse().map((s) => html`<tr>
          <td class="mono">${s.market_ticker.split("#")[0]}</td>
          <td>${s.result === "exit" ? "Sold early" : s.result.toUpperCase()}</td>
          <td class="r num">${usd(s.cost_cents)}</td><td class="r num">${usd(s.revenue_cents)}</td>
          <td class="r num">${usd(s.fee_cents)}</td>
          <td class="r num ${signCls(s.pnl_cents)}">${usd(s.pnl_cents, { sign: true })}</td>
          <td class="hide-sm muted">${when(s.settled_at)}</td></tr>`)}</tbody></table></div>`
        : html`<div class="empty">Nothing has settled yet.</div>`}
    </div>

    <div class="card section">
      <div class="card-head"><h2>Order history</h2></div>
      ${history.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>Market</th><th>Side</th><th class="r">Price</th><th class="r">Filled</th><th>Status</th>
          <th class="hide-sm">Updated</th></tr></thead>
        <tbody>${history.map((o) => html`<tr>
          <td>${o.title || o.market}<div class="mono muted">${o.market}</div></td>
          <td>${o.action === "sell" ? "SELL " : ""}${o.side}</td>
          <td class="r num">${cents(o.avg_fill_price ?? o.price)}</td><td class="r num">${o.filled} / ${o.size}</td>
          <td><span class="status ${STATUS_CLS[o.status] || ""}">${o.status.replace("_", " ").toLowerCase()}</span>
            ${o.status_reason ? html`<div class="small muted">${o.status_reason}</div>` : ""}</td>
          <td class="hide-sm muted">${when(o.updated_at)}</td></tr>`)}</tbody></table></div>`
        : html`<div class="empty">No orders yet.</div>`}
    </div>`);

  lineChart(document.getElementById("pnl-chart"), {
    series: [
      { name: "Realized P&L", cls: "s1", points: perf.pnl_series.map((p) => ({ t: parseTime(p.t), y: p.pnl_cents })).filter((p) => p.t) },
      { name: "API spend", cls: "s2", points: perf.spend_series.map((p) => ({ t: parseTime(p.t), y: p.spend_cents })).filter((p) => p.t) },
    ],
    yFormat: (v) => usd(v),
    emptyText: "P&L and spend appear after the first run.",
  });
  reliabilityChart(document.getElementById("rel-chart"), perf.reliability || []);
  barChart(document.getElementById("brier-chart"), calib, {
    format: (v) => v.toFixed(3), ref: 0.25, refLabel: "coin flip", max: 0.3,
    emptyText: "Appears once assessed markets resolve.",
  });
}

onAction("exit", async (btn) => {
  const bid = btn.dataset.bid ? `${btn.dataset.bid}¢` : "the current bid";
  if (!await confirmDialog({
    title: `Sell ${btn.dataset.title} now?`,
    body: `Sells the whole position immediately at ${bid} or slightly below (up to the configured slippage). Anything that can't fill at that price stays open.`,
    confirm: "Sell now", danger: true,
  })) return;
  btn.disabled = true;
  try {
    const r = await api.post(`/api/exit/${encodeURIComponent(btn.dataset.ticker)}`);
    if (!r.success) throw new Error(r.error);
    toast(`Sold ${r.filled} of ${r.contracts} (floor ${r.floor}¢).`);
    show(document.getElementById("view"));
  } catch (e) { toast(e.message, { error: true }); btn.disabled = false; }
});

onAction("cancel-order", async (btn) => {
  if (!await confirmDialog({ title: "Cancel this order?", body: "Unfilled contracts are cancelled on Kalshi; anything already filled stays.", confirm: "Cancel order", danger: true })) return;
  btn.disabled = true;
  try {
    const r = await api.post(`/api/cancel/${btn.dataset.id}`);
    if (!r.success) throw new Error(r.error || "Cancel failed");
    toast("Order cancelled.");
    btn.closest("tr").remove();
  } catch (e) { toast(e.message, { error: true }); btn.disabled = false; }
});
