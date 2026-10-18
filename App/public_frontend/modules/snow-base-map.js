/* Local-only base map. The caller owns character selection and visit confirmation. */
let installation;

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

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
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

export async function installBaseMap({ snapshot, visit, icon }) {
  if (document.documentElement.dataset.ui !== "snow-adv") return null;
  if (typeof snapshot !== "function" || typeof visit !== "function" || typeof icon !== "function") {
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
    const dialog = node("dialog", "snow-base-map");
    dialog.id = "snow-base-map";
    dialog.setAttribute("aria-labelledby", "snow-base-map-title");
    dialog.setAttribute("aria-describedby", "snow-base-map-summary");
    const header = node("header", "snow-base-map-header");
    const heading = node("div");
    const title = node("h2", "", "基地地图");
    title.id = "snow-base-map-title";
    const summary = node("p");
    summary.id = "snow-base-map-summary";
    heading.append(title, summary);
    const closeButton = node("button", "snow-base-map-close");
    closeButton.id = "snow-base-map-close";
    closeButton.type = "button";
    closeButton.setAttribute("aria-label", "关闭基地地图");
    closeButton.append(icon("x"));
    header.append(heading, closeButton);
    const list = node("div", "snow-base-map-list");
    list.id = "snow-base-map-list";
    list.setAttribute("role", "list");
    const actions = node("footer", "snow-base-map-actions");
    const status = node("p");
    status.id = "snow-base-map-status";
    status.tabIndex = 0;
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const visitButton = node("button", "snow-base-map-visit", "去见她");
    visitButton.id = "snow-base-map-visit";
    visitButton.type = "button";
    actions.append(status, visitButton);
    dialog.append(header, list, actions);
    let selected = "", characters = [], busy = false, pending = false, handoff = false;

    function controls() {
      visitButton.disabled = busy || pending || !characters.some(character => character.id === selected);
      entry.disabled = pending;
      for (const button of list.querySelectorAll("[data-base-map-select]")) {
        const active = button.dataset.baseMapSelect === selected;
        button.disabled = busy || pending;
        button.setAttribute("aria-pressed", String(active));
        button.dataset.selected = String(active);
      }
    }

    function open() {
      if (dialog.open || pending || document.documentElement.dataset.ui !== "snow-adv") return;
      let state;
      try { state = object(snapshot()); } catch { state = {}; }
      const decoded = decodeWorld(state.worldPackage);
      const presence = object(decoded.presence || object(decoded.world).presence);
      const current = currentPackage(decoded);
      const seen = new Set();
      characters = (Array.isArray(state.characters) ? state.characters : []).flatMap(character => {
        const id = text(character?.character_id, 128);
        if (!id || seen.has(id)) return [];
        seen.add(id);
        return [{ id, name: text(character?.display_name, 80) || "未命名角色" }];
      });
      selected = characters.some(character => character.id === state.selected) ? state.selected : "";
      busy = Boolean(state.busy);
      list.replaceChildren();
      for (const character of characters) {
        const item = object(Object.hasOwn(presence, character.id) ? presence[character.id] : null);
        const location = text(item.location, 120), activity = text(item.activity, 240);
        const placeText = location ? `${current ? "本地位置" : "上次位置"}：${location}` : "位置尚未建立";
        const activityText = activity ? `${current ? "活动" : "上次活动"}：${activity}` : "活动尚未建立";
        character.detail = `${placeText}；${activityText}`;
        const listItem = node("div", "snow-base-map-item");
        listItem.setAttribute("role", "listitem");
        const card = node("button", "snow-base-map-card snow-base-map-select");
        card.type = "button";
        card.dataset.characterId = character.id;
        card.dataset.presence = location ? (current ? "known" : "stale") : "missing";
        card.dataset.baseMapSelect = character.id;
        card.setAttribute("aria-label", `选择${character.name}。${character.detail}`);
        const identity = node("span", "snow-base-map-identity");
        const initial = node("span", "snow-base-map-initial", Array.from(character.name)[0]);
        initial.setAttribute("aria-hidden", "true");
        identity.append(initial, node("strong", "snow-base-map-name", character.name));
        const place = node("span", "snow-base-map-location", placeText);
        const activityLine = node("span", "snow-base-map-activity", activityText);
        card.onclick = () => {
          if (busy || pending) return;
          selected = character.id;
          status.textContent = `${character.name} · ${character.detail}。前往前仍需确认。`;
          controls();
        };
        card.append(identity, place, activityLine);
        listItem.append(card);
        list.append(listItem);
      }
      summary.textContent = `${characters.length} 位角色 · 本机保存的位置，不会刷新场景。选中可读完整活动。`;
      const selectedCharacter = characters.find(character => character.id === selected);
      status.textContent = busy ? "正在处理其他操作，请稍后重新打开地图。"
        : !characters.length ? "角色清单尚未建立。"
        : selectedCharacter ? `${selectedCharacter.name} · ${selectedCharacter.detail}。前往前仍需确认。` : "先选择一位角色，再去见她。";
      controls();
      dialog.showModal();
      closeButton.focus();
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
