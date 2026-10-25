// Hand-rolled SVG charts. One y-axis per chart; thin marks; recessive grid;
// text in ink tokens; every chart has a hover tooltip and a data table.

import { html, render } from "./util.js";

// ── Tooltip ───────────────────────────────────────────────────────────────

function tip() {
  let t = document.getElementById("tooltip");
  if (!t) {
    t = document.createElement("div");
    t.id = "tooltip";
    t.setAttribute("role", "tooltip");
    t.className = "hidden";
    document.body.appendChild(t);
  }
  return t;
}
export function showTip(x, y, content) {
  const t = tip();
  render(t, content);
  t.classList.remove("hidden");
  const w = t.offsetWidth, h = t.offsetHeight;
  t.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  t.style.top = Math.max(8, y - h - 10) + "px";
}
export function hideTip() { tip().classList.add("hidden"); }

// ── Scales ────────────────────────────────────────────────────────────────

function niceTicks(min, max, count = 4) {
  if (min === max) { max = min + 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}
const lin = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);

function observe(el, draw) {
  draw();
  if (el._ro) el._ro.disconnect();
  let last = el.clientWidth;
  el._ro = new ResizeObserver(() => { if (Math.abs(el.clientWidth - last) > 4) { last = el.clientWidth; draw(); } });
  el._ro.observe(el);
}

function dataTable(headers, rows) {
  return html`<details class="small"><summary>Show data</summary><div class="table-wrap"><table>
    <thead><tr>${headers.map((h) => html`<th>${h}</th>`)}</tr></thead>
    <tbody>${rows.map((r) => html`<tr>${r.map((c) => html`<td class="num">${c}</td>`)}</tr>`)}</tbody>
  </table></div></details>`;
}

// ── Line chart (time on x, one shared y unit) ─────────────────────────────

/**
 * series: [{name, cls: "s1"|"s2"|"s3", points: [{t: Date, y: number}]}], drawn as step lines
 * (cumulative values change only at events).
 */
export function lineChart(el, { series, yFormat, height = 220, emptyText = "No data yet" }) {
  const all = series.flatMap((s) => s.points);
  if (all.length === 0) { render(el, html`<div class="empty">${emptyText}</div>`); return; }

  observe(el, () => {
    const W = Math.max(280, el.clientWidth || 600), H = height;
    const m = { l: 56, r: 72, t: 10, b: 26 };
    const t0 = Math.min(...all.map((p) => +p.t)), t1 = Math.max(Date.now(), ...all.map((p) => +p.t));
    const yt = niceTicks(Math.min(0, ...all.map((p) => p.y)), Math.max(0, ...all.map((p) => p.y)));
    const x = lin(t0, t1, m.l, W - m.r), y = lin(yt[0], yt[yt.length - 1], H - m.b, m.t);

    const grid = yt.map((v) => html`
      <line class="${v === 0 ? "baseline" : "gridline"}" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
      <text class="tick num" x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${yFormat(v)}</text>`);
    const xt = [t0, (t0 + t1) / 2, t1].map((t) => html`
      <text class="tick" x="${x(t)}" y="${H - 6}" text-anchor="middle">${new Date(t).toLocaleDateString([], { month: "short", day: "numeric" })}</text>`);

    const paths = series.map((s) => {
      if (!s.points.length) return "";
      const pts = [...s.points].sort((a, b) => a.t - b.t);
      let d = `M${x(+pts[0].t)},${y(0)} V${y(pts[0].y)}`;
      for (let i = 1; i < pts.length; i++) d += ` H${x(+pts[i].t)} V${y(pts[i].y)}`;
      d += ` H${x(t1)}`;
      const lastY = pts[pts.length - 1].y;
      return html`<path class="line ${s.cls}" d="${d}" fill="none" stroke-width="2" stroke-linejoin="round"/>
        <text class="lbl" x="${W - m.r + 6}" y="${y(lastY) + 4}">${s.name}</text>`;
    });

    const legend = series.length > 1
      ? html`<div class="legend">${series.map((s) => html`<span><i class="sw ${s.cls}"></i>${s.name}</span>`)}</div>` : "";

    render(el, html`${legend}<div class="chart"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
        aria-label="${series.map((s) => s.name).join(" and ")} over time">
        ${grid}${xt}${paths}
        <line class="crosshair hidden" y1="${m.t}" y2="${H - m.b}"/>
        <rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/>
      </svg></div>
      ${dataTable(["Time", ...series.map((s) => s.name)],
        [...new Set(all.map((p) => +p.t))].sort().map((t) => [new Date(t).toLocaleString(),
          ...series.map((s) => { const p = valueAt(s.points, t); return p === null ? "—" : yFormat(p); })]))}`);

    const svg = el.querySelector("svg"), hit = svg.querySelector(".hit"), cross = svg.querySelector(".crosshair");
    hit.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      const t = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.classList.remove("hidden");
      showTip(ev.clientX, ev.clientY, html`<b>${new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</b>
        ${series.map((s) => { const v = valueAt(s.points, t); return html`<div><i class="sw ${s.cls}"></i> ${s.name}: <b class="num">${v === null ? "—" : yFormat(v)}</b></div>`; })}`);
    });
    hit.addEventListener("pointerleave", () => { hideTip(); cross.classList.add("hidden"); });
  });
}

function valueAt(points, t) {
  let v = null;
  for (const p of [...points].sort((a, b) => a.t - b.t)) { if (+p.t <= t) v = p.y; }
  return v === null && points.length ? 0 : v;
}

// ── Reliability diagram ───────────────────────────────────────────────────

/** bins: [{predicted, observed, n, bin}] — dots vs the perfect-calibration diagonal. */
export function reliabilityChart(el, bins) {
  if (!bins.length) { render(el, html`<div class="empty">Scores appear once assessed markets resolve.</div>`); return; }
  observe(el, () => {
    const W = Math.max(260, Math.min(el.clientWidth || 360, 420)), H = W * 0.8;
    const m = { l: 44, r: 12, t: 10, b: 34 };
    const x = lin(0, 1, m.l, W - m.r), y = lin(0, 1, H - m.b, m.t);
    const ticks = [0, 0.25, 0.5, 0.75, 1];
    const maxN = Math.max(...bins.map((b) => b.n));
    render(el, html`<div class="chart"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
        aria-label="Calibration: predicted versus observed frequency">
      ${ticks.map((v) => html`
        <line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
        <text class="tick num" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>
        <text class="tick num" x="${x(v)}" y="${H - m.b + 16}" text-anchor="middle">${Math.round(v * 100)}%</text>`)}
      <line class="ref" x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}"/>
      <text class="tick" x="${(m.l + W - m.r) / 2}" y="${H - 2}" text-anchor="middle">Predicted YES probability</text>
      ${bins.map((b, i) => html`<circle class="dot s1" data-i="${i}" cx="${x(b.predicted)}" cy="${y(b.observed)}"
          r="${5 + 4 * Math.sqrt(b.n / maxN)}" stroke-width="2"/>`)}
    </svg></div>
    <div class="small muted">Dots on the dashed line = well calibrated. Larger dots have more resolved markets.</div>
    ${dataTable(["Bin", "Predicted", "Observed", "Markets"],
      bins.map((b) => [b.bin, `${Math.round(b.predicted * 100)}%`, `${Math.round(b.observed * 100)}%`, b.n]))}`);
    el.querySelectorAll("circle.dot").forEach((c) => {
      c.addEventListener("pointerenter", (ev) => {
        const b = bins[+c.dataset.i];
        showTip(ev.clientX, ev.clientY, html`<b>${b.bin}</b><div>Predicted ${Math.round(b.predicted * 100)}%</div>
          <div>Resolved YES ${Math.round(b.observed * 100)}%</div><div>${b.n} markets</div>`);
      });
      c.addEventListener("pointerleave", hideTip);
    });
  });
}

// ── Horizontal bars ───────────────────────────────────────────────────────

/** rows: [{label, value, note}] sorted by caller; optional reference line. */
export function barChart(el, rows, { format, ref = null, refLabel = "", max = null, emptyText = "No data yet" }) {
  if (!rows.length) { render(el, html`<div class="empty">${emptyText}</div>`); return; }
  observe(el, () => {
    const W = Math.max(280, el.clientWidth || 500), rowH = 30;
    const m = { l: 110, r: 56, t: 6, b: 20 };
    const H = m.t + m.b + rows.length * rowH;
    const top = Math.max(max ?? 0, ref ?? 0, ...rows.map((r) => r.value)) * 1.05;
    const x = lin(0, top, m.l, W - m.r);
    render(el, html`<div class="chart"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">
      <line class="baseline" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>
      ${rows.map((r, i) => {
        const yy = m.t + i * rowH + 6, w = Math.max(2, x(r.value) - m.l), h = rowH - 12;
        return html`<text class="lbl" x="${m.l - 8}" y="${yy + h / 2 + 4}" text-anchor="end">${r.label}</text>
          <path class="bar s1" data-i="${i}" d="M${m.l},${yy} h${w - 4} a4,4 0 0 1 4,4 v${h - 8} a4,4 0 0 1 -4,4 h${-(w - 4)} z"/>
          <text class="lbl num" x="${m.l + w + 6}" y="${yy + h / 2 + 4}">${format(r.value)}</text>`;
      })}
      ${ref !== null ? html`<line class="ref" x1="${x(ref)}" x2="${x(ref)}" y1="${m.t}" y2="${H - m.b}"/>
        <text class="tick" x="${x(ref)}" y="${H - 4}" text-anchor="middle">${refLabel}</text>` : ""}
    </svg></div>
    ${dataTable(["", "Value", ""], rows.map((r) => [r.label, format(r.value), r.note || ""]))}`);
    el.querySelectorAll("path.bar").forEach((p) => {
      p.addEventListener("pointerenter", (ev) => {
        const r = rows[+p.dataset.i];
        showTip(ev.clientX, ev.clientY, html`<b>${r.label}</b><div>${format(r.value)}</div>${r.note ? html`<div>${r.note}</div>` : ""}`);
      });
      p.addEventListener("pointerleave", hideTip);
    });
  });
}

// ── Price bar for opportunity cards ───────────────────────────────────────

/**
 * Where our price sits relative to the market and our fair value, on the
 * 0–100¢ scale of the side being bought. Returns static SVG markup.
 */
export function priceBar({ bid, ask, price, max, fair }) {
  const W = 420, H = 64, m = { l: 14, r: 14 }, mid = 32;
  const x = lin(0, 100, m.l, W - m.r);
  const clampX = (v) => Math.min(W - m.r - 20, Math.max(m.l + 20, x(v)));
  const band = bid !== null && ask !== null
    ? html`<rect class="band" x="${x(bid)}" y="${mid - 7}" width="${Math.max(2, x(ask) - x(bid))}" height="14" rx="3"/>` : "";
  return html`<div class="chart pricebar"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="${`Market ${bid ?? "?"}–${ask ?? "?"}¢, our bid ${price}¢, max ${max}¢, fair ${Math.round(fair)}¢`}">
    <line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${mid}" y2="${mid}"/>
    ${[0, 50, 100].map((v) => html`<text class="tick num" x="${x(v)}" y="${mid + 4}" dx="${v === 0 ? -10 : v === 100 ? 10 : 0}" text-anchor="middle">${v === 50 ? "" : v}</text>`)}
    ${band}
    ${max !== null && max !== price ? html`<line class="ref" x1="${x(max)}" x2="${x(max)}" y1="${mid - 10}" y2="${mid + 10}"/>` : ""}
    <line class="mk-fair" x1="${x(fair)}" x2="${x(fair)}" y1="${mid - 14}" y2="${mid + 6}" stroke-width="2"/>
    <text class="lbl num" x="${clampX(fair)}" y="12" text-anchor="middle">fair ${Math.round(fair)}¢</text>
    <line class="mk-price" x1="${x(price)}" x2="${x(price)}" y1="${mid - 6}" y2="${mid + 14}" stroke-width="2"/>
    <text class="lbl num" x="${clampX(price)}" y="${H - 4}" text-anchor="middle">our bid ${price}¢</text>
  </svg></div>
  <div class="legend small"><span><i class="sw band"></i>market bid–ask</span><span><i class="sw s1"></i>our bid</span>
    <span><i class="sw s2"></i>fair value</span>${max !== null && max !== price ? html`<span>dashed = max we'll pay (${max}¢)</span>` : ""}</div>`;
}
