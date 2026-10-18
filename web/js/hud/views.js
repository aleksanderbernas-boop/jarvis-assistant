// Detail views opened from the navigation menu. All data comes from the backend API.

import { api } from "../ws.js";
import { timeAgo } from "./panels.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ms = (v) => (v == null ? "—" : `${Math.round(v)} ms`);

export class Views {
  constructor(app) {
    this.app = app; // { send(text), config, log, fileEvents, speaker, toggleMic(), toggleVoice() }
    this.current = "home";
    document.querySelectorAll(".nav-item").forEach((el) => {
      el.addEventListener("click", () => this.open(el.dataset.view));
    });
    $("view-close").onclick = () => this.open("home");
    $("view-body").addEventListener("click", (e) => this._onClick(e));
    $("view-body").addEventListener("change", (e) => this._onChange(e));
  }

  async open(name) {
    this.current = name;
    document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === name));
    $("crumb-view").textContent = name === "home" ? "MAIN" : name.toUpperCase();
    if (name === "home") { $("view").classList.add("hidden"); return; }
    $("view").classList.remove("hidden");
    $("view-title").textContent = name.toUpperCase();
    $("view-body").innerHTML = `<div class="muted">Loading…</div>`;
    try {
      $("view-body").innerHTML = await this[`_${name}`]();
    } catch (err) {
      $("view-body").innerHTML = `<div class="bad-text">Couldn't load this view: ${esc(err.message)}</div>`;
    }
  }

  refresh() { if (this.current !== "home") this.open(this.current); }

  async _json(path) {
    const res = await api(path);
    if (!res.ok) throw new Error(`${path} returned ${res.status}`);
    return res.json();
  }

  async _systems() {
    const d = await this._json("/api/diagnostics");
    const comps = Object.entries(d.components).map(([k, c]) =>
      `<tr><td>${esc(k.replace(/_/g, " "))}</td><td class="${c.ok ? "ok-text" : "bad-text"}">${c.ok ? "OK" : "ISSUE"}</td><td>${esc(c.detail)}</td></tr>`).join("");
    const lat = d.metrics.latency;
    const latencyRows = ["stt", "first_token", "first_sentence", "first_audio", "tts", "response_total"]
      .map((k) => `<tr><td>${k.replace(/_/g, " ")}</td><td>${ms(lat[k]?.avg_ms)}</td><td>${ms(lat[k]?.p95_ms)}</td><td>${lat[k]?.count ?? 0}</td></tr>`).join("");
    const toolRows = Object.entries(d.metrics.tools).sort((a, b) => b[1].count - a[1].count).slice(0, 12)
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${ms(v.avg_ms)}</td><td>${v.count}</td></tr>`).join("");
    const c = d.metrics.counters;
    const errors = (d.recent_errors || []).slice().reverse()
      .map((e) => `<div class="log-entry"><b class="${e.level === "ERROR" ? "bad-text" : "warn-text"}">${esc(e.level)}</b> ${esc(e.message)} <span class="muted small">${timeAgo(e.timestamp)}</span></div>`).join("");
    return `
      <div class="row-actions">
        <button class="btn" data-cmd="Run diagnostics">RUN DIAGNOSTICS</button>
        <button class="btn" data-cmd="What's using most of my memory?">TOP MEMORY</button>
        <button class="btn" data-cmd="What's using the most CPU?">TOP CPU</button>
        <button class="btn" data-cmd="Check system status">SYSTEM STATUS</button>
      </div>
      <h3>COMPONENTS — v${esc(d.version)} · ${esc(d.mode)} mode · ${esc(d.platform)}</h3>
      <table>${comps}</table>
      <h3>LATENCY</h3>
      <table><tr><th>stage</th><th>avg</th><th>p95</th><th>n</th></tr>${latencyRows}</table>
      <h3>TOOLS</h3>
      <table><tr><th>tool</th><th>avg</th><th>calls</th></tr>${toolRows || "<tr><td colspan=3 class=muted>No tool calls yet</td></tr>"}</table>
      <p class="muted">Tool calls: ${c.tool_success || 0} ok / ${c.tool_failure || 0} failed ·
        Tasks completed: ${d.metrics.task_completion_rate == null ? "—" : Math.round(d.metrics.task_completion_rate * 100) + "%"} ·
        Turns: ${c.turns || 0} · Errors: ${c.turn_errors || 0}</p>
      <h3>RECENT WARNINGS & ERRORS</h3>${errors || `<div class="muted">None logged.</div>`}`;
  }

  async _files() {
    const events = this.app.fileEvents.slice(0, 25).map((e) =>
      `<tr><td>${esc(e.display)}</td><td class="${e.ok ? "ok-text" : "bad-text"}">${e.ok ? "ok" : "failed"}</td><td>${esc(e.summary)}</td><td class="muted">${timeAgo(e.timestamp)}</td></tr>`).join("");
    const roots = (this.app.config.allowed_paths || []).map((p) => `<li>${esc(p)}</li>`).join("");
    return `
      <div class="row-actions">
        <button class="btn" data-cmd="Open my Downloads folder">DOWNLOADS</button>
        <button class="btn" data-cmd="Open my Documents folder">DOCUMENTS</button>
        <button class="btn" data-cmd="Open my Desktop folder">DESKTOP</button>
        <button class="btn" data-cmd="What files did I change today?">CHANGED TODAY</button>
        <button class="btn" data-cmd="What are the largest files in my Downloads folder?">LARGEST DOWNLOADS</button>
      </div>
      <h3>FILE OPERATIONS THIS SESSION</h3>
      <table><tr><th>operation</th><th></th><th>result</th><th></th></tr>${events || "<tr><td colspan=4 class=muted>None yet.</td></tr>"}</table>
      <h3>FOLDERS JARVIS MAY ACCESS</h3><ul>${roots}</ul>
      <p class="muted small">Change with JARVIS_ALLOWED_PATHS in .env. Deletions go to the Recycle Bin and always ask first.</p>`;
  }

  async _internet() {
    const tasks = (await this._json("/api/tasks")).filter((t) => t.kind === "research");
    const rows = tasks.map((t) => `
      <div class="log-entry"><b>${esc(t.description)}</b> — <span class="${t.status === "completed" ? "ok-text" : t.status === "failed" ? "bad-text" : ""}">${esc(t.status)}</span>
        <span class="muted small">${timeAgo(t.created_at)}</span>
        ${t.result ? `<details><summary>Report</summary><pre>${esc(t.result)}</pre></details>` : ""}
        ${t.error ? `<div class="bad-text small">${esc(t.error)}</div>` : ""}
      </div>`).join("");
    return `
      <div class="row-actions">
        <button class="btn" data-cmd="Open my browser">OPEN BROWSER</button>
        <button class="btn" data-cmd="Check my internet connection">NETWORK CHECK</button>
      </div>
      <p class="muted">Say “Research …” to start a background research task. Search provider: ${esc(this.app.config.search_provider)}.</p>
      <h3>RESEARCH</h3>${rows || `<div class="muted">No research yet.</div>`}`;
  }

  async _media() {
    return `
      <h3>VOLUME</h3>
      <div class="row-actions">
        <button class="btn" data-cmd="Mute">MUTE</button>
        <button class="btn" data-cmd="Unmute">UNMUTE</button>
        <button class="btn" data-cmd="Volume down">VOL −</button>
        <button class="btn" data-cmd="Volume up">VOL +</button>
      </div>
      <label>Set volume: <input type="range" min="0" max="100" step="5" value="40" data-volume></label>
      <h3>APPS</h3>
      <div class="row-actions">
        <button class="btn" data-cmd="Open Spotify">SPOTIFY</button>
        <button class="btn" data-cmd="Open Photos">PHOTOS</button>
        <button class="btn" data-cmd="Take a screenshot">SCREENSHOT</button>
      </div>`;
  }

  async _settings() {
    const cfg = this.app.config;
    const [memories, tools] = await Promise.all([this._json("/api/memory"), this._json("/api/tools")]);
    const voices = this.app.speaker.voices().filter((v) => v.lang?.startsWith("en") || v.lang?.startsWith("pl"));
    const current = this.app.speaker.voice?.name;
    const voiceSelect = cfg.tts_server_side ? `<span class="muted">Server voice (${esc(cfg.tts_provider)})</span>` :
      `<select data-voice>${voices.map((v) => `<option ${v.name === current ? "selected" : ""}>${esc(v.name)}</option>`).join("")}</select>`;
    const memRows = memories.map((m) => `<tr><td class="muted">${esc(m.kind)}</td><td>${esc(m.content)}</td>
      <td><button class="btn danger" data-forget="${esc(m.id)}">FORGET</button></td></tr>`).join("");
    const toolRows = tools.map((t) => `<tr><td>${esc(t.name)}</td><td class="muted">${esc(t.category)}</td>
      <td class="${t.risk === "high" ? "bad-text" : t.risk === "medium" ? "warn-text" : ""}">${esc(t.risk)}</td>
      <td class="${t.enabled ? "ok-text" : "muted"}">${t.enabled ? "on" : esc(t.reason)}</td>
      <td class="muted">${t.data_boundary === "remote" ? "sends data out" : "local"}</td></tr>`).join("");
    return `
      <h3>VOICE</h3>
      <div class="row-actions">
        <button class="btn" data-action="mic">MICROPHONE: ${this.app.micOn() ? "ON" : "OFF"}</button>
        <button class="btn" data-action="voice">SPOKEN REPLIES: ${this.app.speaker.enabled ? "ON" : "OFF"}</button>
        <button class="btn primary" data-action="test-voice">TEST VOICE</button>
        <button class="btn" data-action="barge">INTERRUPT BY TALKING: ${this.app.bargeInOn() ? "ON" : "OFF"}</button>
      </div>
      <p>Voice: ${voiceSelect}</p>
      <p class="muted small">Using speakers rather than headphones? If JARVIS keeps cutting himself off, turn
        "Interrupt by talking" off. You can still say or type "stop".</p>
      <p class="muted">Speech recognition: ${esc(cfg.stt_provider)} · Wake words: ${esc((cfg.wake_words || []).join(", "))}
        ${cfg.require_wake_word ? `(required; follow-ups within ${cfg.conversation_window_s}s don't need it)` : "(optional)"}</p>
      <h3>PERMISSIONS</h3>
      <table>
        <tr><td>Web access</td><td>${cfg.allow_web ? "allowed" : "blocked"}</td></tr>
        <tr><td>Screen analysis (vision)</td><td>${cfg.allow_vision ? "allowed" : "blocked"}</td></tr>
        <tr><td>Keyboard / mouse control</td><td>${cfg.allow_input_control ? "allowed" : "blocked"}</td></tr>
        <tr><td>Clipboard</td><td>${cfg.allow_clipboard ? "allowed" : "blocked"}</td></tr>
        <tr><td>Confirm medium-risk actions</td><td>${cfg.confirm_medium_risk ? "yes" : "no"} (high-risk always asks)</td></tr>
      </table>
      <p class="muted small">Permissions are set in .env and apply after a restart.</p>
      <h3>MEMORY (${memories.length})</h3>
      <div class="row-actions"><button class="btn danger" data-cmd="Clear my memories">CLEAR ALL…</button></div>
      <table>${memRows || "<tr><td class=muted>Nothing stored.</td></tr>"}</table>
      <h3>TOOLS</h3>
      <table><tr><th>tool</th><th>category</th><th>risk</th><th>status</th><th>data</th></tr>${toolRows}</table>`;
  }

  async _log() {
    const entries = this.app.log.map((e) =>
      `<div class="log-entry ${e.role}"><b>${e.role === "user" ? "YOU" : "JARVIS"}</b> <span class="muted small">${timeAgo(e.timestamp)}</span><div>${esc(e.text)}</div></div>`).join("");
    return entries || `<div class="muted">No conversation yet.</div>`;
  }

  async _more() { return this._settings(); }

  async _onClick(e) {
    const cmd = e.target.closest("[data-cmd]")?.dataset.cmd;
    if (cmd) { this.app.send(cmd); return; }
    const forget = e.target.closest("[data-forget]")?.dataset.forget;
    if (forget) {
      await api(`/api/memory/${encodeURIComponent(forget)}`, { method: "DELETE" });
      this.refresh();
      return;
    }
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "mic") { await this.app.toggleMic(); this.refresh(); }
    if (action === "voice") { this.app.toggleVoice(); this.refresh(); }
    if (action === "test-voice") await this.app.testVoice();
    if (action === "barge") { this.app.setBargeIn(!this.app.bargeInOn()); this.refresh(); }
  }

  _onChange(e) {
    if (e.target.matches("[data-volume]")) this.app.send(`Set volume to ${e.target.value}`);
    if (e.target.matches("[data-voice]")) this.app.speaker.setVoice(e.target.value);
  }
}
