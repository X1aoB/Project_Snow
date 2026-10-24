// 独立场景的专属背景与换场白雪转场。首屏稳定后由 app.js 按需加载；背景注册进 snow-world.js 的场景表，
// 转场只增强已有的 #snow-scene-veil（覆盖 -> 标题卡场景徽记 -> 中心揭开）。说明见 docs/snow-adv-ui-notes.md。
import { registerSnowPlaces, setSnowTimeShift, checkSnowTime } from "/modules/snow-world.js";
import { createSnowIcon } from "/modules/snow-icons.js";
import { createSceneIcon } from "/modules/snow-scene-icons.js";

const STYLE_URL = "/modules/snow-scenes.css";
// 场景键 -> [区域编号, 标题卡徽记]。编号与地点胶囊、基地地图一致；附属设施与基地外统一为 EXT。
export const SCENES = Object.freeze({
  observation: ["B-01", "mountain-snow"], archive: ["B-02", "book-open"], quarters: ["B-03", "bed-double"], analyst: ["B-03", "monitor"],
  lounge: ["B-04", "sofa"], canteen: ["B-05", "utensils"], training: ["B-06", "dumbbell"], medical: ["B-07", "stethoscope"],
  corridor: ["B-08", "door-open"], hot_spring: ["EXT", "bath"], arcade: ["EXT", "gamepad-2"], bar: ["EXT", "glass-water"],
  healing: ["EXT", "leaf"], beach: ["EXT", "waves"], park: ["EXT", "trees"], street: ["EXT", "store"], mall: ["EXT", "shopping-bag"],
  generic: ["EXT", "snowflake"],
});
export const PLACE_KEYS = Object.freeze(["beach", "hot_spring", "arcade", "bar", "healing", "park", "street", "mall", "analyst"]);

function places(kit) {
  const { mix, f, lg, rg, rr, quad, wall, glass, shaft, floor, tone, lamp, marks } = kit;
  marks.analyst = ["B-03", "分析员", 1478, 168, 0.9, "v"];
  const wood = (p, k = 0) => mix(mix(p.wall[1], "#8c6a4c", 0.5), "#1f2d3a", k);
  const glow = (id, c) => rg(id, [[0, c, 0.9], [0.35, c, 0.4], [1, c, 0]]);
  const night = (p) => Boolean(p.stars > 0.5);
  // 带雪顶的圆石。
  const rock = (p, x, y, w, h) => {
    const body = mix(mix(p.mid[1], "#8d9096", 0.55), p.near, night(p) ? 0.35 : 0.12);
    return `<path d="M${f(x)} ${f(y + h)}Q${f(x - w * 0.06)} ${f(y + h * 0.3)} ${f(x + w * 0.3)} ${f(y + h * 0.08)}Q${f(x + w * 0.62)} ${f(y - h * 0.08)} ${f(x + w * 0.9)} ${f(y + h * 0.26)}Q${f(x + w * 1.06)} ${f(y + h * 0.7)} ${f(x + w)} ${f(y + h)}Z" fill="${body}"/>`
      + `<path d="M${f(x + w * 0.06)} ${f(y + h * 0.36)}Q${f(x + w * 0.3)} ${f(y)} ${f(x + w * 0.6)} ${f(y + h * 0.02)}Q${f(x + w * 0.86)} ${f(y + h * 0.1)} ${f(x + w * 0.94)} ${f(y + h * 0.36)}Q${f(x + w * 0.6)} ${f(y + h * 0.2)} ${f(x + w * 0.06)} ${f(y + h * 0.36)}Z" fill="${p.snow}" opacity=".92"/>`;
  };
  // 积雪的落叶树：枝干逐级分叉，枝上压一道雪。
  const bareTree = (p, r, x, base, h) => {
    const bark = mix(p.near, "#3b3440", 0.25);
    let limbs = "", snow = "";
    const grow = (bx, by, ang, len, w, depth) => {
      const ex = bx + Math.cos(ang) * len, ey = by + Math.sin(ang) * len;
      limbs += `<path d="M${f(bx)} ${f(by)}L${f(ex)} ${f(ey)}" stroke-width="${f(w)}"/>`;
      if (depth > 1 && Math.sin(ang) < -0.2) snow += `<path d="M${f(bx)} ${f(by - w * 0.6)}L${f(ex)} ${f(ey - w * 0.6)}" stroke-width="${f(Math.max(1.5, w * 0.55))}"/>`;
      if (!depth) return;
      for (const turn of [-0.42 - r() * 0.2, 0.36 + r() * 0.24]) grow(ex, ey, ang + turn, len * (0.66 + r() * 0.1), w * 0.62, depth - 1);
    };
    grow(x, base, -Math.PI / 2 + (r() - 0.5) * 0.12, h * 0.36, h * 0.05, 4);
    return `<g stroke="${bark}" stroke-linecap="round" fill="none">${limbs}</g><g stroke="${p.snow}" stroke-linecap="round" fill="none" opacity=".9">${snow}</g>`
      + `<ellipse cx="${f(x)}" cy="${f(base + 4)}" rx="${f(h * 0.2)}" ry="${f(h * 0.03)}" fill="${p.snow}"/>`;
  };
  const lampPost = (p, id, x, base, h) => {
    const pole = tone(p, 0.55), w = Math.max(3, h * 0.022);
    return `<rect x="${f(x - w / 2)}" y="${f(base - h)}" width="${f(w)}" height="${f(h)}" fill="${pole}"/>`
      + `<path d="M${f(x - h * 0.06)} ${f(base - h)}h${f(h * 0.12)}l${f(-h * 0.02)} ${f(h * 0.07)}h${f(-h * 0.08)}z" fill="${pole}"/>`
      + `<rect x="${f(x - h * 0.03)}" y="${f(base - h + h * 0.07)}" width="${f(h * 0.06)}" height="${f(h * 0.05)}" rx="2" fill="${p.lamp}" opacity="${(0.55 + p.lampOpacity * 0.45).toFixed(2)}"/>`
      + `<rect x="${f(x - h * 0.07)}" y="${f(base - h - 5)}" width="${f(h * 0.14)}" height="6" rx="3" fill="${p.snow}"/>`
      + lamp(p, id, f(x), f(base - h + h * 0.1), f(h * 0.5), 1.4);
  };

  // 海滩的远景：雪岛托着世界树，左侧海岬上有一座小灯塔，海面上一条太阳（夜里是月亮）的光带和几道浪纹。
  function beachLand(p, r, H) {
    const top = mix(p.sky[2], p.far[1], 0.45), deep = mix(p.far[1], p.near, 0.55);
    let s = `<defs><linearGradient id="b-sea" gradientUnits="userSpaceOnUse" x1="0" y1="${H}" x2="0" y2="900"><stop offset="0" stop-color="${top}"/><stop offset=".4" stop-color="${mix(top, deep, 0.5)}"/><stop offset="1" stop-color="${deep}"/></linearGradient></defs>`;
    const isle = mix(p.far[0], p.far[1], 0.5);
    s += `<path d="M360 ${H + 2}Q440 ${H - 60} 520 ${H - 104}Q600 ${H - 140} 680 ${H - 106}Q760 ${H - 60} 860 ${H + 2}Z" fill="${isle}"/>`;
    s += `<path d="M430 ${H - 40}Q520 ${H - 110} 600 ${H - 136}Q680 ${H - 112} 780 ${H - 40}Q690 ${H - 86} 600 ${H - 100}Q510 ${H - 86} 430 ${H - 40}Z" fill="${p.snow}" opacity=".85"/>`;
    const cape = mix(p.mid[1], p.far[1], 0.35);
    s += `<path d="M-20 ${H + 2}V${H - 74}Q70 ${H - 116} 160 ${H - 98}Q260 ${H - 72} 340 ${H - 30}Q400 ${H - 8} 460 ${H + 2}Z" fill="${cape}"/>`;
    s += `<path d="M-20 ${H - 74}Q70 ${H - 116} 160 ${H - 98}Q260 ${H - 72} 340 ${H - 30}Q250 ${H - 60} 160 ${H - 80}Q70 ${H - 96} -20 ${H - 56}Z" fill="${p.snow}"/>`;
    s += `<rect x="132" y="${H - 168}" width="16" height="74" fill="${mix(p.wall[0], p.mid[1], 0.25)}"/><path d="M128 ${H - 168}h24l-4 -12h-16z" fill="${mix(p.near, "#c9483e", 0.4)}"/>`;
    s += `<circle cx="140" cy="${H - 150}" r="${p.stars ? 30 : 16}" fill="${p.lamp}" opacity="${(0.2 + p.lampOpacity * 0.4).toFixed(2)}"/><circle cx="140" cy="${H - 150}" r="5" fill="${p.lamp}"/>`;
    s += `<rect y="${H}" width="1600" height="${900 - H}" fill="url(#b-sea)"/>`;
    s += `<rect y="${H}" width="1600" height="3" fill="#ffffff" opacity=".35"/>`;
    const sx = p.sunPos[0] * 1600;
    for (let i = 0; i < 54; i++) {
      const t = r(), y = H + 6 + t * t * (900 - H - 10), w = 8 + t * 80 * (0.4 + r());
      s += `<rect x="${f(sx - w / 2 + (r() - 0.5) * (24 + t * 260))}" y="${f(y)}" width="${f(w)}" height="${f(1.4 + t * 2.6)}" rx="1" fill="${p.sun}" opacity="${(0.3 + r() * 0.5).toFixed(2)}"/>`;
    }
    for (let i = 0; i < 12; i++) {
      const t = (i + r()) / 12, y = H + 10 + t * t * (900 - H), x0 = r() * 1400 - 100, len = 120 + t * 360;
      s += `<path d="M${f(x0)} ${f(y)}q${f(len / 4)} ${f(-3 - t * 5)} ${f(len / 2)} 0t${f(len / 2)} 0" stroke="#ffffff" stroke-width="${f(1 + t * 2.5)}" fill="none" opacity="${(0.12 + t * 0.2).toFixed(2)}"/>`;
    }
    return s;
  }

  return {
    // 基地海滩：远处是世界树所在的雪岛与海岬灯塔，近处是沙滩、浪花、礁石、遮阳伞和救生瞭望台。
    beach(p, r, id) {
      const sand = mix(p.ground[0], "#efd8b3", 0.45), sandDeep = mix(p.ground[1], "#d6ba92", 0.42);
      let s = `<defs>${lg(`${id}-sand`, [[0, sand], [1, sandDeep]], { y1: 690, y2: 900 })}${lg(`${id}-wet`, [[0, mix(sand, p.far[1], 0.4)], [1, sand]], { y1: 694, y2: 740 })}</defs>`;
      s += `<path d="M-20 900V716Q240 700 520 710T1120 702T1620 712V900Z" fill="url(#${id}-sand)"/>`;
      s += `<path d="M-20 716Q240 700 520 710T1120 702T1620 712V734Q1200 724 800 734T-20 738Z" fill="url(#${id}-wet)"/>`;
      s += `<path d="M-20 713Q150 700 320 708T660 704T1000 710T1340 702T1620 709" stroke="${p.snow}" stroke-width="5" fill="none" stroke-linecap="round" opacity=".9"/>`;
      s += `<path d="M120 728Q300 720 470 726M880 724Q1040 716 1220 722" stroke="${p.snow}" stroke-width="3" fill="none" stroke-linecap="round" opacity=".55"/>`;
      // 两行并排的脚印：和她一起走过来的路。
      for (let i = 0; i < 12; i++) {
        const t = i / 11, side = i % 2 ? 1 : -1;
        for (const lane of [0, 64]) {
          const x = 600 + lane * (1 - t * 0.6) + t * 220 + side * 9 * (1 - t * 0.5), y = 892 - t * 150;
          s += `<ellipse cx="${f(x)}" cy="${f(y)}" rx="${f(8 - t * 4)}" ry="${f(4 - t * 2)}" fill="${mix(sandDeep, p.near, 0.3)}" opacity=".42"/>`;
        }
      }
      s += `<path d="M-20 900V830Q100 806 240 832T420 900Z" fill="${p.snow}"/><path d="M1620 900V818Q1500 798 1370 828T1200 900Z" fill="${p.snow}"/>`;
      s += `<g filter="url(#${id}-soft)">` + rock(p, -40, 650, 260, 130) + rock(p, 170, 700, 120, 60) + rock(p, 1400, 670, 240, 110);
      // 遮阳伞（伞面压着薄雪）与两张躺椅。
      const canvas = night(p) ? mix("#c9b6d8", p.wall[1], 0.5) : mix("#f3c9a8", p.wall[0], 0.3), stripe = mix(canvas, "#e98a7a", 0.5);
      s += `<rect x="318" y="430" width="7" height="300" fill="${tone(p, 0.45)}"/>`;
      s += `<path d="M160 470Q322 360 484 470Q440 452 402 470Q362 452 322 470Q282 452 242 470Q202 452 160 470Z" fill="${canvas}"/>`;
      s += `<path d="M242 470Q262 410 322 386Q302 420 302 470ZM402 470Q382 410 322 386Q342 420 342 470Z" fill="${stripe}" opacity=".7"/>`;
      s += `<path d="M196 444Q322 360 448 444Q322 392 196 444Z" fill="${p.snow}" opacity=".95"/>`;
      for (const x of [176, 352]) {
        const frame = wood(p, 0.2), cloth = mix(canvas, "#ffffff", 0.25);
        s += `<path d="M${x} 706L${x + 22} 662M${x + 120} 706L${x + 104} 662M${x + 6} 690L${x + 10} 706" stroke="${frame}" stroke-width="6" stroke-linecap="round"/>`;
        s += `<path d="M${x + 14} 664H${x + 112}L${x + 120} 652H${x + 40}L${x + 8} 604L${x - 4} 612Z" fill="${cloth}" stroke="${frame}" stroke-width="4" stroke-linejoin="round"/>`;
        s += `<path d="M${x + 4} 618L${x + 30} 656H${x + 108}" stroke="${stripe}" stroke-width="5" fill="none" opacity=".55"/>`;
      }
      // 救生瞭望台。
      const post = wood(p, 0.1), deck = wood(p, 0.22);
      s += `<path d="M1240 720L1270 470M1400 720L1370 470M1250 640H1390M1262 560H1378" stroke="${post}" stroke-width="10" fill="none"/>`;
      s += `<rect x="1228" y="452" width="184" height="22" rx="4" fill="${deck}"/><rect x="1250" y="360" width="140" height="92" rx="6" fill="${mix(p.wall[0], "#e7eef4", 0.4)}"/>`;
      s += `<rect x="1272" y="384" width="96" height="44" rx="4" fill="${mix(p.sky[1], p.far[1], 0.4)}" opacity=".85"/>`;
      s += `<path d="M1232 362L1320 316L1408 362Z" fill="${mix("#d9634f", p.wall[1], 0.35)}"/><path d="M1240 356L1320 314L1400 356Q1320 330 1240 356Z" fill="${p.snow}"/>`;
      s += `<path d="M1320 316V270" stroke="${post}" stroke-width="4"/><path d="M1322 272h40l-10 12 10 12h-40z" fill="${mix("#e9a34a", p.wall[1], 0.3)}"/></g>`;
      s += lamp(p, id, 1320, 404, 150, night(p) ? 1.2 : 0.4);
      return { room: s, horizon: 500, outdoor: true, land: beachLand };
    },
    // 基地温泉：露天汤池、热气、围着的石头，木栅和挂着灯笼的檐。
    hot_spring(p, r, id) {
      const water = mix(p.sky[2], "#86c9cf", 0.5), deep = mix(p.far[1], "#3f8592", 0.45);
      let s = `<defs>${lg(`${id}-water`, [[0, water], [1, deep]], { y1: 600, y2: 780 })}<filter id="${id}-steam" x="-50%" y="-80%" width="200%" height="260%"><feGaussianBlur stdDeviation="24"/></filter></defs>`;
      s += `<path d="M-20 900V572Q400 540 800 556T1620 566V900Z" fill="${p.snow}"/>`;
      // 木栅：左右各一段，栅顶积雪。
      for (const [x0, x1] of [[-20, 330], [1270, 1620]]) {
        for (let x = x0; x < x1; x += 34) {
          const top = 300 + Math.abs(x - (x0 < 0 ? x1 : x0)) * 0.05 + r() * 10;
          s += `<rect x="${x}" y="${f(top)}" width="30" height="${f(680 - top)}" fill="${wood(p, (x / 34) % 2 ? 0.08 : 0)}"/><rect x="${x - 2}" y="${f(top - 8)}" width="34" height="12" rx="6" fill="${p.snow}"/>`;
        }
        s += `<rect x="${x0}" y="420" width="${x1 - x0}" height="12" fill="${wood(p, 0.25)}"/><rect x="${x0}" y="560" width="${x1 - x0}" height="12" fill="${wood(p, 0.25)}"/>`;
      }
      // 檐与灯笼。
      s += `<path d="M1150 250L1640 196V262L1180 300Z" fill="${wood(p, 0.3)}"/><path d="M1140 252L1640 192V206L1150 262Z" fill="${p.snow}"/>`;
      for (const x of [1280, 1480]) {
        s += `<path d="M${x} 286V316" stroke="${tone(p, 0.5)}" stroke-width="3"/><rect x="${x - 22}" y="316" width="44" height="62" rx="18" fill="${mix(p.lamp, "#ff9a62", 0.35)}" opacity="${(0.6 + p.lampOpacity * 0.4).toFixed(2)}"/>`;
        s += `<rect x="${x - 16}" y="312" width="32" height="8" rx="3" fill="${tone(p, 0.5)}"/>` + lamp(p, id, x, 348, 120, 1.6);
      }
      // 汤池、石沿与水面反光。
      s += `<path d="M330 650Q350 560 800 552Q1250 560 1270 650Q1250 740 800 748Q350 740 330 650Z" fill="url(#${id}-water)"/>`;
      for (let i = 0; i < 7; i++) s += `<path d="M${f(450 + r() * 560)} ${f(592 + i * 20)}h${f(60 + r() * 120)}" stroke="#ffffff" stroke-width="3" stroke-linecap="round" opacity="${(0.2 + r() * 0.24).toFixed(2)}"/>`;
      for (let i = 0; i < 18; i++) {
        const a = Math.PI + (i / 17) * Math.PI, w = 74 + r() * 50;
        s += rock(p, 800 + Math.cos(a) * 500 - w / 2, 650 + Math.sin(a) * 100 - 30, w, 44 + r() * 16);
      }
      s += rock(p, 250, 660, 160, 96) + rock(p, 1200, 650, 180, 100);
      // 热气：贴着水面升起，夜里更显眼。
      const k = night(p) ? 0.42 : 0.3 + p.lampOpacity * 0.2;
      s += `<g filter="url(#${id}-steam)" fill="${night(p) ? "#e8eefc" : "#ffffff"}">`;
      for (let i = 0; i < 9; i++) s += `<ellipse cx="${f(440 + r() * 720)}" cy="${f(430 + r() * 150)}" rx="${f(90 + r() * 110)}" ry="${f(46 + r() * 50)}" opacity="${(k + r() * 0.16).toFixed(2)}"/>`;
      s += `</g><g fill="none" stroke="#ffffff" stroke-linecap="round" opacity="${(k + 0.1).toFixed(2)}">`;
      for (const x of [520, 700, 900, 1080]) s += `<path d="M${x} 560q-18 -40 0 -80t0 -80" stroke-width="5"/>`;
      return { room: s + "</g>", horizon: 520, outdoor: true };
    },
    // 基地游戏厅：偏暗的厅里霓虹灯牌、两侧街机与抓娃娃机，高处一条窄窗看得到外面的雪。
    arcade(p, r, id) {
      const holes = [rr(160, 56, 400, 96, 10), rr(1040, 56, 400, 96, 10)], fy = 640;
      const cyan = "#5fd6d0", pink = "#ee86c3", amber = "#ffc46b", dim = "#191433";
      let s = `<defs>${glow(`${id}-cy`, cyan)}${glow(`${id}-pk`, pink)}${lg(`${id}-scr`, [[0, mix(cyan, "#ffffff", 0.25)], [1, mix(pink, "#6a5bd6", 0.35)]])}</defs>`;
      s += wall(id, holes) + glass(id, holes) + holes.map((h) => `<path d="${h}" fill="none" stroke="${p.frame}" stroke-width="12"/>`).join("");
      s += `<path d="M0 0H1600V900H0Z${holes.join("")}" fill="${dim}" fill-rule="evenodd" opacity="${night(p) ? 0.42 : 0.3}"/>`;
      s += floor(id, fy) + `<rect y="${fy}" width="1600" height="${900 - fy}" fill="${dim}" opacity=".45"/>`;
      for (let i = 0; i < 46; i++) s += `<circle cx="${f(r() * 1600)}" cy="${f(fy + 20 + r() * (900 - fy))}" r="${f(2 + r() * 3)}" fill="${[cyan, pink, amber][i % 3]}" opacity=".35"/>`;
      // 天花霓虹灯带。
      s += `<path d="M0 196H1600" stroke="${pink}" stroke-width="5" opacity=".75"/><path d="M0 214H1600" stroke="${cyan}" stroke-width="3" opacity=".6"/>`;
      // 中间的霓虹灯牌。
      s += `<circle cx="800" cy="300" r="260" fill="url(#${id}-pk)" opacity=".35"/>`;
      s += `<rect x="620" y="236" width="360" height="124" rx="22" fill="${mix(dim, "#000000", 0.2)}" stroke="${pink}" stroke-width="6"/>`;
      s += `<text x="800" y="318" text-anchor="middle" font-family="ui-monospace, Consolas, monospace" font-weight="700" font-size="64" letter-spacing="14" fill="none" stroke="${cyan}" stroke-width="4">PLAY</text>`;
      s += `<text x="800" y="392" text-anchor="middle" font-family="'LXGW WenKai Screen', 'Microsoft YaHei', sans-serif" font-size="24" letter-spacing="10" fill="${amber}" opacity=".9">游戏厅</text>`;
      // 两侧街机，近大远小。
      const cabinet = (x, k, flip) => {
        const w = 150 * k, h = 330 * k, y = fy + 70 * k - h, body = mix(dim, p.wall[1], 0.18), side = mix(dim, "#000000", 0.25);
        let c = `<g transform="translate(${f(x)} ${f(y)})${flip ? ` scale(-1 1)` : ""}">`;
        c += `<path d="M0 ${f(h)}V${f(h * 0.12)}L${f(w * 0.12)} 0H${f(w)}V${f(h)}Z" fill="${body}"/><path d="M${f(w)} 0h${f(w * 0.16)}V${f(h)}H${f(w)}Z" fill="${side}"/>`;
        c += `<rect x="${f(w * 0.14)}" y="${f(h * 0.03)}" width="${f(w * 0.8)}" height="${f(h * 0.1)}" rx="3" fill="${flip ? pink : amber}" opacity=".85"/>`;
        c += `<rect x="${f(w * 0.12)}" y="${f(h * 0.2)}" width="${f(w * 0.82)}" height="${f(h * 0.32)}" rx="6" fill="url(#${id}-scr)"/>`;
        c += `<path d="M${f(w * 0.02)} ${f(h * 0.62)}H${f(w * 1.04)}L${f(w * 0.98)} ${f(h * 0.7)}H${f(w * 0.06)}Z" fill="${mix(body, "#ffffff", 0.12)}"/>`;
        c += `<circle cx="${f(w * 0.3)}" cy="${f(h * 0.655)}" r="${f(w * 0.05)}" fill="${pink}"/><circle cx="${f(w * 0.62)}" cy="${f(h * 0.655)}" r="${f(w * 0.045)}" fill="${cyan}"/><circle cx="${f(w * 0.78)}" cy="${f(h * 0.655)}" r="${f(w * 0.045)}" fill="${amber}"/>`;
        return c + "</g>";
      };
      s += `<g filter="url(#${id}-soft)">` + cabinet(30, 1.2, false) + cabinet(230, 1, false) + cabinet(400, 0.82, false) + cabinet(1570, 1.2, true) + cabinet(1370, 1, true);
      // 抓娃娃机。
      s += `<rect x="1050" y="380" width="160" height="300" rx="8" fill="${mix(dim, p.wall[1], 0.22)}"/><rect x="1066" y="400" width="128" height="170" rx="4" fill="${mix(cyan, "#ffffff", 0.6)}" opacity=".32"/>`;
      s += `<path d="M1130 400V448M1116 448h28l-6 14M1116 448l6 14" stroke="${tone(p, 0.2)}" stroke-width="3" fill="none"/>`;
      for (const [x, y, c] of [[1090, 548, pink], [1124, 552, amber], [1160, 546, cyan], [1106, 528, amber], [1146, 526, pink]]) s += `<circle cx="${x}" cy="${y}" r="16" fill="${c}" opacity=".85"/>`;
      s += `<rect x="1050" y="380" width="160" height="16" rx="4" fill="${pink}" opacity=".8"/></g>`;
      s += `<circle cx="300" cy="420" r="200" fill="url(#${id}-cy)" opacity=".28"/><circle cx="1300" cy="420" r="200" fill="url(#${id}-pk)" opacity=".24"/>`;
      return { room: s, horizon: 250 };
    },
    // 基地酒吧：暖光吊灯、长吧台与高脚凳、背后层架上的玻璃器皿，右侧大窗外在下雪。
    bar(p, r, id) {
      const hole = rr(1120, 110, 400, 410, 12), fy = 700, warm = mix(p.lamp, "#ffb36b", 0.4);
      let s = wall(id, [hole]) + glass(id, [hole]) + `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="14"/><rect x="1316" y="110" width="8" height="410" fill="${p.frame}"/>`;
      s += `<path d="M0 0H1600V900H0Z${hole}" fill="${mix(warm, "#5a3a28", 0.5)}" fill-rule="evenodd" opacity="${night(p) ? 0.3 : 0.16}"/>`;
      s += floor(id, fy) + shaft(id, 1130, 520, 380, fy, -200);
      s += `<defs>${lg(`${id}-back`, [[0, warm, 0], [0.5, warm, 0.5], [1, warm, 0]], { ax1: 0, ay1: 0, ax2: 1, ay2: 0 })}</defs>`;
      // 背后层架与背光。
      s += `<rect x="120" y="200" width="920" height="320" rx="10" fill="${wood(p, 0.35)}"/><rect x="140" y="214" width="880" height="292" fill="url(#${id}-back)" opacity="${(0.3 + p.lampOpacity * 0.4).toFixed(2)}"/>`;
      const tints = ["#d9a35a", "#7fb4a8", "#c98a9a", "#b8c8d8", "#e2c58a"];
      for (const y of [306, 404, 500]) {
        let x = 160;
        while (x < 1000) {
          const c = mix(tints[Math.floor(r() * tints.length)], p.wall[0], 0.3), tall = r() < 0.55;
          if (tall) {
            const h = 60 + r() * 26;
            s += `<path d="M${f(x)} ${y}V${f(y - h * 0.6)}Q${f(x)} ${f(y - h * 0.72)} ${f(x + 8)} ${f(y - h * 0.76)}V${f(y - h)}h10V${f(y - h * 0.76)}Q${f(x + 26)} ${f(y - h * 0.72)} ${f(x + 26)} ${f(y - h * 0.6)}V${y}Z" fill="${c}" opacity=".85"/>`;
            x += 36 + r() * 10;
          } else {
            s += `<path d="M${f(x)} ${f(y - 34)}h26l-4 34h-18z" fill="${mix(c, "#ffffff", 0.4)}" opacity=".6"/>`;
            x += 32 + r() * 8;
          }
        }
        s += `<rect x="130" y="${y}" width="900" height="10" fill="${wood(p, 0.45)}"/>`;
      }
      // 吧台、踏脚铜杆与高脚凳。
      s += `<g filter="url(#${id}-soft)"><rect x="40" y="582" width="1240" height="26" rx="8" fill="${wood(p, 0.05)}"/><rect x="60" y="608" width="1200" height="170" fill="${wood(p, 0.3)}"/>`;
      for (let x = 100; x < 1260; x += 120) s += `<rect x="${x}" y="624" width="96" height="138" rx="6" fill="${wood(p, 0.22)}"/>`;
      s += `<rect x="60" y="760" width="1200" height="6" fill="${mix(warm, "#b88a3e", 0.5)}"/>`;
      for (const x of [220, 470, 1000, 1220]) {
        s += `<ellipse cx="${x}" cy="740" rx="52" ry="14" fill="${mix(p.wall[1], "#3c2a2a", 0.4)}"/><rect x="${x - 5}" y="740" width="10" height="130" fill="${tone(p, 0.5)}"/><ellipse cx="${x}" cy="872" rx="34" ry="8" fill="${tone(p, 0.5)}"/>`;
      }
      // 小黑板（只画几道粉笔线，不写字）。
      s += `<rect x="1300" y="560" width="190" height="130" rx="6" fill="${mix("#2d3b38", p.wall[1], 0.25)}" stroke="${wood(p, 0.2)}" stroke-width="8"/>`;
      for (let i = 0; i < 4; i++) s += `<path d="M1324 ${596 + i * 24}h${f(70 + r() * 70)}" stroke="#f3efe6" stroke-width="3" stroke-linecap="round" opacity=".55"/>`;
      s += "</g>";
      for (const x of [240, 500, 760, 1020]) {
        s += `<path d="M${x} 0V150" stroke="${tone(p, 0.5)}" stroke-width="2"/><path d="M${x - 42} 196Q${x} 132 ${x + 42} 196Z" fill="${wood(p, 0.3)}"/><ellipse cx="${x}" cy="196" rx="30" ry="6" fill="${warm}"/>`;
        s += lamp(p, id, x, 210, 170, 1.7);
      }
      return { room: s, horizon: 430 };
    },
    // 基地疗愈中心：正中一扇圆窗，两侧木格栅，石墙水幕、置物架上的蜡烛与叠石，零重力躺椅和绿植，整体偏鼠尾草绿的安静空间。
    healing(p, r, id) {
      const hole = "M590 320a210 210 0 1 0 420 0a210 210 0 1 0 -420 0Z", fy = 690, sage = mix(p.wall[0], "#b9dfcd", 0.62);
      let s = wall(id, [hole]) + glass(id, [hole]);
      s += `<path d="M0 0H1600V900H0Z${hole}" fill="${sage}" fill-rule="evenodd" opacity="${night(p) ? 0.16 : 0.5}"/>`;
      // 圆窗两侧的竖向木格栅。
      for (const x0 of [470, 1050]) for (let i = 0; i < 5; i++) s += `<rect x="${x0 + i * 17}" y="96" width="10" height="560" rx="3" fill="${wood(p, 0.05 + (i % 2) * 0.06)}" opacity=".9"/>`;
      s += `<circle cx="800" cy="320" r="214" fill="none" stroke="${p.frame}" stroke-width="18"/><circle cx="800" cy="320" r="238" fill="none" stroke="${mix(p.frame, "#ffffff", 0.4)}" stroke-width="4" opacity=".7"/>`;
      s += floor(id, fy) + shaft(id, 640, 520, 320, fy, 0);
      for (let x = 0; x < 1600; x += 80) s += `<path d="M${x} ${fy}L${f(800 + (x - 800) * 1.6)} 900" stroke="${mix(p.floor[1], "#1f2d3a", 0.08)}" stroke-width="2" opacity=".35"/>`;
      s += `<ellipse cx="800" cy="822" rx="430" ry="56" fill="${mix(sage, "#ffffff", 0.35)}" opacity=".6"/>`;
      s += `<rect x="560" y="18" width="480" height="40" rx="20" fill="${mix(p.lamp, "#ffffff", 0.6)}" opacity="${(0.55 + p.lampOpacity * 0.4).toFixed(2)}"/>` + lamp(p, id, 800, 40, 260, 1);
      const leaf = mix(tone(p, 0.3), "#6fae8f", 0.55), leafDeep = mix(leaf, "#1f2d3a", 0.18), stone = mix(p.wall[1], "#8f9a96", 0.45);
      s += `<g filter="url(#${id}-soft)">`;
      // 左侧石墙水幕与石盆。
      s += `<rect x="250" y="150" width="170" height="510" rx="8" fill="${stone}"/>`;
      for (let y = 170; y < 650; y += 46) s += `<path d="M262 ${y}h${f(50 + r() * 60)}M${f(330 + r() * 30)} ${y + 22}h${f(40 + r() * 40)}" stroke="${mix(stone, "#1f2d3a", 0.12)}" stroke-width="3" opacity=".5"/>`;
      s += `<rect x="296" y="160" width="78" height="490" rx="4" fill="#ffffff" opacity=".26"/><path d="M312 170v470M338 170v470M360 170v470" stroke="#ffffff" stroke-width="2" opacity=".35"/>`;
      s += `<path d="M228 650h214l-18 44H246z" fill="${mix(stone, "#1f2d3a", 0.1)}"/><ellipse cx="335" cy="652" rx="92" ry="9" fill="${mix(p.sky[1], "#b6e1e6", 0.5)}"/>`;
      // 右侧置物架：蜡烛与叠石。
      for (const y of [300, 410]) s += `<rect x="1180" y="${y}" width="230" height="12" rx="4" fill="${wood(p, 0.15)}"/>`;
      for (const x of [1200, 1240, 1290]) s += `<rect x="${x}" y="${270 + (x % 3) * 4}" width="20" height="${30 - (x % 3) * 4}" rx="4" fill="${mix(p.wall[0], "#fff4e2", 0.6)}"/><ellipse cx="${x + 10}" cy="${264 + (x % 3) * 4}" rx="3" ry="6" fill="${p.lamp}"/>` + lamp(p, id, x + 10, 266, 46, 2.2);
      for (const [w, y] of [[56, 398], [44, 384], [32, 372], [22, 362]]) s += `<ellipse cx="1350" cy="${y}" rx="${w / 2}" ry="8" fill="${mix(stone, "#ffffff", 0.15)}"/>`;
      s += `<path d="M1220 410V352h70v58" fill="${mix(sage, "#ffffff", 0.4)}"/><path d="M1255 352c-24-30-6-54 0-60c6 6 24 30 0 60z" fill="${leaf}"/>`;
      // 零重力躺椅：弯曲的软垫搭在细框上，左右各一。
      for (const [x, d] of [[90, 1], [1510, -1]]) {
        const pad = mix(p.wall[0], "#efe6d8", 0.5), padDeep = mix(pad, "#1f2d3a", 0.14);
        s += `<path d="M${x} 640Q${x + d * 60} 720 ${x + d * 170} 716Q${x + d * 250} 712 ${x + d * 320} 650" stroke="${padDeep}" stroke-width="40" stroke-linecap="round" fill="none"/>`;
        s += `<path d="M${x} 630Q${x + d * 60} 706 ${x + d * 170} 702Q${x + d * 250} 698 ${x + d * 320} 638" stroke="${pad}" stroke-width="30" stroke-linecap="round" fill="none"/>`;
        s += `<path d="M${x + d * 120} 730L${x + d * 70} 820M${x + d * 220} 730L${x + d * 280} 820M${x + d * 70} 820H${x + d * 280}" stroke="${tone(p, 0.38)}" stroke-width="7" stroke-linecap="round" fill="none"/>`;
      }
      // 大叶绿植与吊篮。
      s += `<path d="M1080 700h90l-12 70h-66z" fill="${mix(p.wall[0], "#ffffff", 0.35)}"/>`;
      for (const [ang, len] of [[-70, 170], [-40, 150], [-100, 160], [-15, 120], [-130, 130]]) {
        s += `<ellipse cx="0" cy="${-len / 2}" rx="${len * 0.24}" ry="${len / 2}" fill="${ang % 20 ? leaf : leafDeep}" transform="translate(1125 700) rotate(${ang + 90})"/>`;
      }
      for (const hx of [140, 1470]) {
        s += `<path d="M${hx} 0V110" stroke="${tone(p, 0.4)}" stroke-width="2"/><path d="M${hx - 40} 110h80l-12 34h-56z" fill="${mix(p.wall[0], "#ffffff", 0.3)}"/>`;
        for (let i = 0; i < 5; i++) {
          const x = hx - 32 + i * 16, len = 80 + r() * 150;
          s += `<path d="M${x} 140Q${x - 12} ${140 + len / 2} ${x + 4} ${140 + len}" stroke="${leafDeep}" stroke-width="3" fill="none"/>`;
          for (let k = 1; k < 5; k++) s += `<ellipse cx="${f(x - 4 + (k % 2) * 8)}" cy="${f(140 + (len * k) / 5)}" rx="7" ry="4" fill="${leaf}"/>`;
        }
      }
      return { room: s + "</g>", horizon: 440 };
    },
    // 公园：积雪的落叶树、通向远处的小路与路灯、长椅、结冰的小池塘和远处的亭子。
    park(p, r, id) {
      const path = mix(p.ground[1], p.near, 0.16);
      let s = `<path d="M-20 900V604Q400 588 800 596T1620 600V900Z" fill="${p.snow}"/>`;
      s += `<path d="${quad([[520, 900], [1080, 900], [826, 600], [774, 600]])}" fill="${path}" opacity=".7"/>`;
      s += `<path d="${quad([[760, 900], [840, 900], [802, 600], [798, 600]])}" fill="${p.snow}" opacity=".45"/>`;
      // 远处的亭子。
      const roof = mix(p.near, "#6d4b4b", 0.3);
      s += `<g opacity=".85"><rect x="1236" y="548" width="6" height="52" fill="${roof}"/><rect x="1318" y="548" width="6" height="52" fill="${roof}"/><path d="M1206 552Q1280 520 1354 552L1340 534Q1280 506 1220 534Z" fill="${roof}"/><path d="M1214 538Q1280 508 1346 538Q1280 520 1214 538Z" fill="${p.snow}"/></g>`;
      // 结冰的小池塘。
      s += `<ellipse cx="1300" cy="684" rx="170" ry="34" fill="${mix(p.sky[1], "#bfe2f0", 0.5)}" opacity=".9"/><path d="M1180 680h90M1300 692h110" stroke="#ffffff" stroke-width="3" stroke-linecap="round" opacity=".6"/>`;
      s += `<g filter="url(#${id}-soft)">` + bareTree(p, r, 150, 760, 520) + bareTree(p, r, 450, 640, 300) + bareTree(p, r, 1470, 780, 560) + bareTree(p, r, 1130, 640, 280);
      // 长椅。
      const bench = wood(p, 0.15);
      s += `<rect x="250" y="704" width="210" height="14" rx="4" fill="${bench}"/><rect x="250" y="668" width="210" height="12" rx="4" fill="${bench}"/><path d="M266 718v40M444 718v40" stroke="${tone(p, 0.5)}" stroke-width="7"/>`;
      s += `<rect x="248" y="696" width="214" height="10" rx="5" fill="${p.snow}"/><rect x="248" y="660" width="214" height="9" rx="4" fill="${p.snow}"/>`;
      s += lampPost(p, id, 580, 880, 330) + lampPost(p, id, 1020, 880, 330) + lampPost(p, id, 724, 650, 120) + lampPost(p, id, 876, 650, 120) + "</g>";
      return { room: s, horizon: 560, outdoor: true };
    },
    // 商业街：两侧店面向远处汇聚，雨棚积雪、橱窗暖光，街上挂着串灯，尽头看得到远山与世界树。
    street(p, r, id) {
      const vp = { l: 690, r: 910, t: 360, b: 560 };
      const edge = (s, side, y0, y1) => {
        const x = side === "l" ? vp.l * s : 1600 + (vp.r - 1600) * s;
        return [x, y0 + (y1 - y0) * s];
      };
      const facade = (side) => side === "l" ? [[0, 110], [vp.l, vp.t], [vp.l, vp.b], [0, 900]] : [[1600, 130], [vp.r, vp.t], [vp.r, vp.b], [1600, 900]];
      let s = `<path d="${quad([[0, 900], [1600, 900], [vp.r, vp.b], [vp.l, vp.b]])}" fill="${mix(p.ground[1], p.near, 0.12)}"/>`;
      s += `<path d="${quad([[220, 900], [1380, 900], [vp.r - 26, vp.b], [vp.l + 26, vp.b]])}" fill="${p.snow}" opacity=".55"/>`;
      // 尽头的一排小楼。
      for (let i = 0; i < 4; i++) {
        const x = vp.l + i * 55, h = 70 + r() * 70;
        s += `<rect x="${f(x)}" y="${f(vp.b - h)}" width="56" height="${f(h)}" fill="${mix(p.mid[1], p.far[1], 0.4)}"/><rect x="${f(x)}" y="${f(vp.b - h - 6)}" width="56" height="8" fill="${p.snow}"/>`;
        for (let y = vp.b - h + 14; y < vp.b - 16; y += 22) s += `<rect x="${f(x + 10)}" y="${f(y)}" width="12" height="10" fill="${p.lamp}" opacity="${(0.25 + p.lampOpacity * 0.6).toFixed(2)}"/><rect x="${f(x + 32)}" y="${f(y)}" width="12" height="10" fill="${p.lamp}" opacity="${(0.15 + p.lampOpacity * 0.5).toFixed(2)}"/>`;
      }
      const awnings = [["#d9705f", "#f6e6d8"], ["#5f8fbf", "#eef3f8"], ["#6fa38c", "#eef6f0"], ["#c99a4f", "#f8efe0"]];
      for (const side of ["l", "r"]) {
        const [a, b, c, d] = facade(side);
        s += `<path d="${quad([a, b, c, d])}" fill="${mix(p.wall[1], p.near, side === "l" ? 0.28 : 0.38)}"/>`;
        s += `<path d="${quad([a, b, [b[0], b[1] + 12], [a[0], a[1] + 28]])}" fill="${p.snow}"/>`;
        const spans = [[0, 0.3], [0.36, 0.56], [0.6, 0.73], [0.76, 0.84]];
        spans.forEach(([s0, s1], k) => {
          const at = (t, y0, y1) => edge(t, side, y0, y1);
          const winTop = (t) => at(t, 560, vp.b - (vp.b - vp.t) * 0.38)[1], winBot = (t) => at(t, 860, vp.b - 6)[1];
          const x0 = at(s0, 0, 0)[0], x1 = at(s1, 0, 0)[0];
          s += `<path d="${quad([[x0, winTop(s0)], [x1, winTop(s1)], [x1, winBot(s1)], [x0, winBot(s0)]])}" fill="${p.lamp}" opacity="${(0.4 + p.lampOpacity * 0.5).toFixed(2)}"/>`;
          const part = (u0, u1, yTop, yBot) => {
            const ta = s0 + (s1 - s0) * u0, tb = s0 + (s1 - s0) * u1, xa = at(ta, 0, 0)[0], xb = at(tb, 0, 0)[0];
            return quad([[xa, yTop(ta)], [xb, yTop(tb)], [xb, yBot(tb)], [xa, yBot(ta)]]);
          };
          const mid = (t) => winTop(t) + (winBot(t) - winTop(t)) * 0.22;
          s += `<path d="${part(0.62, 0.82, mid, winBot)}" fill="${mix(p.wall[1], "#1f2d3a", 0.38)}" opacity=".72"/>`;
          for (const u of [0.2, 0.4]) s += `<path d="${part(u, u + 0.012, winTop, winBot)}" fill="${tone(p, 0.42)}" opacity=".7"/>`;
          // 楼上两层窗户，有的亮着灯。
          for (const [y0, y1, y0f, y1f] of [[196, 268, 374, 392], [294, 362, 398, 414]]) {
            for (const [u0, u1] of [[0.12, 0.42], [0.58, 0.88]]) {
              const lit = r() < 0.55, top = (t) => at(t, y0, y0f)[1], bot = (t) => at(t, y1, y1f)[1];
              s += `<path d="${part(u0, u1, top, bot)}" fill="${lit ? p.lamp : mix(p.wall[1], "#1f2d3a", 0.3)}" opacity="${lit ? (0.3 + p.lampOpacity * 0.6).toFixed(2) : "0.5"}"/>`;
              s += `<path d="${part(u0 - 0.02, u1 + 0.02, (t) => bot(t) - 2, (t) => bot(t) + 6 * (1 - t))}" fill="${p.snow}"/>`;
            }
          }
          const [ac, bc] = awnings[(k + (side === "r" ? 2 : 0)) % awnings.length];
          const awTop = (t) => winTop(t) - 70 * (1 - t * 0.85), awBot = (t) => winTop(t) - 6;
          s += `<path d="${quad([[x0, awTop(s0)], [x1, awTop(s1)], [x1 + (side === "l" ? 10 : -10) * (1 - s1), awBot(s1)], [x0 + (side === "l" ? 40 : -40) * (1 - s0), awBot(s0)]])}" fill="${mix(ac, p.wall[1], 0.2)}"/>`;
          for (let m = 1; m < 6; m += 2) {
            const t0 = s0 + ((s1 - s0) * m) / 6, t1 = s0 + ((s1 - s0) * (m + 1)) / 6;
            const xa = at(t0, 0, 0)[0], xb = at(t1, 0, 0)[0];
            s += `<path d="${quad([[xa, awTop(t0)], [xb, awTop(t1)], [xb, awBot(t1)], [xa, awBot(t0)]])}" fill="${bc}" opacity=".7"/>`;
          }
          s += `<path d="${quad([[x0, awTop(s0) - 6], [x1, awTop(s1) - 4], [x1, awTop(s1) + 2], [x0, awTop(s0) + 4]])}" fill="${p.snow}"/>`;
          const signTop = (t) => awTop(t) - 90 * (1 - t * 0.85), signBot = (t) => awTop(t) - 22 * (1 - t * 0.85);
          s += `<path d="${quad([[x0 + (x1 - x0) * 0.2, signTop(s0 + (s1 - s0) * 0.2)], [x0 + (x1 - x0) * 0.8, signTop(s0 + (s1 - s0) * 0.8)], [x0 + (x1 - x0) * 0.8, signBot(s0 + (s1 - s0) * 0.8)], [x0 + (x1 - x0) * 0.2, signBot(s0 + (s1 - s0) * 0.2)]])}" fill="${mix(ac, "#1f2d3a", 0.35)}" opacity=".9"/>`;
        });
      }
      // 串灯：几道垂弧，越远越细。
      for (const t of [0.12, 0.38, 0.6, 0.76]) {
        const [lx, ly] = edge(t, "l", 300, vp.t + 40), [rx, ry] = edge(t, "r", 320, vp.t + 40), sag = 80 * (1 - t);
        s += `<path d="M${f(lx)} ${f(ly)}Q800 ${f((ly + ry) / 2 + sag * 2)} ${f(rx)} ${f(ry)}" stroke="${tone(p, 0.5)}" stroke-width="${f(2.5 * (1 - t) + 0.6)}" fill="none"/>`;
        for (let i = 1; i < 14; i++) {
          const u = i / 14, x = (1 - u) * (1 - u) * lx + 2 * u * (1 - u) * 800 + u * u * rx;
          const y = (1 - u) * (1 - u) * ly + 2 * u * (1 - u) * ((ly + ry) / 2 + sag * 2) + u * u * ry;
          s += `<circle cx="${f(x)}" cy="${f(y + 4)}" r="${f(5 * (1 - t) + 1.4)}" fill="${[p.lamp, "#ffd7a8", "#ffe9c4"][i % 3]}" opacity="${(0.55 + p.lampOpacity * 0.45).toFixed(2)}"/>`;
        }
      }
      s += lampPost(p, id, 250, 880, 400) + lampPost(p, id, 1350, 880, 400);
      return { room: s, horizon: 470, outdoor: true };
    },
    // 购物中心：玻璃穹顶下的中庭，上下两层店铺、玻璃栏杆、扶梯，正中垂下一枚雪花挂饰。
    mall(p, r, id) {
      const sky = quad([[300, 0], [1300, 0], [1160, 150], [440, 150]]), fy = 640;
      let s = wall(id, [sky]) + glass(id, [sky]) + `<path d="${sky}" fill="none" stroke="${p.frame}" stroke-width="12"/>`;
      for (let i = 1; i < 6; i++) s += `<path d="M${300 + i * 166.7} 0L${440 + i * 120} 150" stroke="${p.frame}" stroke-width="6"/>`;
      s += `<path d="M370 75H1230" stroke="${p.frame}" stroke-width="5"/>`;
      s += floor(id, fy) + shaft(id, 460, 150, 680, fy, 0);
      const store = (x, y, w, h, c) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${p.lamp}" opacity="${(0.32 + p.lampOpacity * 0.45).toFixed(2)}"/>`
        + `<rect x="${x}" y="${y - 26}" width="${w}" height="22" fill="${mix(c, p.wall[1], 0.3)}"/><rect x="${x + 10}" y="${y + 10}" width="${w - 20}" height="${h * 0.46}" fill="#ffffff" opacity=".18"/>`;
      const signs = ["#d9705f", "#5f8fbf", "#6fa38c", "#c99a4f", "#9a7fbf", "#d98fa8"];
      // 上层：店面、楼板与玻璃栏杆。
      for (let i = 0; i < 6; i++) s += store(40 + i * 260, 196, 210, 92, signs[i]);
      s += `<rect y="298" width="1600" height="30" fill="${tone(p, 0.22)}"/><rect y="244" width="1600" height="54" fill="${mix(p.wall[0], "#dff1fb", 0.5)}" opacity=".35"/><rect y="240" width="1600" height="6" fill="${tone(p, 0.32)}"/>`;
      for (let x = 20; x < 1600; x += 120) s += `<rect x="${x}" y="244" width="4" height="54" fill="${tone(p, 0.3)}" opacity=".6"/>`;
      // 下层：店面。
      for (let i = 0; i < 5; i++) s += store(30 + i * 330, 410, 270, 200, signs[(i + 3) % signs.length]);
      s += `<g filter="url(#${id}-soft)">`;
      // 扶梯（左侧斜向上层）。
      s += `<path d="${quad([[150, 660], [270, 660], [610, 328], [530, 328]])}" fill="${mix(p.wall[1], "#9fb4c8", 0.4)}"/>`;
      for (let i = 1; i < 14; i++) { const t = i / 14; s += `<path d="M${f(150 + t * 380)} ${f(660 - t * 332)}h${f(120 - t * 40)}" stroke="${tone(p, 0.35)}" stroke-width="2" opacity=".6"/>`; }
      s += `<path d="M140 640L520 300M290 640L620 316" stroke="${tone(p, 0.42)}" stroke-width="10" stroke-linecap="round"/>`;
      // 盆栽小树。
      for (const x of [1080, 1480]) {
        s += `<rect x="${x - 40}" y="600" width="80" height="60" rx="8" fill="${mix(p.wall[0], "#ffffff", 0.3)}"/>`;
        s += `<circle cx="${x}" cy="540" r="62" fill="${mix(tone(p, 0.3), "#6fa38c", 0.45)}"/><circle cx="${x - 30}" cy="520" r="30" fill="${p.snow}" opacity=".6"/>`;
      }
      s += "</g>";
      // 正中的雪花挂饰。
      const ice = mix(p.frame, "#9fd8f0", 0.45);
      s += `<path d="M800 0V112M640 0V150M960 0V150" stroke="${tone(p, 0.4)}" stroke-width="2"/><g transform="translate(800 196)" stroke="${ice}" stroke-width="9" stroke-linecap="round" fill="none">`;
      for (let k = 0; k < 6; k++) s += `<g transform="rotate(${k * 60})"><path d="M0 0V-84M0 -48l-22 -18M0 -48l22 -18M0 -72l-12 -10M0 -72l12 -10"/></g>`;
      s += `</g><circle cx="800" cy="196" r="14" fill="${ice}"/>` + lamp(p, id, 800, 196, 190, 0.9);
      for (const x of [640, 960]) s += `<path d="M${x} 150l-14 22 14 22 14 -22z" fill="${ice}" opacity=".85"/>`;
      for (const x of [300, 800, 1300]) s += lamp(p, id, x, 470, 180, 0.9);
      return { room: s, horizon: 250 };
    },
    // 分析员的房间：百叶窗、贴着便签和地图的软木板、双屏工作桌与台灯、办公椅。
    analyst(p, r, id) {
      const hole = rr(500, 110, 500, 420, 14), fy = 720;
      let s = wall(id, [hole]) + glass(id, [hole]) + `<path d="${hole}" fill="none" stroke="${p.frame}" stroke-width="16"/>`;
      for (let y = 124; y < 300; y += 18) s += `<rect x="506" y="${y}" width="488" height="10" rx="2" fill="${mix(p.frame, "#ffffff", 0.35)}" opacity=".85"/>`;
      s += `<rect x="496" y="300" width="508" height="8" rx="3" fill="${tone(p, 0.3)}"/>`;
      s += floor(id, fy) + shaft(id, 520, 530, 460, fy, 120) + `<ellipse cx="760" cy="830" rx="430" ry="50" fill="${mix(p.floor[0], "#9fb0c8", 0.3)}" opacity=".5"/>`;
      // 软木板：便签、照片和用红线连起来的图钉。
      s += `<rect x="70" y="150" width="340" height="270" rx="8" fill="${mix("#c9a27a", p.wall[1], 0.35)}" stroke="${wood(p, 0.2)}" stroke-width="10"/>`;
      const pins = [];
      for (let i = 0; i < 7; i++) {
        const x = 100 + r() * 250, y = 175 + r() * 200, w = 46 + r() * 30, a = (r() - 0.5) * 14;
        s += `<rect x="${f(x)}" y="${f(y)}" width="${f(w)}" height="${f(w * 0.8)}" fill="${["#fff6c8", "#ffffff", "#d8ecf6", "#ffd9d2"][i % 4]}" transform="rotate(${f(a)} ${f(x)} ${f(y)})" opacity=".92"/>`;
        pins.push([x + w / 2, y + 6]);
      }
      s += `<path d="M${pins.map(([x, y]) => `${f(x)} ${f(y)}`).join("L")}" stroke="#c9483e" stroke-width="2" fill="none" opacity=".8"/>`;
      s += pins.map(([x, y]) => `<circle cx="${f(x)}" cy="${f(y)}" r="5" fill="#c9483e"/>`).join("");
      s += `<g filter="url(#${id}-soft)"><rect x="40" y="560" width="400" height="200" rx="8" fill="${tone(p, 0.26)}"/>`;
      for (const y of [584, 650, 716]) s += `<rect x="60" y="${y}" width="360" height="52" rx="4" fill="${tone(p, 0.18)}"/><rect x="220" y="${y + 22}" width="40" height="7" rx="3" fill="${tone(p, 0.42)}"/>`;
      // 工作桌、双屏与台灯。
      const scr = mix("#9fd0ee", p.lamp, 0.15);
      s += `<rect x="1040" y="600" width="540" height="22" rx="6" fill="${wood(p, 0.1)}"/><rect x="1060" y="622" width="16" height="230" fill="${tone(p, 0.42)}"/><rect x="1546" y="622" width="16" height="230" fill="${tone(p, 0.42)}"/>`;
      for (const x of [1100, 1290]) {
        s += `<rect x="${x}" y="430" width="176" height="122" rx="8" fill="${tone(p, 0.6)}"/><rect x="${x + 8}" y="438" width="160" height="104" rx="4" fill="${scr}" opacity="${(0.65 + p.lampOpacity * 0.3).toFixed(2)}"/>`;
        s += `<rect x="${x + 82}" y="552" width="12" height="40" fill="${tone(p, 0.5)}"/><rect x="${x + 56}" y="590" width="64" height="10" rx="4" fill="${tone(p, 0.5)}"/>`;
        for (let k = 0; k < 4; k++) s += `<rect x="${x + 20}" y="${452 + k * 20}" width="${f(40 + r() * 90)}" height="6" rx="3" fill="#ffffff" opacity=".55"/>`;
      }
      s += `<path d="M1300 488l18 -26 22 14 18 -30 22 20 24 -22" stroke="#2f6f9a" stroke-width="4" fill="none" opacity=".7"/>`;
      s += `<path d="M1500 600V520L1460 470" stroke="${tone(p, 0.5)}" stroke-width="7" fill="none" stroke-linecap="round"/><path d="M1430 470h60l-12 -26h-36z" fill="${tone(p, 0.42)}"/>`;
      s += `<rect x="1146" y="570" width="34" height="30" rx="6" fill="${mix(p.wall[0], "#ffffff", 0.4)}"/>`;
      // 办公椅。
      const seat = mix(p.wall[1], "#3f4f68", 0.5);
      s += `<rect x="1200" y="560" width="150" height="150" rx="40" fill="${seat}"/><rect x="1180" y="700" width="190" height="44" rx="20" fill="${mix(seat, "#1f2d3a", 0.15)}"/><rect x="1268" y="744" width="14" height="70" fill="${tone(p, 0.5)}"/><path d="M1200 828h150" stroke="${tone(p, 0.5)}" stroke-width="10" stroke-linecap="round"/></g>`;
      s += lamp(p, id, 1460, 480, 200, 1.6) + lamp(p, id, 1270, 490, 230, 0.7) + lamp(p, id, 800, 60, 200, 0.6);
      return { room: s, horizon: 430 };
    },
  };
}

let installation = null;
let stylesReady = false;
let canOpen = false;

function loadStyles() {
  if (document.querySelector(`link[href="${STYLE_URL}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = STYLE_URL;
  link.onload = () => { stylesReady = true; };
  link.onerror = () => link.remove();
  document.head.append(link);
}
function registerOpen() {
  try { CSS.registerProperty({ name: "--snow-open", syntax: "<percentage>", inherits: false, initialValue: "0%" }); canOpen = true; }
  catch { canOpen = typeof CSS !== "undefined" && typeof CSS.registerProperty === "function" && CSS.supports?.("(--snow-open: 1%)") === true; }
}
// 雪团的位置（百分比）与大小（vmax），从四周向中间堆满。
const DRIFTS = [[6, 14, 62], [94, 12, 60], [2, 84, 66], [98, 88, 64], [28, 104, 58], [72, 106, 60], [34, -8, 54], [70, -6, 56], [50, 50, 70]];

function fxNodes(root) {
  let fx = root.querySelector(":scope > .snow-veil-fx");
  if (fx) return fx;
  fx = document.createElement("div");
  fx.className = "snow-veil-fx";
  fx.setAttribute("aria-hidden", "true");
  const fill = document.createElement("div");
  fill.className = "snow-veil-fill";
  fx.append(fill);
  for (const [x, y, size] of DRIFTS) {
    const drift = document.createElement("div");
    drift.className = "snow-veil-drift";
    drift.style.cssText = `left:${x}%;top:${y}%;--s:${size}vmax`;
    fx.append(drift);
  }
  for (const kind of ["far", "near"]) {
    const flakes = document.createElement("div");
    flakes.className = `snow-veil-flakes snow-veil-flakes-${kind}`;
    fx.append(flakes);
  }
  root.prepend(fx);
  return fx;
}

// 完整档位的换场：返回给 app.js 的时长与容器帧；子层动画都挂在 veil 子树上，app.js 的 clearSnowVeil 会一并取消。
function veil(root, key) {
  if (!stylesReady || typeof root?.animate !== "function") return null;
  const [code, emblem] = SCENES[key] || SCENES.generic;
  const fx = fxNodes(root);
  const card = root.querySelector(".snow-scene-veil-card");
  const kicker = card?.querySelector(".snow-scene-veil-kicker");
  if (kicker) kicker.textContent = code;
  let mark = card?.querySelector(".snow-scene-veil-emblem");
  if (card && !mark) {
    mark = document.createElement("span");
    mark.className = "snow-scene-veil-emblem";
    card.prepend(mark);
  }
  const icon = emblem === "snowflake" ? createSnowIcon("snowflake") : createSceneIcon(emblem);
  for (const child of icon.children) child.setAttribute("pathLength", "1");
  mark?.replaceChildren(icon);
  root.dataset.scene = key;
  const fadeIn = 560, hold = 720, fadeOut = 780, total = fadeIn + hold + fadeOut;
  const ease = "cubic-bezier(.3,.05,.3,1)";
  const both = { fill: "both" };
  fx.querySelector(".snow-veil-fill").animate([{ opacity: 0 }, { opacity: 0, offset: 0.4 }, { opacity: 1 }], { duration: fadeIn, easing: "ease-in", ...both });
  [...fx.querySelectorAll(".snow-veil-drift")].forEach((drift, i) => drift.animate(
    [{ opacity: 0, transform: "translate(-50%, -50%) scale(.15)" }, { opacity: 1, transform: "translate(-50%, -50%) scale(1)" }],
    { duration: fadeIn * 0.8, delay: (i % 4) * 34, easing: ease, ...both }));
  fx.querySelectorAll(".snow-veil-flakes").forEach((flakes, i) => flakes.animate(
    [{ opacity: 0, backgroundPosition: "0px 0px, 0px 0px, 0px 0px" }, { opacity: 1, offset: 0.12 }, { opacity: 0.7, offset: 0.7 },
      { opacity: 0, backgroundPosition: i ? "90px 520px, 60px 640px, 40px 380px" : "40px 260px, 24px 300px, 16px 200px" }],
    { duration: total, easing: "linear", ...both }));
  if (card) card.animate([{ opacity: 0, transform: "translateY(10px)" }, { opacity: 1, transform: "none" }],
    { duration: 380, delay: fadeIn * 0.75, easing: "cubic-bezier(.34,1.32,.5,1)", ...both });
  [...icon.children].forEach((child, i) => child.animate([{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }],
    { duration: 520, delay: fadeIn * 0.75 + i * 70, easing: ease, ...both }));
  return {
    fadeIn, hold, fadeOut,
    inFrames: [{ opacity: 1 }, { opacity: 1 }],
    outFrames: canOpen ? [{ opacity: 1 }, { opacity: 1, offset: 0.72 }, { opacity: 0 }] : [{ opacity: 1 }, { opacity: 0 }],
    down() {
      if (canOpen) root.animate([{ "--snow-open": "0%" }, { "--snow-open": "140%" }], { duration: fadeOut, easing: "cubic-bezier(.2,.55,.3,1)", ...both });
      if (card) card.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-8px)" }], { duration: fadeOut * 0.45, easing: "ease-in", ...both });
    },
  };
}

// 时段一变，舞台背景、地点胶囊和界面色调在同一次视图过渡里交叉淡入（看起来是天色慢慢变了），
// 不再一下子跳过去。不支持视图过渡、页面在后台或动态关闭（含系统「减少动态」）时直接切换。
// 时段覆盖（<html data-snow-time>，宣传片录制和测试用）一改就切，不等 30 秒一次的检查。
function installSnowTimeShift() {
  const root = document.documentElement;
  setSnowTimeShift(run => {
    if (typeof document.startViewTransition !== "function" || document.hidden || root.dataset.motionLevel === "off") { run(); return; }
    root.dataset.snowTimeShift = "1";
    try {
      const shift = document.startViewTransition(run);
      shift.ready.catch(() => {});
      shift.finished.finally(() => { delete root.dataset.snowTimeShift; });
    } catch { delete root.dataset.snowTimeShift; run(); }
  });
  new MutationObserver(checkSnowTime).observe(root, { attributes: true, attributeFilter: ["data-snow-time"] });
}

/** 注册专属背景并返回换场接口；重复调用返回同一份。 */
export function installSnowScenes() {
  if (installation) return installation;
  registerSnowPlaces(places);
  registerOpen();
  loadStyles();
  installSnowTimeShift();
  installation = Object.freeze({ veil, keys: PLACE_KEYS, scenes: SCENES });
  return installation;
}
