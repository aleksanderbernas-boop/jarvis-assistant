// Rotating wireframe globe with the configured location marked.
// Land outlines come from world-atlas (via CDN); offline, only the graticule is drawn.

const LAND_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/land-110m.json";

export class Globe {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.location = null;
    this.land = null;
    this.angle = 0;
    this._loadLand();
    requestAnimationFrame((t) => this._loop(t));
  }

  setLocation(lat, lon) { this.location = [lon, lat]; }

  async _loadLand() {
    for (let i = 0; i < 20 && !(window.d3 && window.topojson); i++) await new Promise((r) => setTimeout(r, 250));
    if (!window.d3 || !window.topojson) return;
    try {
      const topo = await (await fetch(LAND_URL)).json();
      this.land = window.topojson.feature(topo, topo.objects.land);
    } catch { this.land = null; }
  }

  _loop(now) {
    this._draw(now / 1000);
    setTimeout(() => requestAnimationFrame((t) => this._loop(t)), 50); // ~20 fps is plenty
  }

  _draw(t) {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width) return;
    if (this.canvas.width !== Math.round(rect.width * dpr) || this.canvas.height !== Math.round(rect.height * dpr)) {
      this.canvas.width = Math.round(rect.width * dpr);
      this.canvas.height = Math.round(rect.height * dpr);
    }
    const ctx = this.ctx, w = this.canvas.width, h = this.canvas.height;
    const r = Math.min(w * 0.36, h * 0.47);
    const cx = r + w * 0.05, cy = h / 2;
    ctx.clearRect(0, 0, w, h);

    const halo = ctx.createRadialGradient(cx, cy, r * 0.7, cx, cy, r * 1.15);
    halo.addColorStop(0, "rgba(31,150,255,0.18)");
    halo.addColorStop(1, "rgba(31,150,255,0)");
    ctx.fillStyle = halo;
    ctx.beginPath(); ctx.arc(cx, cy, r * 1.15, 0, Math.PI * 2); ctx.fill();
    const sea = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r);
    sea.addColorStop(0, "rgba(40,140,230,0.55)");
    sea.addColorStop(1, "rgba(6,40,80,0.85)");
    ctx.fillStyle = sea;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();

    const [lon, lat] = this.location || [15, 30];
    const d3 = window.d3;
    if (d3) {
      const rotate = [-(lon - 25 + Math.sin(t * 0.15) * 25), -Math.max(-60, Math.min(60, lat - 15))];
      const projection = d3.geoOrthographic().scale(r).translate([cx, cy]).rotate(rotate).clipAngle(90);
      const path = d3.geoPath(projection, ctx);
      ctx.beginPath(); path(d3.geoGraticule10());
      ctx.strokeStyle = "rgba(120,200,255,0.22)"; ctx.lineWidth = 0.8 * dpr; ctx.stroke();
      if (this.land) {
        ctx.beginPath(); path(this.land);
        ctx.fillStyle = "rgba(90,190,255,0.45)"; ctx.fill();
        ctx.strokeStyle = "rgba(170,230,255,0.9)"; ctx.lineWidth = 0.7 * dpr;
        ctx.shadowColor = "rgba(90,200,255,0.9)"; ctx.shadowBlur = 6 * dpr; ctx.stroke(); ctx.shadowBlur = 0;
      }
      if (this.location) {
        const p = projection(this.location);
        const visible = d3.geoDistance(this.location, [-rotate[0], -rotate[1]]) < Math.PI / 2;
        if (p && visible) this._marker(p[0], p[1], w, cy, t, dpr);
      }
    } else {
      ctx.strokeStyle = "rgba(120,200,255,0.25)";
      for (let i = 1; i < 6; i++) {
        ctx.beginPath(); ctx.ellipse(cx, cy, r * Math.abs(Math.cos(t * 0.2 + i * 0.5)), r, 0, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath(); ctx.ellipse(cx, cy, r, r * (i / 6), 0, 0, Math.PI * 2); ctx.stroke();
      }
    }
    ctx.strokeStyle = "rgba(140,210,255,0.7)"; ctx.lineWidth = 1.2 * dpr;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
  }

  _marker(x, y, w, cy, t, dpr) {
    const ctx = this.ctx;
    const pulse = (t * 0.8) % 1;
    ctx.strokeStyle = `rgba(255,255,255,${1 - pulse})`;
    ctx.beginPath(); ctx.arc(x, y, (4 + pulse * 14) * dpr, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.shadowColor = "#7fd8ff"; ctx.shadowBlur = 10 * dpr;
    ctx.beginPath(); ctx.arc(x, y, 3 * dpr, 0, Math.PI * 2); ctx.fill(); ctx.shadowBlur = 0;
    const endX = w * 0.7;
    ctx.strokeStyle = "rgba(200,235,255,0.8)"; ctx.lineWidth = 1 * dpr;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(endX, cy * 0.85); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(endX, cy * 0.85, 3 * dpr, 0, Math.PI * 2); ctx.fill();
  }
}
