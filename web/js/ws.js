// WebSocket link to the JARVIS backend, with automatic reconnect and session restore.

const TOKEN = document.querySelector('meta[name="jarvis-token"]').content;
const SESSION_KEY = "jarvis.session";

export function api(path, options = {}) {
  const headers = { "X-Jarvis-Token": TOKEN, ...(options.headers || {}) };
  return fetch(path, { ...options, headers });
}

export class Link {
  constructor() {
    this.handlers = new Map();
    this.ws = null;
    this.connected = false;
    this.retry = 0;
    this.sessionId = safeGet(SESSION_KEY);
    this.helloExtras = () => ({});
  }

  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(handler);
  }

  emit(type, payload) {
    for (const handler of this.handlers.get(type) || []) {
      try { handler(payload); } catch (err) { console.error(`handler for ${type} failed`, err); }
    }
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      ws.send(JSON.stringify({ type: "hello", session_id: this.sessionId, ...this.helloExtras() }));
    };
    ws.onmessage = (msg) => {
      let event;
      try { event = JSON.parse(msg.data); } catch { return; }
      if (event.type === "session_snapshot") {
        this.sessionId = event.session_id;
        safeSet(SESSION_KEY, event.session_id);
        this.connected = true;
        this.emit("connection", true);
      }
      this.emit(event.type, event);
      this.emit("*", event);
    };
    ws.onclose = () => {
      const was = this.connected;
      this.connected = false;
      if (was) this.emit("connection", false);
      const delay = Math.min(10000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  send(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
      return true;
    }
    return false;
  }
}

function safeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }
export const storage = { get: safeGet, set: safeSet };
