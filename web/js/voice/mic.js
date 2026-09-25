// Microphone capture + voice-activity detection.
// Emits: level (0..1), speech start (used for barge-in), and complete utterances as 16 kHz WAV.

const FRAME_MS = 20;
const PREROLL_MS = 350;
const END_SILENCE_MS = 750;
const MIN_SPEECH_MS = 280;
const MAX_UTTERANCE_MS = 25000;

const WORKLET = `
class JarvisCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("jarvis-capture", JarvisCapture);`;

export class Microphone {
  constructor({ onLevel, onSpeechStart, onUtterance, onError }) {
    Object.assign(this, { onLevel, onSpeechStart, onUtterance, onError });
    this.active = false;
    this.guarded = false; // true while JARVIS is speaking → stricter detection (echo)
    this.captureUtterances = true;
  }

  async start() {
    if (this.active) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext();
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    await this.ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "jarvis-capture");
    this.source.connect(this.node);
    this.rate = this.ctx.sampleRate;
    this.frameSize = Math.round((this.rate * FRAME_MS) / 1000);
    this.pending = new Float32Array(0);
    this.preroll = [];
    this.speech = null;
    this.floor = 0.008;
    this.onset = 0;
    this.node.port.onmessage = (e) => this._ingest(e.data);
    this.stream.getAudioTracks()[0].addEventListener("ended", () => {
      this.stop();
      this.onError?.("Microphone disconnected");
    });
    this.active = true;
  }

  stop() {
    this.active = false;
    this.speech = null;
    try { this.node?.disconnect(); this.source?.disconnect(); } catch { /* already closed */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.onLevel?.(0);
  }

  _ingest(chunk) {
    const merged = new Float32Array(this.pending.length + chunk.length);
    merged.set(this.pending);
    merged.set(chunk, this.pending.length);
    let offset = 0;
    while (merged.length - offset >= this.frameSize) {
      this._frame(merged.subarray(offset, offset + this.frameSize).slice());
      offset += this.frameSize;
    }
    this.pending = merged.slice(offset);
  }

  _frame(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const rms = Math.sqrt(sum / frame.length);
    this.onLevel?.(Math.min(1, rms * 12));

    const threshold = this.guarded ? Math.max(this.floor * 6, 0.045) : Math.max(this.floor * 3, 0.014);
    const onsetFrames = this.guarded ? 12 : 4; // guarded: ~240 ms of clear speech before barging in
    const loud = rms > threshold;

    if (!this.speech) {
      this.preroll.push(frame);
      if (this.preroll.length > PREROLL_MS / FRAME_MS) this.preroll.shift();
      if (!loud) this.floor = Math.min(0.05, Math.max(0.002, this.floor * 0.97 + rms * 0.03));
      this.onset = loud ? this.onset + 1 : 0;
      if (this.onset >= onsetFrames) {
        this.speech = { frames: [...this.preroll], voiced: this.onset, silence: 0 };
        this.preroll = [];
        this.onSpeechStart?.();
      }
      return;
    }

    const s = this.speech;
    s.frames.push(frame);
    if (rms > threshold * 0.6) { s.voiced++; s.silence = 0; } else { s.silence++; }
    const durationMs = s.frames.length * FRAME_MS;
    if (s.silence * FRAME_MS >= END_SILENCE_MS || durationMs >= MAX_UTTERANCE_MS) {
      this.speech = null;
      this.onset = 0;
      if (s.voiced * FRAME_MS >= MIN_SPEECH_MS && this.captureUtterances) {
        this.onUtterance?.(encodeWav(s.frames, this.rate));
      }
    }
  }
}

function encodeWav(frames, inputRate, outputRate = 16000) {
  const total = frames.reduce((n, f) => n + f.length, 0);
  const input = new Float32Array(total);
  let offset = 0;
  for (const f of frames) { input.set(f, offset); offset += f.length; }
  const ratio = inputRate / outputRate;
  const length = Math.floor(input.length / ratio);
  const pcm = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let acc = 0;
    for (let j = start; j < end; j++) acc += input[j];
    const sample = Math.max(-1, Math.min(1, acc / Math.max(1, end - start)));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const write = (o, s) => [...s].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
  write(0, "RIFF"); view.setUint32(4, 36 + pcm.length * 2, true); write(8, "WAVE");
  write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, outputRate, true); view.setUint32(28, outputRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  write(36, "data"); view.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  return new Blob([buffer], { type: "audio/wav" });
}
