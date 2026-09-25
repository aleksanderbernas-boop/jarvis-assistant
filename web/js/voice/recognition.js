// Browser speech recognition (Web Speech API) — used when JARVIS_STT_PROVIDER=browser.

export class BrowserRecognizer {
  constructor({ onFinal, onInterim, onError }) {
    Object.assign(this, { onFinal, onInterim, onError });
    const Impl = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.supported = Boolean(Impl);
    this.enabled = false;
    if (!Impl) return;
    this.rec = new Impl();
    this.rec.continuous = true;
    this.rec.interimResults = true;
    this.rec.lang = navigator.language || "en-GB";
    this.rec.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const text = result[0].transcript.trim();
        if (!text) continue;
        if (result.isFinal) this.onFinal?.(text);
        else this.onInterim?.(text);
      }
    };
    this.rec.onerror = (e) => {
      if (e.error !== "no-speech" && e.error !== "aborted") this.onError?.(e.error);
    };
    this.rec.onend = () => { if (this.enabled) setTimeout(() => this._safeStart(), 250); };
  }

  _safeStart() { try { this.rec.start(); } catch { /* already running */ } }

  start() {
    if (!this.supported) { this.onError?.("Speech recognition isn't supported in this browser"); return; }
    this.enabled = true;
    this._safeStart();
  }

  stop() {
    this.enabled = false;
    try { this.rec?.stop(); } catch { /* not running */ }
  }
}
