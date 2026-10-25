// Shared helpers. Everything rendered goes through `html`, which escapes every
// interpolated value unless it is explicitly wrapped in raw(). Model output and
// web text are untrusted, so never build markup by string concatenation.

export class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(String(s));

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

function interp(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(interp).join("");
  if (v === null || v === undefined || v === false) return "";
  return esc(v);
}

export function html(strings, ...values) {
  let out = "";
  strings.forEach((s, i) => { out += s + (i < values.length ? interp(values[i]) : ""); });
  return new Raw(out);
}

export function render(el, content) {
  el.innerHTML = content instanceof Raw ? content.s : esc(content ?? "");
}

/** An <a> to an external page — only http(s) URLs become links. */
export function link(url, text) {
  const ok = typeof url === "string" && /^https?:\/\//i.test(url);
  if (!ok) return html`<span>${text || url || ""}</span>`;
  return html`<a href="${url}" target="_blank" rel="noopener noreferrer">${text || url}</a>`;
}

// ── Formatting ────────────────────────────────────────────────────────────

export const usd = (cents, { sign = false } = {}) => {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  const v = cents / 100;
  const s = Math.abs(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
  if (v < 0) return "−" + s;
  return (sign && v > 0 ? "+" : "") + s;
};
export const dollars = (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toFixed(2));
export const cents = (c) => (c === null || c === undefined ? "—" : `${Math.round(c)}¢`);
export const pct = (p, digits = 0) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);
export const signCls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

export function parseTime(t) {
  if (!t) return null;
  // Server timestamps are naive UTC unless they carry an offset
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : t + "Z";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}
export function when(t) {
  const d = t instanceof Date ? t : parseTime(t);
  if (!d) return "—";
  const same = d.toDateString() === new Date().toDateString();
  return same ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
export function rel(t) {
  const d = t instanceof Date ? t : parseTime(t);
  if (!d) return "—";
  let s = Math.round((d - Date.now()) / 1000);
  const future = s > 0;
  s = Math.abs(s);
  const parts = s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m`
    : s < 86400 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${Math.round(s / 86400)}d`;
  return future ? `in ${parts}` : `${parts} ago`;
}

// ── UI bits ───────────────────────────────────────────────────────────────

export function toast(msg, { error = false, ms = 4000 } = {}) {
  const host = document.getElementById("toast");
  const div = document.createElement("div");
  div.textContent = msg;
  if (error) div.className = "err";
  host.appendChild(div);
  setTimeout(() => div.remove(), error ? ms * 2 : ms);
}

/** Promise<boolean>. Optional `typed` requires typing a word to confirm. */
export function confirmDialog({ title, body, confirm = "Confirm", danger = false, typed = null }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    render(dlg, html`
      <form method="dialog" class="stack">
        <h2>${title}</h2>
        <p class="ink2">${body}</p>
        ${typed ? html`<label class="field"><span>Type <b>${typed}</b> to confirm</span>
          <input type="text" name="typed" autocomplete="off"></label>` : ""}
        <div class="row" style="justify-content:flex-end">
          <button class="btn ghost" value="cancel">Cancel</button>
          <button class="btn ${danger ? "danger" : "primary"}" value="ok">${confirm}</button>
        </div>
      </form>`);
    document.body.appendChild(dlg);
    const input = dlg.querySelector("input[name=typed]");
    const okBtn = dlg.querySelector("button[value=ok]");
    if (input) {
      okBtn.disabled = true;
      input.addEventListener("input", () => { okBtn.disabled = input.value.trim().toUpperCase() !== typed; });
    }
    dlg.addEventListener("close", () => { resolve(dlg.returnValue === "ok"); dlg.remove(); });
    dlg.showModal();
  });
}

// One delegated click handler for the whole app: elements declare data-action.
const actions = {};
export function onAction(name, fn) { actions[name] = fn; }
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const fn = actions[el.dataset.action];
  if (!fn) return;
  if (!el.matches("input")) e.preventDefault();   // let radios/checkboxes toggle
  fn(el, e);
});
