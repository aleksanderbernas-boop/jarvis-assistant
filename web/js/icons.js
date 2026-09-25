// Inline line icons (stroke = currentColor).
const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

export const icons = {
  mic: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/>'),
  micOff: svg('<path d="M9 9v2a3 3 0 0 0 5.1 2.1M15 10V6a3 3 0 0 0-5.7-1.3M5 11a7 7 0 0 0 11.5 5.4M19 11a7 7 0 0 1-.6 2.8M12 18v3M8 21h8M3 3l18 18"/>'),
  speaker: svg('<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>'),
  speakerOff: svg('<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 9l5 6M22 9l-5 6"/>'),
  log: svg('<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>'),
  folder: svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  globe: svg('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
  music: svg('<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>'),
  image: svg('<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>'),
  gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  shield: svg('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>'),
  cloud: svg('<path d="M7 18a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 9.5a4 4 0 0 1-.5 8.5z"/>'),
  grid: svg('<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>'),
  sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  rain: svg('<path d="M7 15a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 6.5a4 4 0 0 1-.5 8.5z"/><path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3"/>'),
  snow: svg('<path d="M7 15a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 6.5a4 4 0 0 1-.5 8.5z"/><path d="M8 19h.01M12 20h.01M16 19h.01M10 22h.01M14 22h.01"/>'),
  storm: svg('<path d="M7 15a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 6.5a4 4 0 0 1-.5 8.5z"/><path d="M12 14l-2 4h4l-2 4"/>'),
  fog: svg('<path d="M4 10h16M3 14h18M5 18h14"/>'),
};

// WMO weather code → icon name.
export function weatherIcon(code) {
  if (code === 0 || code === 1) return "sun";
  if (code === 2 || code === 3) return "cloud";
  if (code === 45 || code === 48) return "fog";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "storm";
  if (code >= 51) return "rain";
  return "cloud";
}

// A larger, glowing "partly cloudy" style illustration for the weather panel.
export function bigWeatherIcon(code) {
  const kind = weatherIcon(code);
  const cloud = '<path d="M20 44a11 11 0 0 1-1-22 15 15 0 0 1 28 4 9 9 0 0 1 3 18z" fill="url(#wg)" stroke="#d8f0ff" stroke-width="1.2"/>';
  const extra = {
    sun: '<circle cx="35" cy="28" r="12" fill="url(#sg)"/>',
    rain: cloud + '<path d="M24 48l-3 6M33 48l-3 6M42 48l-3 6" stroke="#8fd3ff" stroke-width="2"/>',
    snow: cloud + '<path d="M24 50h.1M33 52h.1M42 50h.1" stroke="#fff" stroke-width="3" stroke-linecap="round"/>',
    storm: cloud + '<path d="M34 46l-4 7h6l-4 7" stroke="#ffe27a" stroke-width="2" fill="none"/>',
    fog: '<path d="M10 30h46M14 38h40M18 46h32" stroke="#bfe6ff" stroke-width="2.5" stroke-linecap="round"/>',
    cloud,
  }[kind];
  return `<svg viewBox="0 0 70 60"><defs>
    <linearGradient id="wg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#8ec9f0"/></linearGradient>
    <radialGradient id="sg"><stop offset="0" stop-color="#fff7c2"/><stop offset="1" stop-color="#ffc94a"/></radialGradient>
  </defs>${extra}</svg>`;
}
