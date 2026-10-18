// Sentence-level speech queue with instant interruption (barge-in).
// mode "browser": Web Speech synthesis. mode "server": audio fetched from /api/voice/tts (prefetched).

import { api, storage } from "../ws.js";

const VOICE_KEY = "jarvis.voice";
const PREFERRED = [/daniel/i, /george/i, /ryan/i, /uk english male/i, /arthur/i, /oliver/i, /en-gb/i];

export class Speaker {
  constructor({ onPlaying, onLevel, onError }) {
    this.onPlaying = onPlaying;
    this.onLevel = onLevel;
    this.onError = onError;
    this.mode = "browser";
    this.enabled = true;
    this.queue = [];
    this.current = null;
    this.playing = false;
    this.muted = new Set();
    this.responseStarts = new Map();
    this.firstAudioReported = new Set();
    this.audio = new Audio();
    this.ctx = null;
    if ("speechSynthesis" in window) {
      speechSynthesis.onvoiceschanged = () => this._pickVoice();
      this._pickVoice();
    }
  }

  unlock() {
    // Called from a user gesture so audio playback and analysis are permitted.
    if (!this.ctx) {
      this.ctx = new AudioContext();
      const source = this.ctx.createMediaElementSource(this.audio);
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 256;
      source.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      this._meter();
    }
    this.ctx.resume();
  }

  voices() { return "speechSynthesis" in window ? speechSynthesis.getVoices() : []; }

  setVoice(name) { storage.set(VOICE_KEY, name); this._pickVoice(); }

  _pickVoice() {
    const voices = this.voices();
    const saved = storage.get(VOICE_KEY);
    this.voice = voices.find((v) => v.name === saved)
      || PREFERRED.map((re) => voices.find((v) => re.test(v.name) || re.test(v.lang))).find(Boolean)
      || voices.find((v) => v.lang?.startsWith("en")) || null;
  }

  markResponseStart(responseId) { this.responseStarts.set(responseId, performance.now()); }

  enqueue(event) {
    if (!this.enabled || this.muted.has(event.response_id)) return;
    const item = { ...event };
    if (this.mode === "server") item.audio = this._fetch(event.text);
    this.queue.push(item);
    if (!this.current) this._next();
  }

  mute(responseId) { if (responseId) this.muted.add(responseId); }

  stop() {
    for (const item of this.queue) this.muted.add(item.response_id);
    if (this.current) this.muted.add(this.current.response_id);
    this.queue = [];
    this.current = null;
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    this.audio.pause();
    this.audio.removeAttribute("src");
    this._setPlaying(false);
  }

  async _fetch(text) {
    const res = await api("/api/voice/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `voice service returned HTTP ${res.status}`);
    }
    return URL.createObjectURL(await res.blob());
  }

  async _next() {
    const item = this.queue.shift();
    this.current = item || null;
    if (!item) { this._setPlaying(false); return; }
    if (this.muted.has(item.response_id)) { this._next(); return; }
    try {
      if (this.mode === "server") await this._playServer(item);
      else await this._playBrowser(item);
    } catch (err) {
      console.warn("speech failed", err);
      if (this.mode === "server" && this.current === item) {
        // Server voice unavailable (bad key, quota, network): say it with the browser voice instead.
        this.onError?.(err.message);
        await this._playBrowser(item).catch(() => {});
      }
    }
    if (this.current === item) this._next();
  }

  _started(item) {
    this._setPlaying(true);
    if (!this.firstAudioReported.has(item.response_id)) {
      this.firstAudioReported.add(item.response_id);
      const start = this.responseStarts.get(item.response_id);
      this.firstAudioMs = start ? performance.now() - start : null;
    }
  }

  async _playServer(item) {
    const url = await item.audio;
    if (this.current !== item) return;
    this.audio.src = url;
    await new Promise((resolve) => {
      this.audio.onended = this.audio.onerror = this.audio.onpause = () => { URL.revokeObjectURL(url); resolve(); };
      this.audio.play().then(() => this._started(item)).catch(resolve);
    });
  }

  _playBrowser(item) {
    if (!("speechSynthesis" in window)) return Promise.resolve();
    return new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(item.text);
      if (this.voice) { utterance.voice = this.voice; utterance.lang = this.voice.lang; }
      utterance.rate = 1.03;
      utterance.pitch = 0.95;
      utterance.onstart = () => this._started(item);
      utterance.onend = utterance.onerror = () => resolve();
      speechSynthesis.speak(utterance);
      // Chrome occasionally never fires onend; don't let the queue stall.
      setTimeout(resolve, 2000 + item.text.length * 110);
    });
  }

  _setPlaying(playing) {
    if (playing === this.playing) return;
    this.playing = playing;
    this.onPlaying?.(playing, playing ? this.firstAudioMs : null);
    this.firstAudioMs = null;
  }

  _meter() {
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      let level = 0;
      if (this.playing && this.mode === "server") {
        this.analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += ((v - 128) / 128) ** 2;
        level = Math.min(1, Math.sqrt(sum / data.length) * 4);
      } else if (this.playing) {
        // Browser synthesis exposes no audio stream; a gentle pulse while it is genuinely speaking.
        level = 0.35 + 0.25 * Math.sin(performance.now() / 90);
      }
      this.onLevel?.(level);
      requestAnimationFrame(tick);
    };
    tick();
  }
}
