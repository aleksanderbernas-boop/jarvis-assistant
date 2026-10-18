// J.A.R.V.I.S. HUD — wires backend events to the interface, and the microphone/speaker to the backend.

import { Link, api, storage } from "./ws.js";
import { icons } from "./icons.js";
import { Orb } from "./hud/orb.js";
import { Globe } from "./hud/globe.js";
import { Wave, startClock, startLogoRing, startReactor } from "./hud/gauges.js";
import {
  ActivityFeed, BootScreen, Captions, ConfirmDialog, TaskPanel, loadWeather, renderMetrics, toast,
} from "./hud/panels.js";
import { Views } from "./hud/views.js";
import { Microphone } from "./voice/mic.js";
import { Speaker } from "./voice/tts.js";
import { BrowserRecognizer } from "./voice/recognition.js";

const $ = (id) => document.getElementById(id);

const STATE_LABEL = {
  STANDBY: "J.A.R.V.I.S. ONLINE", LISTENING: "LISTENING", THINKING: "PROCESSING", PLANNING: "PLANNING",
  EXECUTING: "EXECUTING", SPEAKING: "SPEAKING", INTERRUPTED: "INTERRUPTED",
  WAITING_FOR_CONFIRMATION: "AWAITING CONFIRMATION", BACKGROUND_TASK: "BACKGROUND TASKS ACTIVE", ERROR: "ERROR",
  OFFLINE: "OFFLINE — RECONNECTING",
};
const IDLE = new Set(["STANDBY", "LISTENING", "BACKGROUND_TASK", "OFFLINE"]);

const app = {
  config: {},
  log: [],
  fileEvents: [],
  assistantState: "OFFLINE",
  llmOk: true,
  cpu: 0,
  micActive: false,
  engaged: false,
};

// ── Instruments ─────────────────────────────────────────────────────────
const link = new Link();
const orb = new Orb($("orb"));
const globe = new Globe($("globe"));
const wave = new Wave($("wave"));
const voiceWave = new Wave($("voice-wave"), 20);
const feed = new ActivityFeed($("activity"));
const captions = new Captions(() => views.open("log"));
const tasks = new TaskPanel((id) => link.send({ type: "cancel_task", task_id: id }));
const confirmDialog = new ConfirmDialog((id, approved) => link.send({ type: "confirm", confirmation_id: id, approved }));
const boot = new BootScreen(engage);
startClock($("clock-ring"), $("clock-time"), $("clock-date"), $("top-time"), $("top-date"));
startReactor($("reactor"), () => app.cpu);
startLogoRing($("logo-ring"), () => !IDLE.has(app.assistantState));

// ── Voice ───────────────────────────────────────────────────────────────
const speaker = new Speaker({
  onPlaying: (playing, firstAudioMs) => {
    mic.guarded = playing;
    link.send({ type: "tts_state", playing, first_audio_ms: firstAudioMs ?? null });
  },
  onError: (() => {
    let warned = false;  // one warning per page load, not one per sentence
    return (message) => {
      if (warned) return;
      warned = true;
      toast("Voice output", `Server voice failed (${message}). Using the browser voice instead — see SYSTEMS.`, "warning", 10000);
    };
  })(),
  onLevel: (level) => {
    if (!speaker.playing) return;
    orb.setLevel(level);
    wave.push(level);
  },
});

const mic = new Microphone({
  onLevel: (level) => {
    voiceWave.push(level);
    if (!speaker.playing) { wave.push(level); orb.setLevel(level * 0.6); }
  },
  onSpeechStart: () => {
    if (speaker.playing) {
      bargeIn("barge_in");
      feed.add("Speech interrupted by voice from the microphone", "state");
      setVoiceStatus("Interrupted — heard you talking");
      console.info("[jarvis] barge-in: microphone speech interrupted playback");
    } else {
      setVoiceStatus("Hearing you…");
    }
  },
  onUtterance: (wav) => transcribe(wav),
  onError: (message) => micFailed(message),
});

const recognizer = new BrowserRecognizer({
  // While JARVIS is talking the recogniser would transcribe JARVIS itself — ignore those results.
  onFinal: (text) => { if (!speaker.playing) link.send({ type: "user_text", text, source: "voice" }); },
  onInterim: (text) => captions.interim(text),
  onError: (message) => micFailed(`Speech recognition: ${message}`),
});

mic.allowBargeIn = storage.get("jarvis.bargeIn") !== "off";

function setBargeIn(on) {
  mic.allowBargeIn = on;
  storage.set("jarvis.bargeIn", on ? "on" : "off");
}

async function testVoice() {
  speaker.unlock();
  if (!speaker.enabled) toggleVoice();
  const result = await speaker.test("Testing. This is JARVIS. If you can hear me, the voice works.");
  if (result.ok) toast("Voice test", `Played via ${result.via}. If you heard nothing, check the speakers and Windows volume.`, "success", 9000);
  else toast("Voice test failed", result.error, "error", 12000);
}

function bargeIn(reason) {
  speaker.stop();
  link.send({ type: "interrupt", reason });
}

async function transcribe(wav) {
  if (!link.sessionId) return;
  setVoiceStatus("Transcribing…");
  const form = new FormData();
  form.append("audio", wav, "speech.wav");
  form.append("session_id", link.sessionId);
  try {
    const res = await api("/api/voice/transcribe", { method: "POST", body: form });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      toast("Speech recognition failed", body.detail || `HTTP ${res.status}`, "error");
    }
  } catch {
    toast("Speech recognition failed", "The backend is unreachable.", "error");
  } finally {
    setVoiceStatus(app.micActive ? "Listening…" : "Microphone off");
  }
}

function setVoiceStatus(text) { $("voice-status").textContent = text; }

function micFailed(message) {
  app.micActive = false;
  updateMicUi();
  toast("Microphone", message, "error");
  link.send({ type: "client_settings", mic_active: false, mic_error: message });
}

async function toggleMic(force) {
  const want = force ?? !app.micActive;
  if (!want) {
    mic.stop();
    recognizer.stop();
    app.micActive = false;
  } else {
    try {
      const browserStt = app.config.stt_provider === "browser";
      mic.captureUtterances = !browserStt && app.config.stt_server_side !== false;
      await mic.start();
      if (browserStt) recognizer.start();
      if (app.config.stt_provider === "none") toast("Voice input disabled", "JARVIS_STT_PROVIDER is set to none.", "warning");
      app.micActive = true;
    } catch (err) {
      micFailed(err.name === "NotAllowedError" ? "Microphone permission was denied." : `Microphone unavailable: ${err.message}`);
      return;
    }
  }
  updateMicUi();
  link.send({ type: "client_settings", mic_active: app.micActive, mic_error: "" });
}

function toggleVoice() {
  speaker.enabled = !speaker.enabled;
  if (!speaker.enabled) speaker.stop();
  updateMicUi();
  link.send({ type: "client_settings", voice_output: speaker.enabled });
}

function updateMicUi() {
  $("mic-btn").innerHTML = app.micActive ? icons.mic : icons.micOff;
  $("mic-btn").classList.toggle("on", app.micActive);
  $("voice-mic").innerHTML = icons.mic;
  $("voice-mic").classList.toggle("live", app.micActive);
  $("voice-btn").innerHTML = speaker.enabled ? icons.speaker : icons.speakerOff;
  $("voice-btn").classList.toggle("off", !speaker.enabled);
  setVoiceStatus(app.micActive ? "Listening…" : "Microphone off");
}

// ── Commands ────────────────────────────────────────────────────────────
function send(text) {
  const clean = text.trim();
  if (!clean) return;
  if (/^(stop|quiet|shut up|be quiet|silence|cancel)$/i.test(clean)) speaker.stop();
  if (!link.send({ type: "user_text", text: clean, source: "text" })) toast("Offline", "Not connected to the JARVIS backend.", "error");
}

$("command-form").addEventListener("submit", (e) => {
  e.preventDefault();
  send($("command-input").value);
  $("command-input").value = "";
});
$("mic-btn").onclick = () => toggleMic();
$("voice-btn").onclick = () => toggleVoice();
$("log-btn").innerHTML = icons.log;
$("log-btn").onclick = () => views.open(views.current === "log" ? "home" : "log");
$("voice-hints").addEventListener("click", (e) => { const cmd = e.target.closest("[data-cmd]")?.dataset.cmd; if (cmd) send(cmd); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !confirmDialog.current && (speaker.playing || !IDLE.has(app.assistantState))) bargeIn("escape");
  if (e.key.toLowerCase() === "m" && e.altKey) toggleMic();
});

const QUICK = [
  ["Files", "folder", "Open File Explorer"], ["Browser", "globe", "Open my browser"],
  ["Music", "music", "Open Spotify"], ["Photos", "image", "Open Photos"],
  ["Settings", "gear", () => views.open("settings")], ["Security", "shield", () => views.open("systems")],
  ["Cloud", "cloud", "Open OneDrive"], ["More", "grid", () => views.open("settings")],
];
$("quick-grid").innerHTML = QUICK.map(([label, icon], i) => `<button data-q="${i}">${icons[icon]}<span>${label}</span></button>`).join("");
$("quick-grid").addEventListener("click", (e) => {
  const index = e.target.closest("[data-q]")?.dataset.q;
  if (index == null) return;
  const action = QUICK[Number(index)][2];
  typeof action === "function" ? action() : send(action);
});

const views = new Views({
  send, get config() { return app.config; }, get log() { return app.log; }, get fileEvents() { return app.fileEvents; },
  speaker, toggleMic, toggleVoice, micOn: () => app.micActive,
  testVoice, setBargeIn, bargeInOn: () => mic.allowBargeIn,
});

// ── State display ───────────────────────────────────────────────────────
function setAssistantState(stateName) {
  app.assistantState = stateName;
  orb.setState(stateName);
  $("state-label").textContent = STATE_LABEL[stateName] || stateName;
  const pill = $("state-status");
  pill.querySelector("span").textContent = stateName.replace(/_/g, " ");
  pill.classList.toggle("active", !IDLE.has(stateName));
  if (stateName === "LISTENING" && app.micActive) setVoiceStatus("Listening…");
}

function setConnection(connected) {
  const pill = $("conn-status");
  pill.className = "pill " + (!connected ? "bad" : app.llmOk ? "ok" : "warn");
  pill.querySelector("span").textContent = !connected ? "OFFLINE" : app.llmOk ? "ONLINE" : "DEGRADED";
  pill.title = !connected ? "Backend unreachable" : app.llmOk ? "All core services connected" : "Language model unavailable — see SYSTEMS";
  $("core-title").textContent = !connected ? "CORE OFFLINE" : app.llmOk ? "CORE STABLE" : "CORE DEGRADED";
  if (!connected) setAssistantState("OFFLINE");
}

function applyBoot(stages, complete) {
  const services = stages.find((s) => s.key === "services");
  if (services && services.status !== "pending") app.llmOk = services.status !== "failed";
  if (!app.engaged) boot.render(stages, complete);
  setConnection(link.connected);
}

// ── Backend events ──────────────────────────────────────────────────────
link.helloExtras = () => ({ voice_output: speaker.enabled, mic_active: app.micActive });
link.on("connection", (up) => {
  setConnection(up);
  if (!up) {
    speaker.stop();
    if (!app.engaged) boot.failed("Backend unreachable — retrying…");
  }
});

link.on("session_snapshot", (ev) => {
  const cfg = ev.config;
  app.config = cfg;
  $("version").textContent = `v${ev.version}`;
  speaker.mode = cfg.tts_server_side ? "server" : "browser";
  applyBoot(cfg.boot || [], cfg.boot_complete);
  $("core-memories").textContent = String(cfg.memory_count ?? "—");
  const loc = cfg.location || {};
  if (loc.latitude != null && loc.longitude != null) {
    globe.setLocation(loc.latitude, loc.longitude);
    $("map-place").textContent = (loc.name || "").toUpperCase();
    $("map-coords").innerHTML = `${Math.abs(loc.latitude).toFixed(4)}° ${loc.latitude >= 0 ? "N" : "S"}<br>${Math.abs(loc.longitude).toFixed(4)}° ${loc.longitude >= 0 ? "E" : "W"}`;
    $("map-lock").className = "lock ok";
    $("map-lock").querySelector("span").textContent = "LOCATION LOCKED";
  } else {
    $("map-lock").querySelector("span").textContent = "LOCATION NOT SET";
  }
  app.log = ev.history.map((h) => ({ role: h.role, text: h.text, timestamp: h.timestamp }));
  const last = ev.history[ev.history.length - 1];
  if (last && last.role === "assistant") captions.show(last.text);
  tasks.reset(ev.tasks);
  feed.items = [];
  ev.activity.forEach((a) => feed.add(a.text, a.kind, a.timestamp));
  if (ev.pending_confirmation) confirmDialog.show(ev.pending_confirmation);
  setAssistantState(ev.state);
});

link.on("boot_progress", (ev) => applyBoot(ev.stages, ev.complete));
link.on("assistant_state", (ev) => setAssistantState(ev.state));

link.on("transcription", (ev) => {
  if (ev.ignored) { setVoiceStatus(`Heard “${ev.text.slice(0, 40)}” — say “Jarvis” first`); return; }
  captions.userSaid(ev.text, "voice");
});
link.on("user_message", (ev) => {
  captions.userSaid(ev.text, ev.source);
  app.log.push({ role: "user", text: ev.text, timestamp: ev.timestamp });
});
link.on("response_start", (ev) => { captions.start(ev.response_id); speaker.markResponseStart(ev.response_id); });
link.on("response_delta", (ev) => captions.delta(ev.response_id, ev.delta));
link.on("response_end", (ev) => {
  if (ev.text) app.log.push({ role: "assistant", text: ev.text, timestamp: ev.timestamp });
  if (views.current === "log") views.refresh();
});
link.on("speak", (ev) => speaker.enqueue(ev));
link.on("speech_stop", () => speaker.stop());

link.on("tool_started", (ev) => feed.add(`${ev.display_name}…`, "tool", ev.timestamp));
link.on("tool_completed", (ev) => {
  const note = ev.simulated ? " (simulated)" : ev.verified === false ? " (unverified)" : "";
  feed.add(`${ev.summary || ev.display_name}${note}`, "tool", ev.timestamp);
  if (/^(fs_|open_file)/.test(ev.tool)) app.fileEvents.unshift({ display: ev.display_name, ok: true, summary: ev.summary, timestamp: ev.timestamp });
});
link.on("tool_failed", (ev) => {
  feed.add(`${ev.display_name} failed: ${ev.error}`, "error", ev.timestamp);
  if (/^(fs_|open_file)/.test(ev.tool)) app.fileEvents.unshift({ display: ev.display_name, ok: false, summary: ev.error, timestamp: ev.timestamp });
});
for (const type of ["task_created", "task_progress", "task_completed", "task_failed", "task_cancelled"]) {
  link.on(type, (ev) => {
    tasks.update(ev.task);
    if (type === "task_completed") feed.add(`Complete: ${ev.task.description}`, "task", ev.timestamp);
    if (type === "task_failed") feed.add(`Failed: ${ev.task.description}`, "error", ev.timestamp);
    if (type === "task_cancelled") feed.add(`Cancelled: ${ev.task.description}`, "task", ev.timestamp);
  });
}
link.on("activity", (ev) => feed.add(ev.text, ev.kind, ev.timestamp));
link.on("memory_updated", (ev) => {
  $("core-memories").textContent = String(ev.total);
  feed.add(`Memory ${ev.action}${ev.content ? ": " + ev.content : ""}`, "memory", ev.timestamp);
});
link.on("system_metrics", (ev) => { renderMetrics(ev.metrics); app.cpu = ev.metrics.cpu_percent / 100; });
link.on("notification", (ev) => toast(ev.title, ev.message, ev.level));
link.on("error", (ev) => toast("Error", ev.message, "error"));
link.on("confirmation_request", (ev) => confirmDialog.show(ev));
link.on("confirmation_resolved", (ev) => confirmDialog.resolve(ev.confirmation_id));

// ── Startup ─────────────────────────────────────────────────────────────
function engage() {
  app.engaged = true;
  speaker.unlock();
  if (app.config.stt_provider !== "none") toggleMic(true);
}

async function refreshCore() {
  try {
    const res = await api("/api/diagnostics");
    if (!res.ok) return;
    const d = await res.json();
    const first = d.metrics.latency.first_token;
    $("core-latency").textContent = first?.avg_ms != null ? `${Math.round(first.avg_ms)} ms` : "—";
    app.llmOk = Boolean(d.components.llm.ok);
    setConnection(link.connected);
  } catch { /* offline; the connection pill already says so */ }
}

updateMicUi();
setAssistantState("OFFLINE");
link.connect();
loadWeather();
setInterval(loadWeather, 15 * 60 * 1000);
setInterval(refreshCore, 30000);
setTimeout(refreshCore, 3000);
