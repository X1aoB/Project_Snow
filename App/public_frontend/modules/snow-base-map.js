/* Local-only base map: a floor plan drawn from the signed world package already in this browser.
   The caller owns character selection and visit confirmation. */
let installation;
const SVG = "http://www.w3.org/2000/svg";

// Plan frame is 1000 x 640. Codes match the scene location pill (B-01..B-08); annexes and the outside use EXT.
// [key, code, name, x, y, w, h, pin capacity, kind, extra location terms]
const ZONES = [
  ["observation", "B-01", "观景区", 292, 44, 236, 180, 8],
  ["archive", "B-02", "资料室", 44, 44, 236, 180, 8],
  ["quarters", "B-03", "宿舍", 44, 316, 173, 204, 6, "room", ["房间"]],
  ["lounge", "B-04", "休息区", 229, 316, 173, 204, 6],
  ["canteen", "B-05", "食堂", 414, 316, 173, 204, 6],
  ["training", "B-06", "训练区", 599, 316, 181, 204, 6],
  ["medical", "B-07", "医疗室", 540, 44, 240, 180, 8],
  ["corridor", "B-08", "走廊", 44, 240, 736, 60, 10, "row"],
  ["hot_spring", "EXT", "温泉", 824, 56, 152, 96, 2, "annex", ["温泉"]],
  ["arcade", "EXT", "游戏厅", 824, 164, 152, 96, 2, "annex", ["游戏厅"]],
  ["bar", "EXT", "酒吧", 824, 272, 152, 96, 2, "annex", ["酒吧"]],
  ["healing", "EXT", "疗愈中心", 824, 380, 152, 96, 2, "annex", ["疗愈"]],
  ["beach", "EXT", "海滩", 824, 488, 152, 128, 2, "annex", ["海滩", "沙滩"]],
  ["outside", "EXT", "基地外", 24, 576, 776, 56, 10, "row"],
].map(([key, code, name, x, y, w, h, capacity, kind = "room", terms = []]) => ({ key, code, name, x, y, w, h, capacity, kind, terms }));
const ZONE = new Map(ZONES.map(zone => [zone.key, zone]));

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value, limit = 160) {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function decodeWorld(token) {
  try {
    if (typeof token !== "string" || token.length > 1024 * 1024) return {};
    const first = token.split(".", 1)[0];
    if (!/^[A-Za-z0-9_-]+$/.test(first)) return {};
    const base64 = first.replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(base64 + "=".repeat((4 - base64.length % 4) % 4)), char => char.charCodeAt(0));
    return object(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
  } catch { return {}; }
}

function currentPackage(decoded) {
  const world = object(decoded.world);
  const date = text(decoded.schedule_date || world.schedule_date || decoded.date);
  const parts = new Intl.DateTimeFormat("en", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date());
  const part = type => parts.find(item => item.type === type)?.value;
  const today = `${part("year")}-${part("month")}-${part("day")}`;
  if (date && date !== today) return false;
  const expiry = decoded.expires_at ?? world.expires_at;
  if (expiry !== undefined && expiry !== "") {
    if (typeof expiry !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(expiry)) return false;
    const stamp = Date.parse(expiry);
    return Number.isFinite(stamp) && stamp > Date.now();
  }
  const exp = decoded.exp ?? world.exp;
  if (exp !== undefined) return typeof exp === "number" && Number.isFinite(exp) && exp * 1000 > Date.now();
  return date === today;
}

// Rooms come from the same mapping as the stage scenes; anything else is an annex or the outside.
function zoneFor(location, sceneKey) {
  if (!location) return "";
  let scene = "generic";
  try { scene = String(sceneKey(location) || "generic"); } catch { scene = "generic"; }
  if (scene !== "generic" && ZONE.has(scene)) return scene;
  return ZONES.find(zone => zone.terms.some(term => location.includes(term)))?.key || "outside";
}

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function shape(parent, tag, attributes) {
  const element = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  parent.append(element);
  return element;
}

// Snowbreak-style cut corners: a wide cut top-right, a small one bottom-left.
function chamfer(x, y, w, h, cut = 14, small = 7) {
  return `M${x} ${y}H${x + w - cut}L${x + w} ${y + cut}V${y + h}H${x + small}L${x} ${y + h - small}Z`;
}

const GLYPHS = {
  observation: "M336 196h52M432 196h52M470 128l26-18m-13 9 9 22m-9-22-13 22",
  archive: "M150 104h112M150 128h112M150 152h112M150 176h112M150 200h112M150 104v96M262 104v96",
  medical: "M736 66h12m-6-6v12M600 172h50v24h-50zM600 180h10M672 172h50v24h-50zM672 180h10",
  quarters: "M64 430h38v70H64zM64 442h38M118 430h38v70h-38zM118 442h38M172 430h32v70h-32zM172 442h32",
  lounge: "M252 430v56h62M252 430h40M346 470a14 14 0 1 0 0 .1M376 430h12v56h-12z",
  canteen: "M440 428h44v28h-44zM506 428h44v28h-44zM440 478h44v28h-44zM506 478h44v28h-44z",
  training: "M690 460m-36 0a36 36 0 1 0 72 0a36 36 0 1 0-72 0M664 404h52M670 398v12M710 398v12",
  corridor: "M64 270h696M300 262l8 8-8 8M520 262l8 8-8 8",
  hot_spring: "M868 128a34 14 0 1 0 68 0a34 14 0 1 0-68 0M888 106c-6-8 6-12 0-20M904 106c-6-8 6-12 0-20M920 106c-6-8 6-12 0-20",
  arcade: "M840 222h18v30h-18zM866 222h18v30h-18zM892 222h18v30h-18zM845 229h8M871 229h8M897 229h8",
  bar: "M846 340q54-26 108 0M864 352a5 5 0 1 0 0 .1M900 356a5 5 0 1 0 0 .1M936 352a5 5 0 1 0 0 .1",
  healing: "M862 468c-20-5-26-22-20-36 16 2 25 16 20 36zm0 0c0-11 5-19 11-24",
  beach: "M840 584q12-8 24 0t24 0 24 0 24 0 24 0M840 600q12-8 24 0t24 0 24 0 24 0 24 0",
};

function drawPlan() {
  const art = document.createElementNS(SVG, "svg");
  art.setAttribute("class", "snow-map-art");
  art.setAttribute("viewBox", "0 0 1000 640");
  art.setAttribute("preserveAspectRatio", "none");
  art.setAttribute("aria-hidden", "true");
  art.setAttribute("focusable", "false");
  const defs = shape(art, "defs", {});
  const pattern = shape(defs, "pattern", { id: "snow-map-grid", width: 40, height: 40, patternUnits: "userSpaceOnUse" });
  shape(pattern, "path", { class: "grid", d: "M40 0H0V40" });
  shape(art, "rect", { class: "field", width: 1000, height: 640 });
  shape(art, "rect", { class: "grid-fill", width: 1000, height: 640, fill: "url(#snow-map-grid)" });
  shape(art, "path", { class: "road", d: "M412 556V640" });
  shape(art, "path", { class: "road-line", d: "M412 566V640" });
  for (const zone of ZONES.filter(item => item.kind === "annex")) {
    shape(art, "path", { class: "link", d: `M800 ${zone.y + zone.h / 2}H${zone.x}` });
  }
  const c = 28, gateLeft = 382, gateRight = 442;
  shape(art, "path", { class: "floor", d: `M${24 + c} 24H${800 - c}L800 ${24 + c}V${560 - c}L${800 - c} 560H${24 + c}L24 ${560 - c}V${24 + c}Z` });
  const wall = inset => `M${gateRight} ${560 - inset}H${800 - c - inset * 0.4}L${800 - inset} ${560 - c - inset * 0.4}V${24 + c + inset * 0.4}`
    + `L${800 - c - inset * 0.4} ${24 + inset}H${24 + c + inset * 0.4}L${24 + inset} ${24 + c + inset * 0.4}V${560 - c - inset * 0.4}`
    + `L${24 + c + inset * 0.4} ${560 - inset}H${gateLeft}`;
  shape(art, "path", { class: "wall", d: wall(0) });
  shape(art, "path", { class: "wall-inner", d: wall(8) });
  for (const zone of ZONES) {
    const room = shape(art, "path", { class: zone.kind === "annex" ? "room annex" : zone.kind === "row" && zone.key === "outside" ? "zone-outside" : "room",
      d: zone.key === "outside" ? `M${zone.x} ${zone.y}h${zone.w}v${zone.h}h-${zone.w}z` : chamfer(zone.x, zone.y, zone.w, zone.h, zone.kind === "annex" ? 10 : 14, zone.kind === "annex" ? 5 : 7) });
    room.dataset.zone = zone.key;
    if (GLYPHS[zone.key]) shape(art, "path", { class: "glyph", d: GLYPHS[zone.key] });
    if (zone.kind === "room" && zone.y < 240) shape(art, "rect", { class: "door", x: zone.x + zone.w / 2 - 16, y: zone.y + zone.h - 2, width: 32, height: 4 });
    if (zone.kind === "room" && zone.y > 300) shape(art, "rect", { class: "door", x: zone.x + zone.w / 2 - 16, y: zone.y - 2, width: 32, height: 4 });
  }
  shape(art, "path", { class: "window", d: "M304 47Q410 14 516 47" });
  for (const [cx, cy] of [[24 + c, 24], [800 - c, 24], [800, 560 - c], [24, 560 - c], [gateLeft, 560], [gateRight, 560]]) {
    shape(art, "circle", { class: "beacon-halo", cx, cy, r: 9 });
    shape(art, "circle", { class: "beacon", cx, cy, r: 4 });
  }
  shape(art, "path", { class: "compass", d: "M962 14l7 18-7-4-7 4z" });
  shape(art, "circle", { class: "compass-ring", cx: 962, cy: 26, r: 15 });
  return art;
}

function loadStyles() {
  return new Promise((resolve, reject) => {
    const link = document.createElement("link");
    link.id = "snow-base-map-style";
    link.rel = "stylesheet";
    link.href = new URL("/modules/snow-base-map.css", import.meta.url).href;
    link.onload = () => resolve();
    link.onerror = () => { link.remove(); reject(new Error("base_map_styles_unavailable")); };
    document.head.append(link);
  });
}

function avatarInto(holder, character) {
  const source = text(character.avatar?.thumbnail_src, 400);
  if (!source || !source.startsWith("/")) return;
  const image = document.createElement("img");
  image.alt = "";
  image.decoding = "async";
  image.src = source;
  image.style.objectPosition = `${Number(character.avatar.portrait_focus_x) || 50}% ${Number(character.avatar.portrait_focus_y) || 50}%`;
  image.onerror = () => image.remove();
  holder.append(image);
}

export async function installBaseMap({ snapshot, visit, icon, sceneKey }) {
  if (document.documentElement.dataset.ui !== "snow-adv") return null;
  if (typeof snapshot !== "function" || typeof visit !== "function" || typeof icon !== "function" || typeof sceneKey !== "function") {
    throw new TypeError("base_map_callbacks_required");
  }
  if (installation) return installation;
  installation = (async () => {
    const footer = document.querySelector(".contact-footer");
    if (!footer) throw new Error("base_map_footer_unavailable");
    await loadStyles();
    if (document.documentElement.dataset.ui !== "snow-adv") return null;

    const entry = node("button", "footer-command", "基地地图");
    entry.id = "open-snow-base-map";
    entry.type = "button";
    entry.setAttribute("aria-haspopup", "dialog");
    entry.setAttribute("aria-controls", "snow-base-map");
    entry.prepend(icon("map-pin"));
    // Reuse the shared modal / icon-button / primary-button skins so every tone matches the other dialogs.
    const dialog = node("dialog", "modal snow-base-map");
    dialog.id = "snow-base-map";
    dialog.setAttribute("aria-labelledby", "snow-base-map-title");
    dialog.setAttribute("aria-describedby", "snow-base-map-summary");
    const header = node("header", "snow-base-map-header");
    const heading = node("div");
    const title = node("h2", "", "基地地图");
    title.id = "snow-base-map-title";
    const summary = node("p");
    summary.id = "snow-base-map-summary";
    heading.append(node("span", "panel-kicker", "本机记录"), title, summary);
    const closeButton = node("button", "icon-button snow-base-map-close");
    closeButton.id = "snow-base-map-close";
    closeButton.type = "button";
    closeButton.setAttribute("aria-label", "关闭基地地图");
    closeButton.append(icon("x"));
    header.append(heading, closeButton);

    const body = node("div", "snow-base-map-body");
    const figure = node("figure", "snow-map-figure");
    const scroller = node("div", "snow-map-scroller");
    const plan = node("div", "snow-map-plan");
    plan.id = "snow-base-map-plan";
    plan.append(drawPlan());
    const zoneLayer = node("div", "snow-map-zones");
    plan.append(zoneLayer);
    scroller.append(plan);
    const legend = node("figcaption", "snow-map-legend");
    figure.append(scroller, legend);
    const list = node("div", "snow-base-map-list");
    list.id = "snow-base-map-list";
    list.setAttribute("aria-label", "按区域排列的角色");
    body.append(figure, list);

    const actions = node("footer", "snow-base-map-actions");
    const status = node("p");
    status.id = "snow-base-map-status";
    status.tabIndex = 0;
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const visitButton = node("button", "primary-button snow-base-map-visit", "去见她");
    visitButton.id = "snow-base-map-visit";
    visitButton.type = "button";
    actions.append(status, visitButton);
    dialog.append(header, body, actions);
    let selected = "", activeZone = "", characters = [], busy = false, pending = false, handoff = false;

    function controls() {
      visitButton.disabled = busy || pending || !characters.some(character => character.id === selected);
      entry.disabled = pending;
      for (const button of dialog.querySelectorAll("[data-base-map-select], [data-map-pin]")) {
        const id = button.dataset.baseMapSelect || button.dataset.mapPin;
        const active = id === selected;
        button.disabled = busy || pending;
        if (button.dataset.baseMapSelect) button.setAttribute("aria-pressed", String(active));
        button.dataset.selected = String(active);
      }
    }

    function markZone(key) {
      activeZone = key;
      for (const item of dialog.querySelectorAll("[data-zone]")) item.dataset.active = String(item.dataset.zone === key);
    }

    function centreMap(key) {
      const zone = zoneLayer.querySelector(`.snow-map-zone[data-zone="${key}"]`);
      if (!zone || scroller.scrollWidth <= scroller.clientWidth) return;
      scroller.scrollLeft = zone.offsetLeft + zone.offsetWidth / 2 - scroller.clientWidth / 2;
    }

    function showZone(key) {
      markZone(key);
      const group = list.querySelector(`.snow-map-group[data-zone="${key}"]`);
      if (group) list.scrollTop += group.getBoundingClientRect().top - list.getBoundingClientRect().top;
      const zone = ZONE.get(key), people = characters.filter(character => character.zone === key);
      status.textContent = !zone ? status.textContent
        : people.length ? `${zone.code} ${zone.name}：${people.map(character => character.name).join("、")}。选一位再去见她。`
        : `${zone.code} ${zone.name}：此刻没有人。`;
    }

    function choose(character, card) {
      if (busy || pending) return;
      selected = character.id;
      if (character.zone) { markZone(character.zone); centreMap(character.zone); }
      status.textContent = `${character.name} · ${character.detail}。前往前仍需确认。`;
      controls();
      // A longer status grows the footer and shrinks the list; keep the chosen card in view.
      card.scrollIntoView({ block: "nearest" });
    }

    function card(character) {
      const listItem = node("div", "snow-base-map-item");
      listItem.setAttribute("role", "listitem");
      const button = node("button", "snow-base-map-card snow-base-map-select");
      button.type = "button";
      button.dataset.characterId = character.id;
      button.dataset.snowCharacter = character.id;
      button.dataset.presence = character.presence;
      button.dataset.baseMapSelect = character.id;
      // Under "B-04 休息区" a location of "基地休息区" adds nothing; other wording (个人房间, 商业街) stays visible.
      button.dataset.placeSame = String(Boolean(character.zone && character.location.includes(ZONE.get(character.zone).name)));
      button.setAttribute("aria-label", `选择${character.name}。${character.detail}`);
      const identity = node("span", "snow-base-map-identity");
      const initial = node("span", "snow-base-map-initial", Array.from(character.name)[0]);
      initial.setAttribute("aria-hidden", "true");
      avatarInto(initial, character);
      identity.append(initial, node("strong", "snow-base-map-name", character.name));
      button.append(identity, node("span", "snow-base-map-location", character.placeText),
        node("span", "snow-base-map-activity", character.activityText));
      button.onclick = () => choose(character, button);
      listItem.append(button);
      return listItem;
    }

    function render(analystZone, current) {
      zoneLayer.replaceChildren();
      list.replaceChildren();
      const art = plan.querySelector(".snow-map-art");
      for (const zone of ZONES) {
        const people = characters.filter(character => character.zone === zone.key);
        const room = art.querySelector(`[data-zone="${zone.key}"]`);
        room.dataset.occupied = String(people.length > 0 || analystZone === zone.key);
        const area = node("div", "snow-map-zone");
        area.dataset.zone = zone.key;
        area.dataset.kind = zone.kind;
        area.dataset.occupied = room.dataset.occupied;
        Object.assign(area.style, { left: `${zone.x / 10}%`, top: `${zone.y / 6.4}%`, width: `${zone.w / 10}%`, height: `${zone.h / 6.4}%` });
        const button = node("button", "snow-map-zone-button");
        button.type = "button";
        button.dataset.mapZone = zone.key;
        button.tabIndex = people.length ? 0 : -1;
        const you = analystZone === zone.key ? "，你在这里" : "";
        button.setAttribute("aria-label", `${zone.code} ${zone.name}，${people.length ? `${people.length} 人` : "没有人"}${you}`);
        const label = node("span", "snow-map-label");
        label.append(node("span", "snow-map-code", zone.code), node("span", "snow-map-name", zone.name));
        button.append(label);
        if (people.length) button.append(node("span", "snow-map-count", String(people.length)));
        button.onclick = () => showZone(zone.key);
        button.onpointerenter = () => { room.dataset.hover = "true"; };
        button.onpointerleave = () => { delete room.dataset.hover; };
        const pins = node("div", "snow-map-pins");
        pins.setAttribute("aria-hidden", "true");
        let slots = zone.capacity;
        if (analystZone === zone.key) {
          const marker = node("span", "snow-map-you", "你");
          marker.title = "你在这里";
          pins.append(marker);
          slots -= 1;
        }
        // On overflow the "+N" chip takes the tail; it is wider than a pin and its text width depends on the
        // system font, so rooms reserve two pin slots for it (annexes, with two slots, keep one pin).
        // The selected character's pin is never folded into it.
        const reserve = zone.capacity >= 4 ? 2 : 1;
        const ordered = [...people].sort((a, b) => Number(b.id === selected) - Number(a.id === selected));
        const shown = people.length > slots ? ordered.slice(0, Math.max(slots - reserve, 0)) : people;
        for (const character of shown) {
          const pin = node("button", "snow-map-pin", Array.from(character.name)[0]);
          pin.type = "button";
          pin.tabIndex = -1;
          pin.title = `${character.name} · ${character.activity || "活动尚未建立"}`;
          pin.dataset.mapPin = character.id;
          pin.dataset.snowCharacter = character.id;
          pin.dataset.presence = character.presence;
          avatarInto(pin, character);
          pin.onclick = () => {
            const row = list.querySelector(`[data-base-map-select="${character.id}"]`);
            if (row) choose(character, row);
          };
          pins.append(pin);
        }
        if (shown.length < people.length) pins.append(node("span", "snow-map-more", `+${people.length - shown.length}`));
        area.append(button, pins);
        zoneLayer.append(area);
        if (!people.length) continue;
        const group = node("section", "snow-map-group");
        group.dataset.zone = zone.key;
        const heading = node("h3", "snow-map-group-title");
        heading.append(node("span", "snow-map-code", zone.code), node("span", "", zone.name), node("span", "snow-map-count", String(people.length)));
        const rows = node("div", "snow-map-group-list");
        rows.setAttribute("role", "list");
        for (const character of people) rows.append(card(character));
        group.append(heading, rows);
        list.append(group);
      }
      const unknown = characters.filter(character => !character.zone);
      if (unknown.length) {
        const group = node("section", "snow-map-group");
        group.dataset.zone = "unknown";
        const heading = node("h3", "snow-map-group-title");
        heading.append(node("span", "snow-map-code", "—"), node("span", "", "位置尚未建立"), node("span", "snow-map-count", String(unknown.length)));
        const rows = node("div", "snow-map-group-list");
        rows.setAttribute("role", "list");
        for (const character of unknown) rows.append(card(character));
        group.append(heading, rows);
        list.append(group);
      }
      const known = characters.length - unknown.length;
      legend.replaceChildren();
      const key = node("span", "snow-map-key");
      key.append(node("span", "snow-map-key-dot", ""), node("span", "", current ? "此刻位置" : "上次位置"));
      legend.append(key);
      if (analystZone) {
        const you = node("span", "snow-map-key");
        you.append(node("span", "snow-map-key-you", "你"), node("span", "", "你所在的地方"));
        legend.append(you);
      }
      if (known && unknown.length) legend.append(node("span", "snow-map-key", `${unknown.length} 位还没有位置记录，见名单末尾`));
      legend.append(node("span", "snow-map-key-note", !characters.length ? "角色清单尚未建立"
        : !known ? "还没有位置记录，和角色通讯后会出现在地图上"
        : current ? "按本机记录绘制，不会刷新场景" : "今天还没有新的位置，显示的是上次记录"));
      plan.dataset.presence = !known ? "missing" : current ? "known" : "stale";
    }

    function open() {
      if (dialog.open || pending || document.documentElement.dataset.ui !== "snow-adv") return;
      let state;
      try { state = object(snapshot()); } catch { state = {}; }
      const decoded = decodeWorld(state.worldPackage);
      const world = object(decoded.world);
      const presence = object(decoded.presence || world.presence);
      const current = currentPackage(decoded);
      const analystLocation = current ? text(decoded.analyst_location ?? world.analyst_location, 120) : "";
      const analystZone = zoneFor(analystLocation, sceneKey);
      const seen = new Set();
      characters = (Array.isArray(state.characters) ? state.characters : []).flatMap(character => {
        const id = text(character?.character_id, 128);
        if (!id || seen.has(id)) return [];
        seen.add(id);
        const item = object(Object.hasOwn(presence, id) ? presence[id] : null);
        const location = text(item.location, 120), activity = text(item.activity, 240);
        const placeText = location ? `${current ? "位置" : "上次位置"}：${location}` : "位置尚未建立";
        const activityText = activity ? `${current ? "活动" : "上次活动"}：${activity}` : "活动尚未建立";
        return [{ id, name: text(character?.display_name, 80) || "未命名角色", avatar: object(character?.avatar),
          location, activity, placeText, activityText, detail: `${placeText}；${activityText}`,
          zone: zoneFor(location, sceneKey), presence: location ? (current ? "known" : "stale") : "missing" }];
      });
      selected = characters.some(character => character.id === state.selected) ? state.selected : "";
      busy = Boolean(state.busy);
      render(analystZone, current);
      const selectedCharacter = characters.find(character => character.id === selected);
      markZone(selectedCharacter?.zone || analystZone || "");
      summary.textContent = `${characters.length} 位角色 · 本机保存的位置，不会刷新场景。点房间看谁在那里。`;
      status.textContent = busy ? "正在处理其他操作，请稍后重新打开地图。"
        : !characters.length ? "角色清单尚未建立。"
        : selectedCharacter ? `${selectedCharacter.name} · ${selectedCharacter.detail}。前往前仍需确认。` : "先选择一位角色，再去见她。";
      controls();
      dialog.showModal();
      closeButton.focus();
      centreMap(activeZone || "corridor");
      list.querySelector(`[data-base-map-select="${selected}"]`)?.scrollIntoView({ block: "nearest" });
    }

    function close() { if (dialog.open) dialog.close(); }
    closeButton.onclick = close;
    dialog.addEventListener("close", () => {
      if (!handoff && entry.isConnected && !entry.disabled) entry.focus();
      handoff = false;
    });
    visitButton.onclick = async () => {
      if (pending || !selected || visitButton.disabled) return;
      let latest;
      try { latest = object(snapshot()); } catch { latest = { busy: true }; }
      if (latest.busy) {
        busy = true;
        status.textContent = "正在处理其他操作，请稍后重新打开地图。";
        controls();
        return;
      }
      const target = selected;
      pending = true;
      controls();
      handoff = true;
      close();
      try { await visit(target); }
      catch {
        // Do not retry an operation whose outcome belongs to the caller.
        pending = false;
        open();
        status.textContent = "未能完成前往操作，请先确认当前场景。";
      } finally { pending = false; controls(); }
    };
    entry.onclick = open;
    document.body.append(dialog);
    footer.append(entry);
    return { open, close };
  })();
  return installation;
}
