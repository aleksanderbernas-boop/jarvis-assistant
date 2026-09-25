// Smaller canvas instruments: clock ring, core reactor, logo ring, waveform.

function fit(canvas) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const { width, height } = canvas.getBoundingClientRect();
  if (width && (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr))) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  return { w: canvas.width, h: canvas.height, dpr };
}

export function startClock(canvas, timeEl, dateEl, topTime, topDate) {
  const ctx = canvas.getContext("2d");
  const draw = () => {
    const now = new Date();
    const { w, h, dpr } = fit(canvas);
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 4 * dpr;
    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "round";
    ctx.shadowColor = "#1fb6ff";
    for (let i = 0; i < 60; i++) {
      const a = (i / 60) * Math.PI * 2 - Math.PI / 2;
      const long = i % 5 === 0;
      ctx.strokeStyle = long ? "rgba(150,220,255,0.8)" : "rgba(80,160,230,0.35)";
      ctx.lineWidth = (long ? 2 : 1) * dpr;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * r * 0.8, cy + Math.sin(a) * r * 0.8);
      ctx.lineTo(cx + Math.cos(a) * r * (long ? 0.87 : 0.84), cy + Math.sin(a) * r * (long ? 0.87 : 0.84));
      ctx.stroke();
    }
    const sec = now.getSeconds() + now.getMilliseconds() / 1000;
    ctx.shadowBlur = 12 * dpr;
    ctx.strokeStyle = "#35c4ff";
    ctx.lineWidth = 4 * dpr;
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.95, -Math.PI / 2, -Math.PI / 2 + (sec / 60) * Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = "rgba(53,196,255,0.5)";
    ctx.lineWidth = 2 * dpr;
    const minuteFrac = (now.getMinutes() + sec / 60) / 60;
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.72, -Math.PI / 2, -Math.PI / 2 + minuteFrac * Math.PI * 2);
    ctx.stroke();
    ctx.shadowBlur = 0;
    const time = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const date = now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" })
      .toUpperCase().replace(/,/g, ",");
    timeEl.textContent = time; dateEl.textContent = date;
    topTime.textContent = time; topDate.textContent = date;
    requestAnimationFrame(draw);
  };
  draw();
}

export function startReactor(canvas, getLoad) {
  const ctx = canvas.getContext("2d");
  let rot = 0;
  const draw = () => {
    const { w, h, dpr } = fit(canvas);
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 6 * dpr;
    const load = getLoad();
    rot += 0.004 + load * 0.03;
    ctx.clearRect(0, 0, w, h);
    ctx.shadowColor = "#1fb6ff";
    const rings = [
      { rr: 0.95, n: 36, len: 0.12, a: 0.5 },
      { rr: 0.78, n: 18, len: 0.18, a: 0.75 },
      { rr: 0.56, n: 12, len: 0.3, a: 0.9 },
    ];
    rings.forEach((ring, i) => {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(rot * (i % 2 ? -1 : 1) * (1 + i * 0.5));
      for (let k = 0; k < ring.n; k++) {
        const a = (k / ring.n) * Math.PI * 2;
        ctx.strokeStyle = `rgba(80,190,255,${ring.a})`;
        ctx.lineWidth = (i === 1 ? 5 : 2) * dpr;
        ctx.shadowBlur = 8 * dpr;
        ctx.beginPath();
        ctx.arc(0, 0, r * ring.rr, a, a + ((Math.PI * 2) / ring.n) * (1 - ring.len));
        ctx.stroke();
      }
      ctx.restore();
    });
    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 0.4);
    core.addColorStop(0, "rgba(255,255,255,1)");
    core.addColorStop(0.4, `rgba(120,215,255,${0.7 + load * 0.3})`);
    core.addColorStop(1, "rgba(31,150,255,0)");
    ctx.fillStyle = core;
    ctx.beginPath(); ctx.arc(cx, cy, r * 0.4, 0, Math.PI * 2); ctx.fill();
    requestAnimationFrame(draw);
  };
  draw();
}

export function startLogoRing(canvas, getActive) {
  const ctx = canvas.getContext("2d");
  let rot = 0;
  const draw = () => {
    const { w, h, dpr } = fit(canvas);
    rot += getActive() ? 0.05 : 0.01;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "#5fd4ff"; ctx.shadowColor = "#1fb6ff"; ctx.shadowBlur = 8 * dpr;
    ctx.lineWidth = 2 * dpr;
    for (let i = 0; i < 3; i++) {
      ctx.beginPath();
      const a = rot + (i * Math.PI * 2) / 3;
      ctx.arc(w / 2, h / 2, w * 0.4, a, a + 1.6);
      ctx.stroke();
    }
    ctx.lineWidth = 1 * dpr;
    ctx.beginPath(); ctx.arc(w / 2, h / 2, w * 0.26, 0, Math.PI * 2); ctx.stroke();
    requestAnimationFrame(draw);
  };
  draw();
}

// Waveform bars driven by a real signal (microphone or speech output level).
export class Wave {
  constructor(canvas, bars = 64) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.history = new Array(bars).fill(0);
    this.level = 0;
    const draw = () => { this._draw(); requestAnimationFrame(draw); };
    setInterval(() => { this.history.push(this.level); this.history.shift(); }, 40);
    draw();
  }

  push(level) { this.level = level; }

  _draw() {
    const { w, h, dpr } = fit(this.canvas);
    const ctx = this.ctx;
    ctx.clearRect(0, 0, w, h);
    const n = this.history.length;
    const barW = w / (n * 2);
    ctx.fillStyle = "#3cc8ff";
    ctx.shadowColor = "#1fb6ff";
    ctx.shadowBlur = 6 * dpr;
    for (let i = 0; i < n; i++) {
      const v = this.history[n - 1 - i]; // newest sample in the centre
      const mirrored = [n - 1 - i, n + i];
      const bh = Math.max(1.5 * dpr, v * h * 0.95);
      for (const j of mirrored) ctx.fillRect(j * barW * 1 + barW * 0.2, (h - bh) / 2, barW * 0.6, bh);
    }
    ctx.shadowBlur = 0;
  }
}
