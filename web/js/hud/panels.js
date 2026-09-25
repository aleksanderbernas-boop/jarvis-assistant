// DOM panels fed by backend events: weather, system bars, activity, tasks, captions, toasts,
// confirmation modal and the boot sequence. Nothing here invents data.

import { api } from "../ws.js";
import { bigWeatherIcon, icons, weatherIcon } from "../icons.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function timeAgo(date) {
  const s = Math.max(0, (Date.now() - new Date(date).getTime()) / 1000);
  if (s < 45) return "now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// ── Weather ─────────────────────────────────────────────────────────────
export async function loadWeather() {
  try {
    const res = await api("/api/weather");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      $("weather-cond").textContent = body.detail || "Weather unavailable";
      $("weather-place").textContent = "WEATHER";
      return;
    }
    const data = await res.json();
    $("weather-place").textContent = data.location;
    $("weather-temp").textContent = `${Math.round(data.current.temperature_c)}°C`;
    $("weather-cond").textContent = data.current.condition + (data.simulated ? " (simulated)" : "");
    $("weather-icon").innerHTML = bigWeatherIcon(data.current.code);
    $("weather-days").innerHTML = data.daily.slice(0, 4).map((d) => {
      const day = /^\d{4}-/.test(d.date) ? new Date(d.date).toLocaleDateString("en-GB", { weekday: "short" }) : d.date;
      return `<li><span>${esc(day)}</span><span>${Math.round(d.max_c)}° / ${Math.round(d.min_c)}°</span>${icons[weatherIcon(d.code)]}</li>`;
    }).join("");
  } catch {
    $("weather-cond").textContent = "Weather unavailable";
  }
}

// ── System metrics ──────────────────────────────────────────────────────
function setBar(key, percent, label) {
  const bar = $(`bar-${key}`), val = $(`val-${key}`);
  if (percent == null) { bar.style.width = "0"; val.textContent = "N/A"; val.title = "Not measurable on this system"; return; }
  bar.style.width = `${Math.min(100, percent)}%`;
  bar.classList.toggle("hot", percent > 85);
  val.textContent = label ?? `${Math.round(percent)}%`;
}

export function renderMetrics(m) {
  setBar("cpu", m.cpu_percent);
  setBar("gpu", m.gpu ? m.gpu.utilization_percent : null);
  setBar("ram", m.ram_percent);
  setBar("disk", m.disk_percent);
  $("core-uptime").textContent = formatUptime(m.uptime_s);
}

function formatUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : `${h}h ${m}m`;
}

// ── Activity feed ───────────────────────────────────────────────────────
export class ActivityFeed {
  constructor(el, limit = 30) { this.el = el; this.items = []; this.limit = limit; setInterval(() => this.render(), 30000); }

  add(text, kind = "info", timestamp = new Date().toISOString()) {
    const last = this.items[0];
    if (last && last.text === text && Date.now() - new Date(last.timestamp) < 3000) return;
    this.items.unshift({ text, kind, timestamp });
    this.items = this.items.slice(0, this.limit);
    this.render();
  }

  render() {
    this.el.innerHTML = this.items.map((i) =>
      `<li><i class="ico ${esc(i.kind)}"></i><span class="txt" title="${esc(i.text)}">${esc(i.text)}</span><span class="t">${timeAgo(i.timestamp)}</span></li>`
    ).join("");
  }
}

// ── Tasks ───────────────────────────────────────────────────────────────
export class TaskPanel {
  constructor(onCancel) {
    this.tasks = new Map();
    this.onCancel = onCancel;
    $("tasks-list").addEventListener("click", (e) => {
      const id = e.target.closest("[data-cancel]")?.dataset.cancel;
      if (id) this.onCancel(id);
    });
  }

  update(task) { this.tasks.set(task.id, task); this.render(); }
  reset(tasks) { this.tasks = new Map(tasks.map((t) => [t.id, t])); this.render(); }
  active() { return [...this.tasks.values()].filter((t) => !["completed", "failed", "cancelled"].includes(t.status)); }

  render() {
    const active = this.active();
    const recent = [...this.tasks.values()].filter((t) => !active.includes(t))
      .sort((a, b) => new Date(b.finished_at || 0) - new Date(a.finished_at || 0)).slice(0, 2);
    const show = [...active, ...recent];
    $("core-tasks").textContent = String(active.length);
    $("tasks-empty").classList.toggle("hidden", show.length > 0);
    $("tasks-list").classList.toggle("hidden", show.length === 0);
    $("tasks-list").innerHTML = `<div class="panel-title">TASKS</div>` + show.map((t) => {
      const done = ["completed", "failed", "cancelled"].includes(t.status);
      const cls = t.status === "failed" ? "bad-text" : t.status === "completed" ? "ok-text" : t.status === "waiting" ? "warn-text" : "";
      const eta = t.eta_seconds ? ` · ~${Math.ceil(t.eta_seconds / 60)} min left` : "";
      return `<div class="task">
        <div class="task-head"><span class="desc" title="${esc(t.description)}">${esc(t.description)}</span>
          <span class="${cls}">${done ? esc(t.status) : Math.round(t.progress * 100) + "%"}
          ${done ? "" : `<button class="x" data-cancel="${esc(t.id)}" title="Cancel">✕</button>`}</span></div>
        ${done ? "" : `<div class="bar"><i style="width:${Math.round(t.progress * 100)}%"></i></div>`}
        <div class="task-step">${esc(t.error || t.current_step || (t.status === "waiting" ? "Waiting for your confirmation" : ""))}${eta}</div>
      </div>`;
    }).join("");
  }
}

// ── Captions (latest exchange) ──────────────────────────────────────────
export class Captions {
  constructor(onMore) {
    this.user = $("caption-user"); this.jarvis = $("caption-jarvis"); this.more = $("caption-more");
    this.responses = new Map();
    this.more.onclick = () => onMore?.();
  }
  _overflow() {
    this.more.textContent = this.jarvis.scrollHeight > this.jarvis.clientHeight + 2 ? "Full response in the log ›" : "";
  }
  show(text) { this.jarvis.textContent = text; this._overflow(); }
  userSaid(text, source) {
    this.user.dataset.prefix = source === "voice" ? "🎙 " : "› ";
    this.user.textContent = text;
    this.show("");
  }
  interim(text) { this.user.dataset.prefix = "🎙 "; this.user.textContent = text + "…"; }
  start(id) { this.responses.set(id, ""); this.current = id; this.show(""); }
  delta(id, text) {
    const value = (this.responses.get(id) || "") + text;
    this.responses.set(id, value);
    if (id === this.current) this.show(value);
  }
}

// ── Toasts ──────────────────────────────────────────────────────────────
export function toast(title, message, level = "info", ms = 6000) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = `<b>${esc(title)}</b>${esc(message)}`;
  $("toasts").prepend(el);
  setTimeout(() => el.remove(), ms);
}

// ── Confirmation modal ──────────────────────────────────────────────────
export class ConfirmDialog {
  constructor(onAnswer) {
    this.onAnswer = onAnswer;
    this.current = null;
    $("confirm-yes").onclick = () => this._answer(true);
    $("confirm-no").onclick = () => this._answer(false);
    document.addEventListener("keydown", (e) => {
      if (!this.current) return;
      if (e.key === "Escape") this._answer(false);
    });
  }

  show(event) {
    this.current = event;
    $("confirm-summary").textContent = event.summary;
    $("confirm-risk").textContent = `${event.risk.toUpperCase()} RISK`;
    $("confirm-risk").className = `risk ${event.risk === "high" ? "bad-text" : "warn-text"}`;
    $("confirm-details").innerHTML = event.details.map((d) => `<li>${esc(d)}</li>`).join("");
    $("confirm").classList.remove("hidden");
    clearInterval(this.timer);
    const tick = () => {
      const left = Math.max(0, Math.round((new Date(event.expires_at) - Date.now()) / 1000));
      $("confirm-countdown").textContent = String(left);
    };
    tick();
    this.timer = setInterval(tick, 1000);
  }

  resolve(confirmationId) {
    if (this.current && this.current.confirmation_id === confirmationId) this.hide();
  }

  hide() { this.current = null; clearInterval(this.timer); $("confirm").classList.add("hidden"); }

  _answer(approved) {
    if (!this.current) return;
    this.onAnswer(this.current.confirmation_id, approved);
    this.hide();
  }
}

// ── Boot sequence (real backend stages) ─────────────────────────────────
export class BootScreen {
  constructor(onEngage) {
    this.el = $("boot");
    this.button = $("boot-engage");
    this.button.onclick = () => { this.el.classList.add("done"); onEngage(); };
    this.shown = new Set();
  }

  render(stages, complete, extra = []) {
    const all = [...stages, ...extra];
    $("boot-stages").innerHTML = all.map((s) => {
      const mark = { ok: "✓", warning: "!", failed: "✗", pending: "…" }[s.status] || "…";
      const cls = { ok: "ok-text", warning: "warn-text", failed: "bad-text" }[s.status] || "muted";
      return `<li><span class="${cls}">${mark}</span><span>${esc(s.label)}…</span><span class="detail" title="${esc(s.detail)}">${esc(s.detail)}</span></li>`;
    }).join("");
    if (complete) {
      const failed = all.some((s) => s.status === "failed");
      const warned = all.some((s) => s.status === "warning");
      $("boot-final").innerHTML = failed
        ? `<span class="warn-text">J.A.R.V.I.S. ONLINE — DEGRADED</span>`
        : warned ? `<span class="warn-text">J.A.R.V.I.S. ONLINE — WITH WARNINGS</span>`
          : `<span class="ok-text">J.A.R.V.I.S. ONLINE</span>`;
      this.button.disabled = false;
      this.button.textContent = "ENGAGE";
    }
  }

  failed(message) {
    $("boot-final").innerHTML = `<span class="bad-text">${esc(message)}</span>`;
  }
}
