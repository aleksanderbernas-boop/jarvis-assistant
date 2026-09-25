// The central orb. Its motion reflects the assistant's real state and real audio level.

const STATE_STYLE = {
  STANDBY: { speed: 0.15, hue: 200, energy: 0.35 },
  LISTENING: { speed: 0.3, hue: 196, energy: 0.6 },
  THINKING: { speed: 1.2, hue: 205, energy: 0.8 },
  PLANNING: { speed: 1.0, hue: 215, energy: 0.8 },
  EXECUTING: { speed: 1.6, hue: 190, energy: 0.9 },
  SPEAKING: { speed: 0.5, hue: 198, energy: 1.0 },
  INTERRUPTED: { speed: 0.2, hue: 200, energy: 0.4 },
  WAITING_FOR_CONFIRMATION: { speed: 0.35, hue: 38, energy: 0.8 },
  BACKGROUND_TASK: { speed: 0.5, hue: 190, energy: 0.55 },
  ERROR: { speed: 0.2, hue: 0, energy: 0.7 },
  OFFLINE: { speed: 0.05, hue: 210, energy: 0.15 },
};

export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.state = "OFFLINE";
    this.style = { ...STATE_STYLE.OFFLINE };
    this.level = 0;
    this.smoothLevel = 0;
    this.rotation = 0;
    this.last = performance.now();
    requestAnimationFrame((t) => this._loop(t));
  }

  setState(state) { this.state = state; }
  setLevel(level) { this.level = level; }

  _resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const { width, height } = this.canvas.getBoundingClientRect();
    if (this.canvas.width !== Math.round(width * dpr) || this.canvas.height !== Math.round(height * dpr)) {
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
    }
    return { w: this.canvas.width, h: this.canvas.height };
  }

  _loop(now) {
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    const target = STATE_STYLE[this.state] || STATE_STYLE.STANDBY;
    for (const key of ["speed", "hue", "energy"]) this.style[key] += (target[key] - this.style[key]) * Math.min(1, dt * 3);
    this.smoothLevel += (this.level - this.smoothLevel) * Math.min(1, dt * 12);
    this.rotation += dt * this.style.speed;
    this._draw(now / 1000);
    requestAnimationFrame((t) => this._loop(t));
  }

  _draw(t) {
    const { w, h } = this._resize();
    const ctx = this.ctx;
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2;
    const hue = this.style.hue, energy = this.style.energy, lvl = this.smoothLevel;
    const col = (a, l = 60) => `hsla(${hue}, 100%, ${l}%, ${a})`;
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(cx, cy);

    // Outer tick ring
    ctx.save();
    ctx.rotate(this.rotation * 0.2);
    for (let i = 0; i < 180; i++) {
      const a = (i / 180) * Math.PI * 2;
      const long = i % 5 === 0;
      const r1 = R * 0.93, r2 = R * (long ? 0.975 : 0.955);
      ctx.strokeStyle = col(long ? 0.55 : 0.25);
      ctx.lineWidth = long ? 1.6 : 1;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
      ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
      ctx.stroke();
    }
    ctx.restore();

    // Rotating arcs
    const arcs = [
      { r: 0.86, w: 5, span: 0.9, speed: 0.6, a: 0.8 },
      { r: 0.86, w: 5, span: 0.35, speed: 0.6, offset: 3.4, a: 0.6 },
      { r: 0.8, w: 1.5, span: 4.2, speed: -0.35, a: 0.35 },
      { r: 0.75, w: 3, span: 0.6, speed: 1.1, a: 0.7 },
      { r: 0.75, w: 3, span: 0.25, speed: 1.1, offset: 2.1, a: 0.5 },
      { r: 0.69, w: 1, span: 6.28, speed: 0, a: 0.3, dash: [2, 6] },
    ];
    ctx.shadowColor = col(0.9);
    for (const arc of arcs) {
      const start = (arc.offset || 0) + this.rotation * arc.speed;
      ctx.beginPath();
      ctx.setLineDash(arc.dash || []);
      ctx.strokeStyle = col(arc.a * (0.6 + energy * 0.4));
      ctx.lineWidth = arc.w * (R / 300);
      ctx.shadowBlur = 12;
      ctx.arc(0, 0, R * arc.r, start, start + arc.span);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // Side brackets (left/right markers)
    ctx.shadowBlur = 16;
    ctx.fillStyle = col(0.8);
    for (const side of [-1, 1]) {
      ctx.fillRect(side * R * 0.9 - (R * 0.012), -R * 0.07, R * 0.024, R * 0.14);
    }

    // Sphere
    const pulse = 1 + lvl * 0.06 + Math.sin(t * (this.state === "LISTENING" ? 2.2 : 1.1)) * 0.012 * energy;
    const sr = R * 0.46 * pulse;
    const glow = ctx.createRadialGradient(0, 0, sr * 0.6, 0, 0, sr * 1.45);
    glow.addColorStop(0, col(0.35 + energy * 0.25));
    glow.addColorStop(1, col(0));
    ctx.fillStyle = glow;
    ctx.shadowBlur = 0;
    ctx.beginPath();
    ctx.arc(0, 0, sr * 1.45, 0, Math.PI * 2);
    ctx.fill();

    const body = ctx.createRadialGradient(-sr * 0.25, -sr * 0.3, sr * 0.05, 0, 0, sr);
    body.addColorStop(0, `hsla(${hue}, 100%, 88%, 1)`);
    body.addColorStop(0.35, `hsla(${hue}, 100%, ${55 + energy * 10}%, 1)`);
    body.addColorStop(0.8, `hsla(${hue + 8}, 100%, 30%, 1)`);
    body.addColorStop(1, `hsla(${hue + 10}, 100%, 14%, 1)`);
    ctx.fillStyle = body;
    ctx.beginPath();
    ctx.arc(0, 0, sr, 0, Math.PI * 2);
    ctx.fill();

    // Plasma filaments inside the sphere
    ctx.save();
    ctx.beginPath();
    ctx.arc(0, 0, sr, 0, Math.PI * 2);
    ctx.clip();
    ctx.globalCompositeOperation = "lighter";
    for (let k = 0; k < 7; k++) {
      ctx.beginPath();
      const phase = t * (0.4 + k * 0.07) * (0.5 + energy) + k * 1.7;
      for (let x = -sr; x <= sr; x += sr / 24) {
        const y = Math.sin(x / sr * (2 + k * 0.4) + phase) * sr * (0.18 + lvl * 0.35) + Math.cos(phase * 0.7 + k) * sr * 0.35;
        x === -sr ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.strokeStyle = `hsla(${hue - 10}, 100%, 80%, ${0.08 + energy * 0.08})`;
      ctx.lineWidth = 1.2 * (R / 300);
      ctx.stroke();
    }
    ctx.restore();

    // Rim
    ctx.strokeStyle = col(0.9, 75);
    ctx.lineWidth = 2 * (R / 300);
    ctx.shadowBlur = 20;
    ctx.beginPath();
    ctx.arc(0, 0, sr, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}
