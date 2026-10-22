/* 雪境 ADV 世界层 */

const SVG_NS = "http://www.w3.org/2000/svg";

// 活动中的 SnowField rAF 循环计数
let activeSnowLoopCount = 0;
export function activeSnowLoops() { return activeSnowLoopCount; }

// 配色
const PALETTES = {
  dawn: { sky: ["#b5c4e2", "#ead3dc", "#fbe7d9"], sun: "#ffdcc2", sunPos: [0.76, 0.52], glow: 170, disc: 46, far: ["#fbf2f1", "#b6c1d9"], mid: ["#f1f1f5", "#8f9fbe"], near: "#6c7c9c", snow: "#fff6f2", ground: ["#fbf3f1", "#dde0ea"], wall: ["#eff0f5", "#d8dce7"], frame: "#c7cddb", floor: ["#e3e6ef", "#c6cbda"], light: "#ffe2cc", lightOpacity: 0.36, lamp: "#ffd6a6", lampOpacity: 0.28, stars: 0, aurora: 0, flake: "#ffffff" },
  day: { sky: ["#8fbde3", "#cde4f4", "#f3f9fd"], sun: "#fffaf0", sunPos: [0.72, 0.2], glow: 170, disc: 26, far: ["#ffffff", "#a6c1da"], mid: ["#f8fbfe", "#7c9fbf"], near: "#5c81a2", snow: "#ffffff", ground: ["#ffffff", "#e1ecf5"], wall: ["#f4f7fa", "#e0e8ef"], frame: "#cddae5", floor: ["#e8eff5", "#cad8e4"], light: "#ffffff", lightOpacity: 0.44, lamp: "#fff3dd", lampOpacity: 0.08, stars: 0, aurora: 0, flake: "#ffffff" },
  dusk: { sky: ["#6a74ab", "#cf97ab", "#f6c29e"], sun: "#ffb284", sunPos: [0.2, 0.58], glow: 200, disc: 58, far: ["#f7dad5", "#998db2"], mid: ["#e8d2d6", "#737099"], near: "#4d4c76", snow: "#f8ddd6", ground: ["#f4ddd8", "#c7bdd1"], wall: ["#e3dae2", "#c6bdcd"], frame: "#b6adc2", floor: ["#d6ccd7", "#b1a7bf"], light: "#ffc8a0", lightOpacity: 0.32, lamp: "#ffce94", lampOpacity: 0.58, stars: 0.15, aurora: 0, flake: "#fff1ec" },
  night: { sky: ["#0b152e", "#1c2e54", "#35507b"], sun: "#e9f1ff", sunPos: [0.24, 0.2], glow: 60, disc: 26, far: ["#c8d5ee", "#2b3e65"], mid: ["#9db0d2", "#1e2e51"], near: "#131e39", snow: "#c6d4ee", ground: ["#9eafcf", "#32456d"], wall: ["#2a3450", "#1c243a"], frame: "#3a4665", floor: ["#232c45", "#151c30"], light: "#bccfff", lightOpacity: 0.16, lamp: "#ffcf88", lampOpacity: 0.88, stars: 1, aurora: 1, flake: "#dfe9ff" },
};

/** 只有舞台背景跟随本机时间 */
export function resolveTime(now = new Date()) {
  const override = document.documentElement.dataset.snowTime;
  if (override && PALETTES[override]) return override;
  const h = now.getHours();
  if (h >= 5 && h < 8) return "dawn";
  if (h >= 8 && h < 17) return "day";
  if (h >= 17 && h < 19) return "dusk";
  return "night";
}

// P8：共享时段时钟
const snowTimeSubscribers = new Set();
let snowTimeTimer = 0;
let snowTimeLast = "";
export function subscribeSnowTime(callback) {
  snowTimeSubscribers.add(callback);
  if (!snowTimeTimer) {
    snowTimeLast = resolveTime();
    snowTimeTimer = window.setInterval(() => {
      const next = resolveTime();
      if (next === snowTimeLast) return;
      snowTimeLast = next;
      for (const fn of [...snowTimeSubscribers]) { try { fn(next); } catch { /* one subscriber never blocks another */ } }
    }, 30000);
  }
  return () => {
    snowTimeSubscribers.delete(callback);
    if (!snowTimeSubscribers.size && snowTimeTimer) { window.clearInterval(snowTimeTimer); snowTimeTimer = 0; }
  };
}

// 工具
function rng(seed) {
  let a = 0;
  for (const ch of String(seed)) a = (Math.imul(a, 31) + ch.charCodeAt(0)) | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
export function mix(a, b, t) {
  const A = hex(a), B = hex(b);
  return "#" + A.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
const f = (n) => (Math.round(n * 10) / 10).toString();
function stops(list) {
  return list.map(([o, c, op = 1]) => `<stop offset="${o}" stop-color="${c}" stop-opacity="${op}"/>`).join("");
}
function lg(id, list, o = {}) {
  const user = o.y1 !== undefined || o.x1 !== undefined;
  const attrs = user
    ? `gradientUnits="userSpaceOnUse" x1="${o.x1 ?? 0}" y1="${o.y1 ?? 0}" x2="${o.x2 ?? 0}" y2="${o.y2 ?? 900}"`
    : `x1="${o.ax1 ?? 0}" y1="${o.ay1 ?? 0}" x2="${o.ax2 ?? 0}" y2="${o.ay2 ?? 1}"`;
  return `<linearGradient id="${id}" ${attrs}>${stops(list)}</linearGradient>`;
}
const rg = (id, list) => `<radialGradient id="${id}">${stops(list)}</radialGradient>`;
function rr(x, y, w, h, rx = 0) {
  if (!rx) return `M${x} ${y}H${x + w}V${y + h}H${x}Z`;
  return `M${x + rx} ${y}H${x + w - rx}A${rx} ${rx} 0 0 1 ${x + w} ${y + rx}V${y + h - rx}A${rx} ${rx} 0 0 1 ${x + w - rx} ${y + h}H${x + rx}A${rx} ${rx} 0 0 1 ${x} ${y + h - rx}V${y + rx}A${rx} ${rx} 0 0 1 ${x + rx} ${y}Z`;
}
const arch = (x, y, w, h) => { const r = w / 2; return `M${x} ${y + h}V${y + r}A${r} ${r} 0 0 1 ${x + w} ${y + r}V${y + h}Z`; };
const quad = (pts) => "M" + pts.map(([x, y]) => `${f(x)} ${f(y)}`).join("L") + "Z";
const svg = (inner, label) => `<svg xmlns="${SVG_NS}" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true" data-layer="${label}">${inner}</svg>`;

function ridge(r, { y, amp, n0 = 5, steps = 6, rough = 0.52 }) {
  let pts = Array.from({ length: n0 }, () => y + (r() - 0.5) * amp);
  let disp = amp * 0.6;
  for (let s = 0; s < steps; s++) {
    const next = [];
    for (let i = 0; i < pts.length - 1; i++) next.push(pts[i], (pts[i] + pts[i + 1]) / 2 + (r() - 0.5) * disp);
    next.push(pts[pts.length - 1]);
    pts = next;
    disp *= rough;
  }
  const step = 1640 / (pts.length - 1);
  return `M-20 900L-20 ${f(pts[0])}` + pts.map((p, i) => `L${f(-20 + i * step)} ${f(p)}`).join("") + "L1620 900Z";
}
function pines(r, { baseY, count, hMin, hMax, color, snow, x0 = -30, x1 = 1630, jitter = 16 }) {
  const trees = Array.from({ length: count }, () => ({ x: x0 + r() * (x1 - x0), h: hMin + r() * (hMax - hMin), y: baseY + r() * jitter }))
    .sort((a, b) => a.y - b.y);
  let body = "", caps = "";
  for (const { x, h, y } of trees) {
    const w = h * (0.38 + r() * 0.1);
    for (let t = 0; t < 3; t++) {
      const ty = y - h * t * 0.27, th = h * (0.5 - t * 0.07), tw = w * (1 - t * 0.25);
      body += `M${f(x - tw / 2)} ${f(ty)}L${f(x)} ${f(ty - th)}L${f(x + tw / 2)} ${f(ty)}Z`;
      caps += `M${f(x - tw * 0.24)} ${f(ty - th * 0.52)}L${f(x)} ${f(ty - th)}L${f(x + tw * 0.24)} ${f(ty - th * 0.52)}Q${f(x)} ${f(ty - th * 0.44)} ${f(x - tw * 0.24)} ${f(ty - th * 0.52)}Z`;
    }
  }
  return `<path d="${body}" fill="${color}"/><path d="${caps}" fill="${snow}" opacity=".92"/>`;
}

// 远景（室外，透过窗看到或 generic 全屏）
function worldTree(p, id, H) {
  const cx = 600, top = H - 400, base = H - 120;
  const light = mix(p.far[1], "#dff7ff", 0.42), dark = mix(p.far[1], "#6fb7d8", 0.22);
  const k = p.stars ? 1 : 0.6;
  const shards = [
    [[cx, top], [cx + 24, top + 140], [cx + 13, base], [cx - 14, base], [cx - 26, top + 152]],
    [[cx - 38, top + 80], [cx - 20, top + 172], [cx - 24, base], [cx - 60, base], [cx - 72, top + 194]],
    [[cx + 42, top + 106], [cx + 70, top + 206], [cx + 57, base], [cx + 24, base], [cx + 20, top + 176]],
    [[cx - 96, top + 176], [cx - 75, top + 238], [cx - 82, base], [cx - 110, base], [cx - 120, top + 246]],
  ];
  let s = `<g opacity="${(0.42 * k + 0.06).toFixed(2)}">`;
  s += `<circle cx="${cx - 16}" cy="${top + 124}" r="190" fill="url(#${id}-tree)" opacity="${p.stars ? 0.22 : 0.08}"/>`;
  shards.forEach((pts, i) => {
    const [a, b, c, d, e] = pts;
    s += `<path d="M${a[0]} ${a[1]}L${b[0]} ${b[1]}L${c[0]} ${c[1]}L${d[0]} ${d[1]}L${e[0]} ${e[1]}Z" fill="${i % 2 ? dark : light}"/>`;
    s += `<path d="M${a[0]} ${a[1]}L${b[0]} ${b[1]}L${c[0]} ${c[1]}Z" fill="${i % 2 ? light : dark}" opacity=".55"/>`;
  });
  return s + "</g>";
}
function containmentWall(p, H) {
  const y = H - 96, h = 22;
  const body = mix(p.mid[1], p.far[1], 0.45), edge = mix(body, "#ffffff", 0.35);
  const beacon = p.stars ? "#ffc08a" : mix("#ffc08a", p.far[1], 0.35);
  let s = `<g opacity="${p.stars ? 0.8 : 0.62}"><rect x="0" y="${y}" width="1600" height="${h}" fill="${body}"/><rect x="0" y="${y}" width="1600" height="2" fill="${edge}"/>`;
  for (let x = 70; x < 1600; x += 250) {
    s += `<rect x="${x}" y="${y - 30}" width="16" height="${30 + h}" fill="${body}"/><rect x="${x - 4}" y="${y - 34}" width="24" height="5" fill="${edge}"/>`;
    s += `<circle cx="${x + 8}" cy="${y - 40}" r="${p.stars ? 9 : 6}" fill="${beacon}" opacity="${p.stars ? 0.32 : 0.18}"/><circle cx="${x + 8}" cy="${y - 40}" r="2.6" fill="${beacon}"/>`;
  }
  return s + "</g>";
}
function outdoor(p, r, H, land) {
  const id = "f";
  let defs = lg(`${id}-sky`, [[0, p.sky[0]], [0.58, p.sky[1]], [1, p.sky[2]]], { y1: 0, y2: H + 40 });
  defs += rg(`${id}-sun`, [[0, p.sun, 0.9], [0.22, p.sun, 0.5], [1, p.sun, 0]]);
  defs += lg(`${id}-far`, [[0, p.far[0]], [0.42, mix(p.far[0], p.far[1], 0.55)], [1, p.far[1]]], { y1: H - 330, y2: H - 40 });
  defs += lg(`${id}-mid`, [[0, p.mid[0]], [0.4, mix(p.mid[0], p.mid[1], 0.62)], [1, p.mid[1]]], { y1: H - 150, y2: H + 30 });
  defs += lg(`${id}-ground`, [[0, p.ground[0]], [1, p.ground[1]]], { y1: H, y2: 900 });
  defs += lg(`${id}-haze`, [[0, p.sky[2], 0], [1, p.sky[2], 0.9]], { y1: H - 230, y2: H + 20 });
  defs += `<filter id="${id}-blur" x="-20%" y="-60%" width="140%" height="220%"><feGaussianBlur stdDeviation="20"/></filter>`;
  let body = `<rect width="1600" height="900" fill="url(#${id}-sky)"/>`;
  if (p.stars) {
    for (let i = 0; i < 150; i++) body += `<circle cx="${f(r() * 1600)}" cy="${f(r() * H * 0.82)}" r="${f(0.5 + r() * 1.3)}" fill="#fff" opacity="${(p.stars * (0.2 + r() * 0.8)).toFixed(2)}"/>`;
  }
  if (p.aurora) {
    body += `<g filter="url(#${id}-blur)" opacity=".5">`;
    ["#6fe3c4", "#7fb6ff", "#ad9dff"].forEach((c, k) => {
      let d = `M-60 ${f(H * 0.34 + k * 36)}`;
      for (let x = -60; x < 1660; x += 170) d += `Q${f(x + 85)} ${f(H * 0.16 + k * 34 + (r() - 0.5) * 90)} ${f(x + 170)} ${f(H * 0.3 + k * 36 + (r() - 0.5) * 60)}`;
      body += `<path d="${d}" stroke="${c}" stroke-width="${48 - k * 11}" fill="none" opacity="${(0.6 - k * 0.14).toFixed(2)}"/>`;
    });
    body += "</g>";
  }
  const [sx, sy] = p.sunPos;
  body += `<circle cx="${f(sx * 1600)}" cy="${f(sy * H)}" r="${p.glow * 3}" fill="url(#${id}-sun)"/>`;
  body += `<circle cx="${f(sx * 1600)}" cy="${f(sy * H)}" r="${p.disc}" fill="${p.sun}" opacity=".96"/>`;
  defs += rg(`${id}-tree`, [[0, "#9fe6ff", 1], [0.4, "#9fe6ff", 0.35], [1, "#9fe6ff", 0]]);
  body += worldTree(p, id, H);
  if (land) return svg(`<defs>${defs}</defs>${body}${land(p, r, H)}`, "far");
  body += `<path d="${ridge(r, { y: H - 190, amp: 250, n0: 6 })}" fill="url(#${id}-far)"/>`;
  body += `<rect y="${H - 230}" width="1600" height="260" fill="url(#${id}-haze)" opacity=".6"/>`;
  body += containmentWall(p, H);
  body += `<path d="${ridge(r, { y: H - 70, amp: 120, n0: 5, rough: 0.5 })}" fill="url(#${id}-mid)"/>`;
  body += pines(r, { baseY: H + 6, count: 80, hMin: 22, hMax: 50, color: mix(p.near, p.mid[1], 0.5), snow: p.snow, jitter: 10 });
  body += `<rect y="${H + 8}" width="1600" height="${900 - H}" fill="url(#${id}-ground)"/>`;
  for (let i = 0; i < 9; i++) body += `<ellipse cx="${f(r() * 1600)}" cy="${f(H + 40 + r() * (900 - H))}" rx="${f(120 + r() * 220)}" ry="${f(10 + r() * 18)}" fill="${p.snow}" opacity=".7"/>`;
  body += pines(r, { baseY: H + 92, count: 18, hMin: 80, hMax: 150, color: p.near, snow: p.snow, jitter: 40 });
  return svg(`<defs>${defs}</defs>${body}`, "far");
}

// 室内（墙带窗洞，雪只透过窗显示）
function roomDefs(p, id) {
  return lg(`${id}-wall`, [[0, p.wall[0]], [1, p.wall[1]]])
    + lg(`${id}-floor`, [[0, p.floor[0]], [1, p.floor[1]]])
    + lg(`${id}-shaft`, [[0, p.light, p.lightOpacity], [1, p.light, 0]])
    + lg(`${id}-glass`, [[0, "#ffffff", 0.16], [0.45, "#ffffff", 0.02], [0.55, "#ffffff", 0.1], [1, "#ffffff", 0]], { ax1: 0, ay1: 0, ax2: 1, ay2: 1 })
    + rg(`${id}-lamp`, [[0, p.lamp, 0.95], [0.3, p.lamp, 0.42], [1, p.lamp, 0]])
    + `<filter id="${id}-soft" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur stdDeviation="3"/></filter>`;
}
const tone = (p, k) => mix(p.wall[1], "#1f2d3a", k);
const lamp = (p, id, x, y, rad, k = 1) => `<circle cx="${x}" cy="${y}" r="${rad}" fill="url(#${id}-lamp)" opacity="${Math.min(1, p.lampOpacity * k).toFixed(2)}"/>`;
let buildPalette = null;
const wall = (id, holes) => `<path d="M0 0H1600V900H0Z${holes.join("")}" fill="url(#${id}-wall)" fill-rule="evenodd"/>`
  + (buildPalette ? wallPanels(buildPalette, id, holes, id.replace(/^r-/, "")) : "");
// P8 尘白元素
const ROOM_MARKS = {
  archive: ["B-02", "资料室", 405, 170, 1, "v"], quarters: ["B-03", "宿舍", 250, 330, 1],
  lounge: ["B-04", "休息区", 460, 160, 1, "v"], canteen: ["B-05", "食堂", 1262, 462, 0.8], training: ["B-06", "训练区", 230, 272, 1],
  medical: ["B-07", "医疗室", 1478, 170, 1, "v"],
};
function wallPanels(p, id, holes, key) {
  const line = mix(p.wall[1], "#1f2d3a", 0.16), mark = ROOM_MARKS[key];
  let s = `<defs><clipPath id="${id}-wallclip"><path d="M0 0H1600V900H0Z${holes.join("")}" clip-rule="evenodd"/></clipPath></defs>`;
  s += `<g clip-path="url(#${id}-wallclip)" fill="none" stroke="${line}" stroke-width="2" opacity=".42">`;
  // 腰线：每 400px 一段，段首做 45° 切角。
  for (let x = 0; x < 1600; x += 400) s += `<path d="M${x} 640H${x + 360}L${x + 384} 616H${x + 400}"/>`;
  // 竖向接缝：顶部切角。
  for (let x = 200; x < 1600; x += 400) s += `<path d="M${x} 0V36L${x + 18} 54V640"/>`;
  s += "</g>";
  if (mark) {
    const [code, name, x, y, k, dir] = mark;
    // 深色墙（夜晚）上用浅色墨，浅色墙上用深色墨，保持同样的对比。
    const [wr, wg, wb] = hex(p.wall[1]);
    const ink = (wr * 0.299 + wg * 0.587 + wb * 0.114) < 110 ? mix(p.wall[1], "#cfe0ee", 0.42) : mix(p.wall[1], "#1f2d3a", 0.32);
    const cjk = `font-family="'LXGW WenKai Screen', 'Microsoft YaHei', sans-serif" font-weight="600"`;
    s += `<g clip-path="url(#${id}-wallclip)" opacity=".72" fill="${ink}" font-family="ui-monospace, Consolas, monospace" font-weight="700" transform="translate(${x} ${y}) scale(${k}) translate(${-x} ${-y})">`;
    if (dir === "v") {
      // 竖排：编号逐字向下，再空一格写房间名。
      s += `<path d="M${x - 8} ${y - 40}h14l-14 14z"/><g text-anchor="middle" font-size="28">`;
      [...code].forEach((ch, i) => { s += `<text x="${x}" y="${y + i * 32}">${ch === "-" ? "|" : ch}</text>`; });
      s += `</g><g text-anchor="middle" font-size="20" ${cjk}>`;
      [...name].forEach((ch, i) => { s += `<text x="${x}" y="${y + code.length * 32 + 22 + i * 26}">${ch}</text>`; });
      s += "</g>";
    } else {
      s += `<path d="M${x} ${y - 30}h12l-12 12z"/><text x="${x + 20}" y="${y}" font-size="30" letter-spacing="6">${code}</text>`;
      if (k >= 1) s += `<text x="${x + 22}" y="${y + 30}" font-size="18" letter-spacing="8" ${cjk}>${name}</text>`;
    }
    s += "</g>";
  }
  return s;
}
const glass = (id, holes) => `<path d="${holes.join("")}" fill="url(#${id}-glass)"/>`;
function shaft(id, x, y, w, floorY, skew = 180) {
  return `<path d="${quad([[x, y], [x + w, y], [x + w + skew * 1.5, 900 + (900 - floorY) * 0.2], [x + skew, 900 + (900 - floorY) * 0.2]])}" fill="url(#${id}-shaft)"/>`;
}
const floor = (id, y) => `<rect y="${y}" width="1600" height="${900 - y}" fill="url(#${id}-floor)"/>`;

const ROOMS = {
  observation(p, r, id) {
    const x = 70, y = 62, w = 1460, h = 580, fy = 668;
    const hole = rr(x, y, w, h, 42);
    let s = wall(id, [hole]) + glass(id, [hole]);
    s += `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="16"/>`;
    s += [x + w / 3, x + (2 * w) / 3].map((mx) => `<rect x="${f(mx - 4)}" y="${y}" width="8" height="${h}" fill="${p.frame}"/>`).join("");
    s += `<rect x="${x - 10}" y="${y + h - 4}" width="${w + 20}" height="26" rx="6" fill="${mix(p.frame, "#ffffff", 0.35)}"/>`;
    s += floor(id, fy) + `<rect x="${x}" y="${fy}" width="${w}" height="140" fill="${p.light}" opacity="${(p.lightOpacity * 0.32).toFixed(2)}"/>`;
    s += shaft(id, x + 220, y + h, w - 440, fy, 120);
    const rail = tone(p, 0.32);
    s += `<g filter="url(#${id}-soft)"><rect x="-10" y="700" width="1620" height="10" rx="5" fill="${rail}"/>`;
    for (let px = 40; px < 1600; px += 170) s += `<rect x="${px}" y="700" width="7" height="120" fill="${rail}"/>`;
    s += `<rect x="-10" y="760" width="1620" height="5" fill="${rail}" opacity=".7"/>`;
    s += `<rect x="1090" y="776" width="380" height="30" rx="12" fill="${tone(p, 0.3)}"/><rect x="1120" y="806" width="12" height="70" fill="${tone(p, 0.36)}"/><rect x="1428" y="806" width="12" height="70" fill="${tone(p, 0.36)}"/></g>`;
    s += lamp(p, id, 120, 720, 140, 0.8) + lamp(p, id, 1480, 720, 140, 0.8);
    return { room: s, horizon: 548 };
  },
  archive(p, r, id) {
    const fy = 704, wins = [[430, 110], [890, 110]], holes = wins.map(([x, y]) => arch(x, y, 280, 500));
    let s = wall(id, holes) + glass(id, holes);
    for (const [x, y] of wins) {
      s += `<path d="${arch(x, y, 280, 500)}" fill="none" stroke="${p.frame}" stroke-width="14"/><rect x="${x + 136}" y="${y}" width="8" height="500" fill="${p.frame}"/><rect x="${x}" y="${y + 250}" width="280" height="8" fill="${p.frame}"/>`;
      s += shaft(id, x + 10, y + 500, 260, fy, 90);
    }
    s += floor(id, fy);
    const books = ["#7d8fb0", "#b07d86", "#8fa58c", "#c4a77a", "#6f7f98", "#a58fb5", "#d6cbb8", "#93a9b8"];
    s += `<g filter="url(#${id}-soft)">`;
    for (const [sx, sw] of [[-20, 400], [1220, 400]]) {
      s += `<rect x="${sx}" y="108" width="${sw}" height="${fy - 96}" rx="8" fill="${tone(p, 0.42)}"/>`;
      for (let row = 0; row < 5; row++) {
        const ry = 128 + row * 114, base = ry + 98;
        let bx = sx + 18;
        while (bx < sx + sw - 30) {
          const bw = 11 + r() * 15, bh = 58 + r() * 38, c = mix(books[Math.floor(r() * books.length)], p.wall[1], 0.38);
          const lean = r() < 0.06 ? 8 : 0;
          s += `<rect x="${f(bx)}" y="${f(base - bh)}" width="${f(bw)}" height="${f(bh)}" rx="2" fill="${c}"${lean ? ` transform="rotate(${lean} ${f(bx)} ${f(base)})"` : ""}/>`;
          bx += bw + 2 + (r() < 0.08 ? 18 : 0);
        }
        s += `<rect x="${sx + 8}" y="${base}" width="${sw - 16}" height="12" fill="${tone(p, 0.5)}"/>`;
      }
    }
    s += `<rect x="470" y="742" width="660" height="26" rx="8" fill="${tone(p, 0.36)}"/><rect x="520" y="768" width="14" height="110" fill="${tone(p, 0.42)}"/><rect x="1066" y="768" width="14" height="110" fill="${tone(p, 0.42)}"/>`;
    s += `<path d="M1000 742V700M972 700H1028L1012 668H988Z" stroke="${tone(p, 0.5)}" stroke-width="5" fill="${mix(p.lamp, p.wall[0], 0.4)}"/></g>`;
    s += lamp(p, id, 1000, 700, 190, 1.2) + lamp(p, id, 300, 90, 110, 0.6) + lamp(p, id, 1300, 90, 110, 0.6);
    return { room: s, horizon: 470 };
  },
  quarters(p, r, id) {
    const fy = 720, hole = rr(930, 120, 500, 430, 16);
    let s = wall(id, [hole]) + glass(id, [hole]);
    s += `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="16"/><rect x="1176" y="120" width="8" height="430" fill="${p.frame}"/><rect x="930" y="330" width="500" height="8" fill="${p.frame}"/>`;
    s += floor(id, fy) + shaft(id, 940, 550, 480, fy, -220);
    const cur = mix(p.wall[0], "#e9c6cf", 0.42), curDeep = mix(cur, "#1f2d3a", 0.12);
    const fold = `${id}-fold`;
    s += `<defs>${lg(fold, [0, 0.17, 0.33, 0.5, 0.67, 0.83, 1].map((o, i) => [o, i % 2 ? curDeep : cur]), { ax1: 0, ay1: 0, ax2: 1, ay2: 0 })}</defs>`;
    s += `<rect x="860" y="96" width="640" height="10" rx="5" fill="${tone(p, 0.4)}"/>`;
    s += `<path d="M862 104H1010Q990 360 1030 640Q950 660 870 640Z" fill="url(#${fold})"/><path d="M1498 104H1350Q1370 360 1330 640Q1410 660 1490 640Z" fill="url(#${fold})"/>`;
    s += `<g filter="url(#${id}-soft)"><rect x="-60" y="676" width="800" height="300" rx="34" fill="${mix(p.wall[0], "#c7d4e5", 0.42)}"/>`;
    s += `<rect x="-60" y="640" width="740" height="120" rx="46" fill="${mix(p.wall[0], "#e3eaf4", 0.3)}"/><ellipse cx="120" cy="634" rx="120" ry="40" fill="${mix(p.wall[0], "#ffffff", 0.5)}"/>`;
    s += `<rect x="-60" y="430" width="70" height="260" rx="20" fill="${tone(p, 0.32)}"/>`;
    s += `<path d="M1520 720h70l-10 120h-50z" fill="${tone(p, 0.38)}"/><g fill="${mix(tone(p, 0.3), "#6f9a84", 0.5)}"><ellipse cx="1540" cy="640" rx="26" ry="70" transform="rotate(-18 1540 640)"/><ellipse cx="1580" cy="630" rx="24" ry="80" transform="rotate(14 1580 630)"/><ellipse cx="1560" cy="600" rx="20" ry="70"/></g></g>`;
    let d = "";
    for (let i = 0; i <= 14; i++) {
      const t = i / 14, lx = 60 + t * 700, ly = 140 + Math.sin(t * Math.PI) * 70 + t * 40;
      d += lamp(p, id, f(lx), f(ly), 30, 1.4) + `<circle cx="${f(lx)}" cy="${f(ly)}" r="4" fill="${p.lamp}" opacity="${Math.min(1, p.lampOpacity * 1.6).toFixed(2)}"/>`;
    }
    s += `<path d="M60 140Q410 280 760 180" stroke="${tone(p, 0.3)}" stroke-width="2" fill="none" opacity=".6"/>` + d;
    s += lamp(p, id, 800, 600, 170, 1.1);
    return { room: s, horizon: 430 };
  },
  lounge(p, r, id) {
    const fy = 704, holes = [150, 490, 830, 1170].map((x) => rr(x, 100, 280, 400, 14));
    let s = wall(id, holes) + glass(id, holes) + holes.map((h) => `<path d="${h}" fill="none" stroke="${p.frame}" stroke-width="14"/>`).join("");
    s += floor(id, fy) + [150, 490, 830, 1170].map((x) => shaft(id, x, 500, 280, fy, 140)).join("");
    s += `<g filter="url(#${id}-soft)">`;
    for (const lx of [320, 800, 1280]) s += `<path d="M${lx} 0V78" stroke="${tone(p, 0.4)}" stroke-width="3"/><path d="M${lx - 46} 120Q${lx} 60 ${lx + 46} 120Z" fill="${tone(p, 0.36)}"/>`;
    const sofa = mix(p.wall[1], "#8ea3c0", 0.4);
    s += `<rect x="380" y="626" width="840" height="130" rx="44" fill="${mix(sofa, "#1f2d3a", 0.08)}"/><rect x="330" y="712" width="940" height="120" rx="40" fill="${sofa}"/>`;
    s += `<rect x="300" y="664" width="110" height="180" rx="40" fill="${mix(sofa, "#1f2d3a", 0.12)}"/><rect x="1190" y="664" width="110" height="180" rx="40" fill="${mix(sofa, "#1f2d3a", 0.12)}"/>`;
    s += `<ellipse cx="800" cy="866" rx="280" ry="30" fill="${tone(p, 0.3)}"/>`;
    // P7：两侧盆栽——花盆 + 扇形细叶，颜色压淡，不再是两块模糊的大绿椭圆。
    const leaf = mix(tone(p, 0.3), "#7fa592", 0.42), leafDeep = mix(leaf, "#1f2d3a", 0.16), pot = mix(p.wall[0], "#ffffff", 0.3);
    const plant = (x, base, flip) => {
      let g = `<g transform="translate(${x} ${base})${flip ? " scale(-1 1)" : ""}">`;
      for (const [ang, len, w, c] of [[-40, 118, 13, leafDeep], [-18, 150, 15, leaf], [4, 162, 14, leafDeep], [24, 136, 13, leaf], [44, 104, 11, leafDeep]]) {
        g += `<ellipse cx="0" cy="${-len / 2}" rx="${w}" ry="${len / 2}" fill="${c}" transform="rotate(${ang})"/>`;
      }
      g += `<path d="M-36 0h72l-9 66h-54z" fill="${pot}"/><rect x="-40" y="-6" width="80" height="12" rx="5" fill="${mix(pot, "#1f2d3a", 0.12)}"/>`;
      return `${g}</g>`;
    };
    s += plant(96, 700, false) + plant(1504, 700, true) + `</g>`;
    s += [320, 800, 1280].map((lx) => lamp(p, id, lx, 126, 120, 1.3)).join("");
    return { room: s, horizon: 410 };
  },
  canteen(p, r, id) {
    const fy = 600, hole = rr(70, 80, 1460, 250, 12);
    let s = wall(id, [hole]) + glass(id, [hole]) + `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="14"/>`;
    for (let x = 70 + 146; x < 1530; x += 146) s += `<rect x="${x - 3}" y="80" width="6" height="250" fill="${p.frame}"/>`;
    s += floor(id, fy) + shaft(id, 200, 330, 1200, fy, 60);
    s += `<g filter="url(#${id}-soft)"><rect x="180" y="380" width="300" height="110" rx="10" fill="${mix(p.wall[0], "#ffffff", 0.4)}" opacity=".85"/>`;
    // P7：右侧出餐台原为 32% 深灰块，压过人物；调浅并给台面一点暖白。
    s += `<rect x="1120" y="500" width="520" height="260" rx="10" fill="${tone(p, 0.18)}"/><rect x="1110" y="490" width="540" height="24" rx="8" fill="${mix(p.wall[0], "#fff6ea", 0.55)}"/>`;
    const tableC = tone(p, 0.26);
    for (const [y, w, hgt] of [[650, 760, 18], [748, 940, 22], [866, 1180, 26]]) {
      const x = 560 - w / 2;
      s += `<rect x="${x}" y="${y}" width="${w}" height="${hgt}" rx="6" fill="${tableC}"/>`;
      for (let k = 0; k < 4; k++) s += `<rect x="${f(x + 40 + k * ((w - 80) / 3))}" y="${y + hgt}" width="10" height="${f(hgt * 2.6)}" fill="${tone(p, 0.36)}"/>`;
    }
    s += "</g>";
    for (let i = 0; i < 6; i++) { const lx = 160 + i * 256; s += `<path d="M${lx} 0V360" stroke="${tone(p, 0.36)}" stroke-width="2"/><path d="M${lx - 30} 392Q${lx} 350 ${lx + 30} 392Z" fill="${tone(p, 0.34)}"/>` + lamp(p, id, lx, 396, 90, 1.4); }
    return { room: s, horizon: 290 };
  },
  training(p, r, id) {
    const fy = 560, holes = [0, 1, 2, 3, 4].map((i) => rr(80 + i * 300, 50, 240, 180, 10));
    let s = wall(id, holes) + glass(id, holes) + holes.map((h) => `<path d="${h}" fill="none" stroke="${p.frame}" stroke-width="12"/>`).join("");
    s += floor(id, fy) + [0, 1, 2, 3, 4].map((i) => shaft(id, 80 + i * 300, 230, 240, fy, 160)).join("");
    const mark = mix(p.floor[0], "#5d9bc2", 0.35);
    s += `<ellipse cx="800" cy="780" rx="440" ry="92" fill="none" stroke="${mark}" stroke-width="6" opacity=".7"/><path d="M0 660H1600M800 660V900" stroke="${mark}" stroke-width="4" opacity=".5"/>`;
    // P7：左侧器械架与右侧沙袋原色偏深，调浅到与墙面同一明度区间，沙袋偏冰蓝。
    s += `<g filter="url(#${id}-soft)"><rect x="60" y="320" width="310" height="250" rx="10" fill="none" stroke="${tone(p, 0.24)}" stroke-width="12"/>`;
    for (let y = 370; y < 560; y += 46) s += `<rect x="70" y="${y}" width="290" height="8" fill="${tone(p, 0.2)}"/>`;
    s += `<path d="M1360 0V250" stroke="${tone(p, 0.28)}" stroke-width="5"/><rect x="1316" y="250" width="88" height="250" rx="40" fill="${mix(tone(p, 0.16), "#7fb0cf", 0.35)}"/></g>`;
    return { room: s, horizon: 200 };
  },
  medical(p, r, id) {
    const fy = 720, hole = rr(980, 110, 460, 400, 14);
    let s = wall(id, [hole]) + glass(id, [hole]) + `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="16"/>`;
    for (let y = 140; y < 500; y += 40) s += `<rect x="986" y="${y}" width="448" height="9" fill="${mix(p.frame, "#ffffff", 0.3)}" opacity=".7"/>`;
    s += floor(id, fy) + shaft(id, 990, 510, 440, fy, -260);
    const cur = mix(p.wall[0], "#cfe3ee", 0.6), curDeep = mix(cur, "#1f2d3a", 0.1), fold = `${id}-mfold`;
    s += `<defs>${lg(fold, [0, 0.12, 0.25, 0.37, 0.5, 0.62, 0.75, 0.87, 1].map((o, i) => [o, i % 2 ? curDeep : cur]), { ax1: 0, ay1: 0, ax2: 1, ay2: 0 })}</defs>`;
    s += `<rect x="60" y="86" width="620" height="8" rx="4" fill="${tone(p, 0.4)}"/><path d="M80 94H640Q660 420 620 800H100Q60 420 80 94Z" fill="url(#${fold})" opacity=".95"/>`;
    s += `<g filter="url(#${id}-soft)"><rect x="700" y="470" width="170" height="250" rx="10" fill="${mix(p.wall[0], "#ffffff", 0.45)}"/><path d="M785 520v40M765 540h40" stroke="#5d9bc2" stroke-width="8" stroke-linecap="round"/>`;
    s += `<rect x="900" y="600" width="40" height="160" rx="10" fill="${tone(p, 0.3)}"/><rect x="900" y="660" width="700" height="100" rx="20" fill="${mix(p.wall[0], "#ffffff", 0.6)}"/><rect x="920" y="760" width="16" height="110" fill="${tone(p, 0.32)}"/><rect x="1540" y="760" width="16" height="110" fill="${tone(p, 0.32)}"/></g>`;
    return { room: s, horizon: 420 };
  },
  corridor(p, r, id) {
    const far = { l: 640, r: 960, t: 330, b: 510 };
    const at = (s, side) => (side === "r" ? 1600 + (far.r - 1600) * s : far.l * s);
    const topAt = (s) => far.t * s, botAt = (s) => 900 + (far.b - 900) * s;
    const winQuads = [[0.04, 0.24], [0.33, 0.48], [0.55, 0.65], [0.7, 0.76], [0.79, 0.83]].map(([a, b]) => {
      const span = (s, k) => topAt(s) + (botAt(s) - topAt(s)) * k;
      return quad([[at(a, "r"), span(a, 0.24)], [at(b, "r"), span(b, 0.24)], [at(b, "r"), span(b, 0.7)], [at(a, "r"), span(a, 0.7)]]);
    });
    const endHole = rr(708, 362, 184, 112, 6);
    let s = `<path d="${quad([[0, 0], [1600, 0], [far.r, far.t], [far.l, far.t]])}" fill="${mix(p.wall[1], "#1f2d3a", 0.08)}"/>`;
    s += `<path d="${quad([[0, 900], [1600, 900], [far.r, far.b], [far.l, far.b]])}" fill="url(#${id}-floor)"/>`;
    s += `<path d="${quad([[0, 0], [far.l, far.t], [far.l, far.b], [0, 900]])}" fill="url(#${id}-wall)"/>`;
    s += `<path d="${rr(far.l, far.t, far.r - far.l, far.b - far.t)}${endHole}" fill="${p.wall[0]}" fill-rule="evenodd"/>`;
    s += `<path d="${quad([[1600, 0], [far.r, far.t], [far.r, far.b], [1600, 900]])}${winQuads.join("")}" fill="url(#${id}-wall)" fill-rule="evenodd"/>`;
    s += glass(id, [...winQuads, endHole]) + [...winQuads, endHole].map((q) => `<path d="${q}" fill="none" stroke="${p.frame}" stroke-width="8"/>`).join("");
    for (const [a, b] of [[0.1, 0.22], [0.42, 0.5], [0.64, 0.69]]) {
      s += `<path d="${quad([[at(a, "l"), topAt(a) + (botAt(a) - topAt(a)) * 0.2], [at(b, "l"), topAt(b) + (botAt(b) - topAt(b)) * 0.2], [at(b, "l"), botAt(b)], [at(a, "l"), botAt(a)]])}" fill="${tone(p, 0.22)}"/>`;
    }
    for (const [a, b] of [[0.02, 0.1], [0.28, 0.34], [0.5, 0.54], [0.66, 0.69], [0.76, 0.78]]) {
      const lw = (s2) => 160 * (1 - s2) + 30 * s2;
      s += `<path d="${quad([[800 - lw(a), topAt(a) * 0.98], [800 + lw(a), topAt(a) * 0.98], [800 + lw(b), topAt(b) * 0.98], [800 - lw(b), topAt(b) * 0.98]])}" fill="${mix(p.lamp, "#ffffff", 0.5)}" opacity="${(0.5 + p.lampOpacity * 0.5).toFixed(2)}"/>`;
    }
    for (const [a, b] of [[0.04, 0.24], [0.33, 0.48]]) {
      s += `<path d="${quad([[at(a, "r"), botAt(a)], [at(b, "r"), botAt(b)], [at(b, "r") - 260 * (1 - b), botAt(b)], [at(a, "r") - 420 * (1 - a), botAt(a)]])}" fill="${p.light}" opacity="${(p.lightOpacity * 0.6).toFixed(2)}"/>`;
    }
    s += lamp(p, id, 800, 120, 160, 0.9) + lamp(p, id, 800, 290, 90, 0.9);
    return { room: s, horizon: 470 };
  },
  generic(p, r, id) {
    const bld = mix(p.wall[1], p.near, 0.35);
    let s = `<g><rect x="-20" y="250" width="400" height="680" fill="${bld}"/><rect x="-30" y="226" width="430" height="40" rx="20" fill="${p.snow}"/>`;
    for (let y = 300; y < 760; y += 90) for (let x = 20; x < 360; x += 86) {
      if (y > 600 && x > 80 && x < 290) continue;
      const lit = r() < 0.6;
      s += `<rect x="${x}" y="${y}" width="56" height="54" rx="4" fill="${lit ? mix(p.lamp, "#ffffff", 0.3) : mix(bld, "#1f2d3a", 0.2)}" opacity="${lit ? (0.35 + p.lampOpacity * 0.6).toFixed(2) : "0.8"}"/>`;
    }
    const dx = 150, dy = 676, dw = 112, dh = 200, frame = mix(bld, "#1f2d3a", 0.3);
    const warm = mix("#ffcf86", p.lamp, 0.35), glowK = Math.max(0.5, p.lampOpacity);
    s += `<defs><radialGradient id="${id}-door"><stop offset="0" stop-color="${warm}" stop-opacity=".7"/><stop offset="1" stop-color="${warm}" stop-opacity="0"/></radialGradient>`
      + `<linearGradient id="${id}-doorin" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${mix(warm, "#ffffff", 0.45)}"/><stop offset="1" stop-color="${warm}"/></linearGradient></defs>`;
    s += `<circle cx="${dx + dw / 2}" cy="${dy + dh * 0.55}" r="170" fill="url(#${id}-door)" opacity="${glowK.toFixed(2)}"/>`;
    s += `<rect x="${dx - 14}" y="${dy - 12}" width="${dw + 28}" height="${dh + 12}" rx="6" fill="${frame}"/>`;
    s += `<rect class="push-target" data-push-target="" x="${dx}" y="${dy}" width="${dw}" height="${dh}" rx="4" fill="url(#${id}-doorin)" opacity="${(0.82 + p.lampOpacity * 0.18).toFixed(2)}"/>`;
    s += `<rect x="${dx + dw / 2 - 1.5}" y="${dy}" width="3" height="${dh}" fill="${frame}" opacity=".55"/>`;
    s += `<rect x="${dx - 30}" y="${dy - 34}" width="${dw + 60}" height="16" rx="6" fill="${mix(bld, "#1f2d3a", 0.38)}"/><rect x="${dx - 36}" y="${dy - 46}" width="${dw + 72}" height="16" rx="8" fill="${p.snow}"/>`;
    s += `<ellipse cx="${dx + dw / 2}" cy="${dy + dh + 4}" rx="130" ry="16" fill="${warm}" opacity="${(0.25 + p.lampOpacity * 0.3).toFixed(2)}"/>`;
    s += `</g>`;
    s += `<path d="M1000 900V520" stroke="${tone(p, 0.5)}" stroke-width="10"/><path d="M1000 524q40-30 80 0" stroke="${tone(p, 0.5)}" stroke-width="8" fill="none"/><circle cx="1080" cy="538" r="12" fill="${p.lamp}"/>` + lamp(p, id, 1080, 540, 160, 1.6);
    s += `<g fill="${tone(p, 0.4)}">`;
    for (let x = 460; x < 1640; x += 110) s += `<rect x="${x}" y="760" width="12" height="110"/>`;
    s += `<rect x="440" y="788" width="1200" height="8"/><rect x="440" y="826" width="1200" height="8"/></g>`;
    for (let x = 460; x < 1640; x += 110) s += `<ellipse cx="${x + 6}" cy="760" rx="14" ry="6" fill="${p.snow}"/>`;
    s += `<path d="M-20 900V840Q300 800 640 836T1300 826T1620 820V900Z" fill="${p.snow}"/>`;
    return { room: s, horizon: 600, outdoor: true };
  },
};

export function registerSnowPlaces(make) {
  Object.assign(ROOMS, make({ mix, f, lg, rg, rr, arch, quad, svg, ridge, pines, wall, glass, shaft, floor, tone, lamp, marks: ROOM_MARKS }));
}

function buildScene(key, time) {
  const p = PALETTES[time] || PALETTES.day;
  const builder = ROOMS[key];
  const id = `r-${key}`;
  buildPalette = p;
  const room = builder(p, rng(`${key}:room`), id);
  buildPalette = null;
  const far = outdoor(p, rng(`${key}:far`), room.horizon, room.land);
  return {
    far,
    room: svg(`<defs>${roomDefs(p, id)}</defs>${room.room}`, "room"),
    outdoor: Boolean(room.outdoor),
    flake: p.flake,
    lamp: p.lamp,
  };
}

// Canvas 雪
let sharedSprite = null;
function baseSprite() {
  if (sharedSprite) return sharedSprite;
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.35, "rgba(255,255,255,.85)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  sharedSprite = c;
  return c;
}
const tintCache = new Map();
function tintedSprite(color) {
  if (tintCache.has(color)) return tintCache.get(color);
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  g.drawImage(baseSprite(), 0, 0);
  g.globalCompositeOperation = "source-in";
  g.fillStyle = color;
  g.fillRect(0, 0, 64, 64);
  tintCache.set(color, c);
  return c;
}

const PRESETS = {
  far: { per: 9000, size: [1.2, 3.2], speed: [16, 40], sway: [6, 18], alpha: [0.5, 0.95], wind: 10 },
  near: { per: 70000, size: [5, 11], speed: [55, 110], sway: [10, 30], alpha: [0.45, 0.8], wind: 22 },
  motes: { per: 90000, size: [2, 4.5], speed: [-6, 6], sway: [8, 20], alpha: [0.25, 0.7], wind: 3 },
};

class SnowField {
  constructor(canvas, kind) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.kind = kind;
    this.color = "#ffffff";
    this.density = 1;
    this.particles = [];
    this.resize();
  }
  setKind(kind) { this.kind = kind; }
  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    this.w = w; this.h = h; this.dpr = dpr;
    this.canvas.width = Math.max(1, Math.round(w * dpr));
    this.canvas.height = Math.max(1, Math.round(h * dpr));
    this.spawn();
  }
  spawn() {
    const P = PRESETS[this.kind];
    const n = this.kind === "off" || !P ? 0 : Math.round(((this.w * this.h) / P.per) * this.density);
    const rnd = (a) => a[0] + Math.random() * (a[1] - a[0]);
    this.particles = Array.from({ length: n }, () => ({
      x: Math.random() * this.w, y: Math.random() * this.h, s: rnd(P.size), v: rnd(P.speed),
      a: rnd(P.alpha), sw: rnd(P.sway), ph: Math.random() * Math.PI * 2, fq: 0.3 + Math.random() * 0.6,
      // P8：夜间约 12% 的雪片带冰青闪光
      g: Math.random() < 0.12,
    }));
  }
  step(dt, t) {
    const P = PRESETS[this.kind];
    if (!P) return;
    for (const q of this.particles) {
      q.y += q.v * dt;
      q.x += P.wind * dt * (q.s / P.size[1]);
      if (q.y > this.h + 12) { q.y = -12; q.x = Math.random() * this.w; }
      if (q.y < -12) { q.y = this.h + 12; q.x = Math.random() * this.w; }
      if (q.x > this.w + 12) q.x = -12;
    }
    this.t = t;
  }
  draw() {
    const { ctx, dpr } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const img = tintedSprite(this.color);
    const glint = this.glint ? tintedSprite(this.glint) : null;
    const t = this.t || 0;
    for (const q of this.particles) {
      const x = q.x + Math.sin(t * q.fq + q.ph) * q.sw;
      const lit = glint && q.g;
      const a = this.kind === "motes" ? q.a * (0.55 + 0.45 * Math.sin(t * 1.3 * q.fq + q.ph))
        : lit ? Math.min(1, q.a * (0.7 + 0.5 * Math.abs(Math.sin(t * 2.2 * q.fq + q.ph)))) : q.a;
      ctx.globalAlpha = a;
      const d = q.s * 2;
      ctx.drawImage(lit ? glint : img, x - q.s, q.y - q.s, d, d);
    }
    ctx.globalAlpha = 1;
  }
}

function el(tag, cls) { const n = document.createElement(tag); n.className = cls; return n; }

/** 读取 app.js 写在 <html> 上的动态档位 */
function resolveMotion(level) {
  const raw = level || document.documentElement.dataset.motionLevel || "auto";
  if (raw === "off") return "off";
  if (raw === "light") return "light";
  return "full";
}

/** 在 stageEl 内建立分层世界（详见 docs/snow-adv-ui-notes.md） */
export function mountSnowWorld(stageEl, { visualKey = "generic", motionLevel, role = "stage" } = {}) {
  const far = el("div", "snow-layer snow-layer-far");
  const snowFar = el("canvas", "snow-layer snow-canvas snow-canvas-far");
  const room = el("div", "snow-layer snow-layer-room");
  const snowNear = el("canvas", "snow-layer snow-canvas snow-canvas-near");
  for (const node of [far, snowFar, room, snowNear]) {
    node.setAttribute("aria-hidden", "true");
    node.dataset.snowWorld = "";
  }
  const art = stageEl.querySelector("#stage-character-art");
  // 远景三层插在立绘之前（在它背后），近景雪插在立绘之后（在它前面）。
  if (art) stageEl.insertBefore(far, art); else stageEl.append(far);
  if (art) stageEl.insertBefore(snowFar, art); else stageEl.append(snowFar);
  if (art) stageEl.insertBefore(room, art); else stageEl.append(room);
  if (art && art.nextSibling) stageEl.insertBefore(snowNear, art.nextSibling); else stageEl.append(snowNear);

  const farField = new SnowField(snowFar, "far");
  const nearField = new SnowField(snowNear, "near");
  const fields = [farField, nearField];

  const world = { scene: visualKey, motion: resolveMotion(motionLevel), time: resolveTime(), timeAt: 0, raf: 0, last: 0, perfMs: 0, destroyed: false };

  function anySnow() { return fields.some((fld) => fld.particles.length); }
  function surfaceHidden() {
    if (document.hidden) return true;
    const surface = document.getElementById("in-person-surface");
    const cameraVisible = document.getElementById("chat-app")?.dataset.snowPushing === "1";
    if (role === "backdrop") {
      // 通讯器背后的世界
      const inPersonVisible = surface && !surface.hidden;
      if (inPersonVisible && !cameraVisible) return true;
      return !stageEl || stageEl.hidden || stageEl.offsetParent === null;
    }
    return !surface || surface.hidden || cameraVisible;
  }
  function loop(now) {
    if (world.loopCounted) { activeSnowLoopCount -= 1; world.loopCounted = false; }
    world.raf = 0;
    if (world.destroyed || surfaceHidden() || world.motion !== "full") return;
    const dt = Math.min(0.05, (now - (world.last || now)) / 1000);
    world.last = now;
    const t0 = performance.now();
    for (const fld of fields) { fld.step(dt, now / 1000); fld.draw(); }
    world.perfMs = world.perfMs * 0.92 + (performance.now() - t0) * 0.08;
    world.raf = requestAnimationFrame(loop);
    activeSnowLoopCount += 1; world.loopCounted = true;
  }
  function startLoop() {
    if (world.raf || world.destroyed || surfaceHidden()) return;
    if (world.motion !== "full" || !anySnow()) { for (const fld of fields) fld.draw(); return; }
    world.last = 0;
    world.raf = requestAnimationFrame(loop);
    activeSnowLoopCount += 1; world.loopCounted = true;
  }
  function stopLoop() {
    if (world.raf) { cancelAnimationFrame(world.raf); world.raf = 0; }
    if (world.loopCounted) { activeSnowLoopCount -= 1; world.loopCounted = false; }
  }

  function render() {
    if (world.destroyed) return;
    const now = performance.now();
    // 时间最多每 10 分钟刷新一次
    const override = document.documentElement.dataset.snowTime;
    if (override) { world.time = resolveTime(); world.timeAt = now; }
    else if (!world.timeAt || now - world.timeAt > 600000) { world.time = resolveTime(); world.timeAt = now; }
    const key = Object.hasOwn(ROOMS, world.scene) ? world.scene : "generic";
    const built = buildScene(key, world.time);
    far.dataset.time = world.time;
    far.dataset.scene = key;
    far.innerHTML = built.far;
    room.innerHTML = built.room;
    // generic 的这一层也包含基地建筑和亮灯入口，必须保留可见。
    room.hidden = false;
    const tier = world.motion;
    const glint = world.time === "night" ? "#9fe8ff" : null;
    farField.color = built.flake;
    farField.glint = glint;
    farField.density = tier === "light" ? 0.45 : 1;
    farField.setKind("far");
    farField.spawn();
    nearField.color = built.outdoor ? built.flake : built.lamp;
    nearField.glint = built.outdoor ? glint : null;
    nearField.density = 1;
    nearField.setKind(tier === "light" || tier === "off" ? "off" : built.outdoor ? "near" : "motes");
    nearField.spawn();
    stopLoop();
    startLoop();
    if (tier !== "full") for (const fld of fields) fld.draw();
  }

  function onVisibility() { if (surfaceHidden()) stopLoop(); else startLoop(); }
  function onResize() { for (const fld of fields) fld.resize(); if (world.motion !== "full") for (const fld of fields) fld.draw(); else startLoop(); }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("resize", onResize);

  // 相机推进
  let cancelCamera = null;
  function pushCamera({ onWhiteout } = {}) {
    cancelCamera?.();
    const target = room.querySelector(".push-target");
    const layers = [far, snowFar, room, snowNear];
    if (typeof far.animate !== "function" || !target) {
      // 不支持 Web Animations（或无入口）时退化为立即执行白光回调。
      if (!world.destroyed) onWhiteout?.();
      return Promise.resolve();
    }
    world.pushing = true;
    for (const el of layers) el.style.transform = "";
    const t = target.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const onScreen = t.right > 0 && t.left < vw && t.bottom > 0 && t.top < vh && t.width > 0;
    // 入口不在屏内（移动端竖屏裁掉建筑）时朝画面中心推进。
    const px = onScreen ? t.left + t.width / 2 : vw / 2;
    const py = onScreen ? t.top + t.height / 2 : vh * 0.62;
    const span = (el, s) => {
      const b = el.getBoundingClientRect();
      const clamp = (want, lo, hi) => Math.min(hi, Math.max(lo, want));
      return {
        origin: `${(px - b.left).toFixed(1)}px ${(py - b.top).toFixed(1)}px`,
        tx: clamp(vw / 2 - px, vw - px - (b.right - px) * s, (px - b.left) * s - px),
        ty: clamp(vh / 2 - py, vh - py - (b.bottom - py) * s, (py - b.top) * s - py),
      };
    };
    const ROOM_S = 6, FAR_S = 1.3;
    const rm = span(room, ROOM_S);
    room.style.transformOrigin = rm.origin;
    for (const el of [far, snowFar, snowNear]) el.style.transformOrigin = "50% 50%";
    const farMax = { x: (vw / 2) * (FAR_S - 1), y: (vh / 2) * (FAR_S - 1) };
    const fx = Math.max(-farMax.x, Math.min(farMax.x, rm.tx * 0.25));
    const fy = Math.max(-farMax.y, Math.min(farMax.y, rm.ty * 0.25));
    const opts = { duration: 1800, easing: "cubic-bezier(.45,0,.75,.3)", fill: "forwards" };
    const anims = [
      far.animate([{ transform: "translate(0,0) scale(1)" }, { transform: `translate(${fx.toFixed(1)}px,${fy.toFixed(1)}px) scale(${FAR_S})` }], opts),
      snowFar.animate([{ transform: "translate(0,0) scale(1)" }, { transform: `translate(${fx.toFixed(1)}px,${fy.toFixed(1)}px) scale(${FAR_S})` }], opts),
      room.animate([{ transform: "translate(0,0) scale(1)" }, { transform: `translate(${rm.tx.toFixed(1)}px,${rm.ty.toFixed(1)}px) scale(${ROOM_S})` }], opts),
      snowNear.animate([{ transform: "scale(1)", opacity: 1 }, { transform: "scale(2.6)", opacity: 0.35 }], opts),
    ];
    world.pushAnimations = anims;
    // 用数据属性标记正在推进，供证据截图/测试断言读取
    far.dataset.pushing = "1";
    // 硬性兜底
    let whiteoutTimer, finishTimer, settle;
    const finish = new Promise(resolve => { settle = resolve; });
    const clearPushing = () => {
      window.clearTimeout(whiteoutTimer);
      window.clearTimeout(finishTimer);
      anims.forEach((a) => { try { a.cancel(); } catch {} });
      for (const el of layers) { el.style.transformOrigin = ""; el.style.transform = ""; }
      delete far.dataset.pushing;
      world.pushing = false;
      world.pushAnimations = null;
      cancelCamera = null;
      settle();
    };
    cancelCamera = clearPushing;
    whiteoutTimer = window.setTimeout(() => {
      if (!world.destroyed && world.pushing) onWhiteout?.();
    }, opts.duration - 480);
    finishTimer = window.setTimeout(clearPushing, opts.duration + 400);
    Promise.all(anims.map((a) => a.finished.catch(() => {}))).then(() => {
      if (cancelCamera === clearPushing) clearPushing();
    });
    return finish;
  }

  render();
  const unsubscribeTime = subscribeSnowTime(() => { world.timeAt = 0; render(); });

  return {
    get root() { return stageEl; },
    get far() { return far; },
    get room() { return room; },
    get snowFar() { return snowFar; },
    get snowNear() { return snowNear; },
    get scene() { return world.scene; },
    isPushing() { return Boolean(world.pushing); },
    cancelPush() { cancelCamera?.(); },
    pushInto(key, options) {
      // 推进完成后由 onWhiteout 回调切场景
      const commit = () => { if (key && key !== world.scene) { world.scene = key; render(); } };
      const onWhiteout = options?.onWhiteout
        ? () => { const r = options.onWhiteout(); if (r === undefined) commit(); return r; }
        : commit;
      return pushCamera({ onWhiteout });
    },
    setScene(key) { if (key !== world.scene) { world.scene = key; render(); } },
    setMotion(level) {
      const next = resolveMotion(level);
      if (next === world.motion) { onVisibility(); return; }
      world.motion = next;
      render();
    },
    refresh() { render(); },
    getPerfMs() { return world.perfMs; },
    isRunning() { return Boolean(world.raf); },
    destroy() {
      world.destroyed = true;
      unsubscribeTime();
      cancelCamera?.();
      stopLoop();
      if (world.pushAnimations) { for (const a of world.pushAnimations) { try { a.cancel(); } catch {} } world.pushAnimations = null; }
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("resize", onResize);
      for (const node of [far, snowFar, room, snowNear]) node.remove();
      tintCache.clear();
    },
  };
}
