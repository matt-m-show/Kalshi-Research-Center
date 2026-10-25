// App shell: posture pill, tabs, actions menu, live event feed.

import * as api from "./api.js";
import { confirmDialog, html, onAction, render, toast } from "./util.js";
import * as overview from "./views/overview.js";
import * as approvals from "./views/approvals.js";
import * as positions from "./views/positions.js";
import * as runs from "./views/runs.js";
import * as settings from "./views/settings.js";

const VIEWS = { overview, approvals, positions, runs, settings };

export const app = {
  status: null,
  feed: [],
  tab: "overview",
  view: document.getElementById("view"),

  async refreshStatus() {
    try {
      this.status = await api.get("/api/status");
      paintTopbar(this.status);
    } catch (e) { /* offline: keep last state */ }
    return this.status;
  },

  async show(tab = this.tab) {
    if (!VIEWS[tab]) tab = "overview";
    this.tab = tab;
    history.replaceState(null, "", `#${tab}`);
    document.querySelectorAll(".tabs button").forEach((b) => {
      if (b.dataset.tab === tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    try {
      await VIEWS[tab].show(this.view, this);
    } catch (e) {
      render(this.view, html`<div class="banner">Couldn't load this view: ${e.message}</div>`);
    }
  },

  async refreshPending() {
    try {
      const { pending } = await api.get("/api/pending");
      const badge = document.getElementById("pending-badge");
      badge.textContent = pending.length;
      badge.classList.toggle("hidden", pending.length === 0);
    } catch { /* ignore */ }
  },
};

// ── Top bar ───────────────────────────────────────────────────────────────

function posture(s) {
  if (s.halted) return ["halted", "⏸", "HALTED"];
  const live = s.env === "prod";
  const mode = { autonomous: "AUTO", hitl: "APPROVAL", paper: "PAPER" }[s.mode] || s.mode.toUpperCase();
  if (!live) return ["demo", "", `DEMO · ${mode}`];
  if (s.mode === "autonomous") return ["live-auto", "", "LIVE · AUTO"];
  if (s.mode === "hitl") return ["live-hitl", "", "LIVE · APPROVAL"];
  return ["paper", "", "LIVE · PAPER"];
}

function paintTopbar(s) {
  const [cls, , label] = posture(s);
  const el = document.getElementById("posture");
  el.className = `posture ${cls}`;
  el.title = s.halted ? `Halted: ${s.halt_reason || ""}` : explainPosture(s);
  render(el, html`<span class="dot"></span><span>${label}</span>`);
  const conn = document.getElementById("conn");
  conn.className = `conn ${s.kalshi_connected ? "ok" : "bad"}`;
  conn.title = s.kalshi_connected ? "Kalshi API reachable" : "Kalshi API unreachable";
}

function explainPosture(s) {
  if (s.mode === "autonomous") return "Orders are placed automatically after QA.";
  if (s.mode === "hitl") return "Each trade waits for your approval.";
  return "Trades are recorded but never sent.";
}

// ── Actions menu ──────────────────────────────────────────────────────────

function closeMenu() {
  document.getElementById("menu-list").classList.add("hidden");
  document.querySelector("[data-action=menu]").setAttribute("aria-expanded", "false");
}

onAction("menu", (btn) => {
  const list = document.getElementById("menu-list");
  if (!list.classList.contains("hidden")) { closeMenu(); return; }
  const s = app.status || {};
  render(list, html`
    <button data-action="run-now">Run research now</button>
    <button data-action="sync-orders">Sync orders &amp; fills</button>
    <button data-action="${s.agents_running ? "stop" : "start"}">${s.agents_running ? "Stop schedule" : "Start schedule"}</button>
    <hr>
    ${s.halted ? html`<button data-action="resume">Resume trading</button>`
      : html`<button data-action="halt" class="danger-text">Halt trading</button>`}
    <button data-action="flatten" class="danger-text">Flatten everything…</button>
    <hr>
    <button data-action="theme">Toggle light / dark</button>
    ${s.password_set ? html`<button data-action="logout">Sign out</button>` : ""}`);
  list.classList.remove("hidden");
  btn.setAttribute("aria-expanded", "true");
});
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closeMenu(); });

async function act(path, okMsg) {
  closeMenu();
  try {
    const r = await api.post(path);
    if (r.success === false) toast(r.message || r.error || "Failed", { error: true });
    else if (okMsg) toast(okMsg);
  } catch (e) { toast(e.message, { error: true }); }
  await app.refreshStatus();
  app.show();
}

onAction("run-now", () => act("/api/agents/run-now", "Research run started — watch the Runs tab."));
onAction("sync-orders", () => act("/api/orders/sync", "Orders synced."));
onAction("start", () => act("/api/agents/start", "Schedule started."));
onAction("stop", () => act("/api/agents/stop", "Schedule stopped."));
onAction("resume", () => act("/api/resume", "Trading resumed."));
onAction("halt", async () => {
  closeMenu();
  if (await confirmDialog({ title: "Halt trading?", body: "No new research or orders until you resume. Resting orders stay on Kalshi.", confirm: "Halt", danger: true }))
    act("/api/halt", "Trading halted.");
});
onAction("flatten", async () => {
  closeMenu();
  if (await confirmDialog({
    title: "Flatten everything?",
    body: "Halts trading, cancels every resting order, and sells every position immediately at whatever price the market gives. This can lock in losses.",
    confirm: "Flatten", danger: true, typed: "FLATTEN",
  })) act("/api/flatten", "Flatten requested — check Positions.");
});
onAction("theme", () => {
  closeMenu();
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("theme", root.dataset.theme); } catch { /* storage unavailable */ }
  app.show();
});
onAction("logout", async () => { await api.post("/api/logout"); location.href = "/login"; });
onAction("tab", (btn) => app.show(btn.dataset.tab));

// ── Live events ───────────────────────────────────────────────────────────

const REFRESH_ON = {
  run_finished: ["overview", "runs", "approvals"], pending_approval: ["approvals"],
  order_submitted: ["overview", "positions"], decision: ["runs"], assessment: ["runs"],
  exit: ["overview", "positions"], thesis_invalidated: ["positions"],
  system_halted: ["overview"], system_resumed: ["overview"], agents_started: ["overview"],
  agents_stopped: ["overview"],
};

let socket = null;
setInterval(() => { if (socket && socket.readyState === 1) socket.send("ping"); }, 25000);

function connect(delay = 1000) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket = ws;
  ws.onopen = () => { delay = 1000; };
  ws.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "init") { app.feed = (ev.feed || []).slice().reverse(); return; }
    app.feed.unshift(ev);
    app.feed = app.feed.slice(0, 200);
    if (["system_halted", "system_resumed", "agents_started", "agents_stopped", "agent_status"].includes(ev.type)) app.refreshStatus();
    if (ev.type === "pending_approval" || ev.type === "run_finished") app.refreshPending();
    if (ev.type === "run_finished") toast(`Research run ${ev.status}: ${ev.approved || 0} approved, ${ev.submitted || 0} sent`);
    if (ev.type === "exit") toast(`Exit ${ev.ticker}: sold ${ev.filled}/${ev.contracts}`);
    if (ev.type === "thesis_invalidated") toast(`News may break the thesis on ${ev.title || ev.ticker}`, { error: true });
    if ((REFRESH_ON[ev.type] || []).includes(app.tab) && !document.querySelector("dialog[open]")) {
      clearTimeout(app._refreshTimer);
      app._refreshTimer = setTimeout(() => app.show(), 400);
    }
    if (app.tab === "overview") overview.onEvent?.(ev, app);
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { location.href = "/login"; return; }
    setTimeout(() => connect(Math.min(delay * 2, 30000)), delay);
  };
}

// ── Boot ──────────────────────────────────────────────────────────────────

(function boot() {
  try {
    const t = localStorage.getItem("theme");
    if (t) document.documentElement.dataset.theme = t;
  } catch { /* storage unavailable */ }
  const start = (location.hash || "#overview").slice(1);
  app.refreshStatus().then(() => app.show(start));
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (tab && tab !== app.tab) app.show(tab);
  });
  app.refreshPending();
  connect();
  setInterval(() => app.refreshStatus(), 30000);
  setInterval(() => app.refreshPending(), 45000);
})();
