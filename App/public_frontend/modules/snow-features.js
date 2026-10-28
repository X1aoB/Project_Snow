// 雪境新功能界面（按需加载，不占首屏）：记忆档案、台词收藏与回看搜索、图片卡入口、本轮用量；
// 第二批：长台词分页与自动翻页（snow-pager.js）、约定提醒与设置「对话」（snow-conversation-ui.js）。
// 数据和写入都经 host.logic（modules/snow-feature-logic.js）；这里只负责界面。说明见 docs/snow-adv-ui-notes.md。
import { createFeatureIcon, SNOW_FEATURE_ICON_NAMES } from "/modules/snow-feature-icons.js";
import { installSnowPager } from "/modules/snow-pager.js";
import { installConversationUi } from "/modules/snow-conversation-ui.js";
import { buildReadingText } from "/modules/snow-reading-logic.js";

const STYLE_URL = "/modules/snow-features.css";
const CARD_URL = "/modules/snow-feature-card.js";
const KIND_LABELS = Object.freeze({ shared_scene_event: "一起经历", user_statement: "你说过的", assistant_commitment: "她答应的", communicator_topic: "通讯器里聊过" });
// 来源徽章：面对面只有 shared_scene_event；其余（自述、承诺、通讯器话题）都来自通讯器文字渠道。
const MEMORY_SOURCE = Object.freeze({ shared_scene_event: "面对面", user_statement: "通讯器", assistant_commitment: "通讯器", communicator_topic: "通讯器" });
const CONVO_URL = "/modules/snow-memory-convo.js";
const PART_LABELS = Object.freeze({ reply: "回复", arrival: "到场反应", summary: "连续性整理", proactive: "主动消息" });
const SENTENCE_END = /[。！？!?…]/u;
const FAVORITE_LIMIT = 240;
let installation = null;

const $ = id => document.getElementById(id);
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "hidden") node.hidden = Boolean(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) if (child != null && child !== false) node.append(child);
  return node;
}
function loadStyles() {
  if (document.querySelector(`link[href="${STYLE_URL}"]`)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const link = el("link", { rel: "stylesheet", href: STYLE_URL });
    link.onload = () => resolve();
    link.onerror = () => { link.remove(); reject(new Error("snow_features_styles_unavailable")); };
    document.head.append(link);
  });
}
// 只取一句台词的范围：整块放得下就整块，太长时截到 240 字以内最后一个句末标点（UTF-16 偏移，不切断代理对）。
export function favoriteRange(text) {
  const units = Array.from(String(text || ""));
  if (!units.length) return null;
  if (units.length <= FAVORITE_LIMIT) return [0, units.join("").length];
  let offset = 0, end = 0;
  for (let index = 0; index < FAVORITE_LIMIT; index += 1) {
    offset += units[index].length;
    if (SENTENCE_END.test(units[index])) end = offset;
  }
  return [0, end || offset];
}
export function compactTokens(value) {
  if (!Number.isFinite(value)) return "";
  if (value >= 10000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(value);
}
// 用量文案：未知次数不当成 0，厂商没返回 token 时直说，部分缺失才加“约”。
export function usageCopy(round) {
  if (!round || (round.calls == null && round.tokens == null)) return null;
  const calls = round.calls == null ? "调用次数未知" : `${round.calls} 次调用`;
  const approx = round.partial ? "约 " : "";
  const tokens = round.tokens == null ? "厂商未返回 token 数" : `${approx}${compactTokens(round.tokens)} tokens`;
  const shortTokens = round.tokens == null ? "未返回" : `${approx.trim()}${compactTokens(round.tokens)}`;
  return { long: `本轮 ${calls} · ${tokens}`, short: `${round.calls ?? "?"} 次 · ${shortTokens}`,
    label: `本轮用量：${calls}，${round.tokens == null ? "厂商未返回 token 数" : `${approx}${round.tokens} tokens`}。查看明细` };
}
function dayKey(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function clock(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Hong_Kong", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function dayLabel(key, today) {
  if (!key) return "时间未记录";
  if (key === today) return "今天";
  const [, month, day] = key.split("-").map(Number);
  return `${month}月${day}日`;
}
function speechRef(message) {
  if (!message || message.status !== "sent" || message.role !== "assistant") return null;
  const blocks = Array.isArray(message.contentBlocks) ? message.contentBlocks : [];
  const blockIndex = blocks.findIndex(block => ["speech", "message"].includes(block?.type) && String(block.text || "").trim());
  if (blockIndex < 0) return null;
  const range = favoriteRange(blocks[blockIndex].text);
  return range ? { messageId: message.id, blockIndex, start: range[0], end: range[1] } : null;
}
// 分页时星标收藏「当前这一页」：把分页器给的原文偏移换算回 contentBlocks 里的块和字符范围
// （一页跨两块时取重叠最多的那一块，去掉首尾空白，太长时同样截到 240 字内最后一个句末标点）。
export function pageRef(message, page) {
  if (!page || page.count < 2 || !message || message.status !== "sent" || message.role !== "assistant") return null;
  const blocks = Array.isArray(message.contentBlocks) ? message.contentBlocks : [];
  const built = buildReadingText(blocks);
  if (built.text !== page.text) return null;
  let best = null;
  for (const span of built.spans) {
    const start = Math.max(page.start, span.textStart), end = Math.min(page.end, span.textEnd);
    if (end > start && (!best || end - start > best.end - best.start)) best = { span, start, end };
  }
  if (!best) return null;
  const raw = String(blocks[best.span.blockIndex]?.text || "");
  let start = best.span.blockStart + best.start - best.span.textStart, end = best.span.blockStart + best.end - best.span.textStart;
  while (start < end && /\s/u.test(raw[start])) start += 1;
  while (end > start && /\s/u.test(raw[end - 1])) end -= 1;
  const range = end > start ? favoriteRange(raw.slice(start, end)) : null;
  return range ? { messageId: message.id, blockIndex: best.span.blockIndex, start, end: start + range[1], page: true } : null;
}
function highlight(text, ranges) {
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0])) {
    if (start < cursor) continue;
    fragment.append(text.slice(cursor, start), el("mark", { class: "snow-mark", text: text.slice(start, end) }));
    cursor = end;
  }
  fragment.append(text.slice(cursor));
  return fragment;
}

export async function installSnowFeatures(host) {
  if (document.documentElement.dataset.ui !== "snow-adv" || !host?.logic) return null;
  if (installation) return installation;
  installation = loadStyles().then(() => mount(host)).catch(error => { installation = null; throw error; });
  return installation;
}

function mount(host) {
  const { logic } = host;
  const icon = name => SNOW_FEATURE_ICON_NAMES.includes(name) ? createFeatureIcon(name) : host.icon(name);
  const snapshot = () => host.snapshot();
  const characterById = id => snapshot().characters?.find(item => item.character_id === id) || null;
  const nameOf = id => characterById(id)?.display_name || "角色";
  const showUsage = () => logic.usage.preferences()?.showUsage !== false;
  const messages = new Map();
  const message = id => {
    if (!id) return Promise.resolve(null);
    if (!messages.has(id)) messages.set(id, logic.messages.get(id).catch(() => null));
    return messages.get(id);
  };
  let toastTimer = 0;
  function notify(text) {
    const toast = $("toast");
    if (!toast) return;
    toast.textContent = text;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3200);
  }
  function readOnly() {
    if (snapshot().writerOwned !== false) return false;
    notify("此页面仅供浏览，另一个标签页正在保存对话。");
    return true;
  }
  function setStar(button, pressed, noun = "这句台词") {
    button.setAttribute("aria-pressed", String(pressed));
    const label = pressed ? `取消收藏${noun}` : `收藏${noun}`;
    button.setAttribute("aria-label", label);
    button.title = label;
  }
  async function toggleFavorite(ref, button) {
    if (!ref || readOnly()) return;
    const wanted = !logic.favorites.has(ref);
    button.disabled = true;
    try {
      const now = await logic.favorites.toggle(ref);
      setStar(button, now, ref.page ? "这一页台词" : undefined);
      if (wanted && !now) notify("没能收藏：最多保存 200 条，或这句台词还没有保存完成。");
      else notify(now ? "已收藏，可在回看的「收藏」里找到" : "已取消收藏");
      syncFavoriteCount();
      if (mode === "favorites") renderFavorites();
    } finally { button.disabled = false; }
  }

  // ---------- 本轮用量：面对面名牌一行 + 通讯器每条回复下方 ----------
  let popover = null;
  function closePopover(restore = true) {
    if (!popover) return;
    const { node, anchor } = popover;
    popover = null;
    node.remove();
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", escape, true);
    if (restore) anchor.focus({ preventScroll: true });
  }
  function outside(event) { if (popover && !popover.node.contains(event.target) && !popover.anchor.contains(event.target)) closePopover(false); }
  function escape(event) { if (event.key === "Escape") { event.stopPropagation(); closePopover(); } }
  function usageRow(label, note, calls, tokens, className = "") {
    return el("div", { class: `snow-usage-row ${className}`.trim() }, el("span", {}, label, note ? el("small", { text: note }) : null),
      el("span", { text: calls }), el("span", { text: tokens }));
  }
  function openUsage(anchor, messageId) {
    closePopover(false);
    const round = logic.usage.round(messageId), today = logic.usage.today();
    const node = el("section", { class: "snow-pop", role: "dialog", "aria-label": "本轮用量明细", tabindex: "-1" }, el("h3", { text: "本轮用量" }));
    for (const part of round.parts || []) {
      const note = part.kind === "summary" ? "每 12 轮自动整理一次，可在设置中关闭" : part.model || "";
      node.append(usageRow(PART_LABELS[part.kind] || "调用", note, part.calls == null ? "次数未知" : `${part.calls} 次`,
        part.tokens == null ? "未返回" : `${part.tokens.toLocaleString("zh-CN")} tokens`));
    }
    node.append(usageRow("合计", "", round.calls == null ? "次数未知" : `${round.calls} 次`,
      round.tokens == null ? "未返回" : `${round.partial ? "约 " : ""}${round.tokens.toLocaleString("zh-CN")} tokens`, "is-total"));
    const hint = round.tokens == null ? "厂商没有返回 token 数。" : round.partial ? "“约”表示部分调用的厂商没有返回 token 数。" : "";
    node.append(el("p", { class: "snow-hint", text: `${hint}费用由你的模型账户承担，以厂商账单为准。` }));
    const todayCopy = usageCopy(today);
    if (todayCopy) node.append(el("p", { class: "snow-usage-today", text: `今天（本机统计）：${todayCopy.long.replace(/^本轮 /, "")}` }));
    document.body.append(node);
    const rect = anchor.getBoundingClientRect();
    const width = node.offsetWidth, height = node.offsetHeight;
    node.style.left = `${Math.max(12, Math.min(innerWidth - width - 12, rect.right - width))}px`;
    node.style.top = `${rect.top - height - 10 >= 12 ? rect.top - height - 10 : Math.min(innerHeight - height - 12, rect.bottom + 10)}px`;
    popover = { node, anchor };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape, true);
    node.focus({ preventScroll: true });
  }
  function paintUsage(button, messageId) {
    const copy = showUsage() && messageId ? usageCopy(logic.usage.round(messageId)) : null;
    button.hidden = !copy;
    if (!copy) return;
    button.querySelector(".snow-usage-long").textContent = copy.long;
    button.querySelector(".snow-usage-short").textContent = copy.short;
    button.setAttribute("aria-label", copy.label);
    button.dataset.messageId = messageId;
  }
  function usageButton(className) {
    const button = el("button", { type: "button", class: className, "aria-haspopup": "dialog", hidden: true },
      icon("gauge"), el("span", { class: "snow-usage-long" }), el("span", { class: "snow-usage-short" }));
    button.addEventListener("click", () => {
      if (popover?.anchor === button) closePopover();
      else openUsage(button, button.dataset.messageId);
    });
    return button;
  }

  // 面对面：名牌一行右侧放星标和本轮用量。当前台词以 #stage-open-feedback 记录的消息 id 为准。
  const speakerLine = document.querySelector("#stage-dialogue .stage-speaker-line");
  const stageStar = el("button", { type: "button", class: "snow-round snow-fav", id: "snow-stage-favorite", hidden: true }, icon("star"));
  const stageUsage = usageButton("snow-chip snow-usage-chip");
  stageUsage.id = "snow-stage-usage";
  setStar(stageStar, false);
  if (speakerLine) speakerLine.insertBefore(el("span", { class: "snow-line-tools" }, stageStar, stageUsage), speakerLine.querySelector(":scope > span"));
  let stageRef = null, stageVersion = 0, pager = null;
  async function refreshStage() {
    const version = ++stageVersion;
    const id = $("stage-open-feedback")?.dataset.messageId || "";
    paintUsage(stageUsage, id);
    const current = await message(id);
    if (version !== stageVersion) return;
    // 台词分成几页时收藏正在看的这一页，只有一页时收藏整句（与回看里一致）。
    stageRef = pageRef(current, pager?.currentPage()) || speechRef(current);
    stageStar.hidden = !stageRef;
    if (stageRef) setStar(stageStar, logic.favorites.has(stageRef), stageRef.page ? "这一页台词" : undefined);
  }
  stageStar.addEventListener("click", event => { event.stopPropagation(); toggleFavorite(stageRef, stageStar); });
  stageUsage.addEventListener("click", event => event.stopPropagation());
  const feedback = $("stage-open-feedback");
  if (feedback) new MutationObserver(() => refreshStage()).observe(feedback, { attributes: true, attributeFilter: ["data-message-id"] });
  // 长台词分页：排版和绘制在 snow-pager.js，节奏与已读在 logic.reading。翻页后星标跟着换成这一页。
  try { pager = installSnowPager(host, { icon, onPage: () => refreshStage() }); }
  catch { pager = null; }
  // 注册时宿主会把已在舞台上的台词交给分页器，那次 onPage 早于上面的赋值；这里按当前页再刷新一次星标。
  if (pager) void refreshStage();

  // 通讯器：每条有用量的回复在名字那一行末尾加一段小字，点开同一个明细。
  // 挂在 .meta 里而不是 .message-body：时间线增量更新会比较 .message-body 的子元素个数。
  function decorateTimeline() {
    for (const article of document.querySelectorAll("#timeline article.message.assistant[data-message-id]")) {
      const meta = article.querySelector(".message-body > .meta");
      if (!meta) continue;
      let button = meta.querySelector(":scope > .snow-message-usage");
      if (!button) { button = usageButton("snow-message-usage"); meta.append(button); }
      paintUsage(button, article.dataset.messageId);
      // 约定提醒时她主动发来的消息：名字一行加一枚小标签，和普通回复区分开。
      if (!meta.querySelector(":scope > .snow-proactive-tag")) message(article.dataset.messageId).then(item => {
        if (item?.source !== "proactive_reminder" || !meta.isConnected || meta.querySelector(":scope > .snow-proactive-tag")) return;
        meta.insertBefore(el("span", { class: "snow-proactive-tag", text: "约定提醒 · 主动发来" }), meta.querySelector(":scope > .snow-message-usage"));
      });
    }
  }

  // ---------- 回看：搜索、收藏 ----------
  const panel = $("transcript-panel");
  const content = $("transcript-content");
  const searchInput = el("input", { type: "search", id: "snow-backlog-search", placeholder: "搜索台词或动作", "aria-label": "搜索回看记录", autocomplete: "off" });
  const clearButton = el("button", { type: "button", class: "snow-round", "aria-label": "清除搜索", hidden: true }, icon("x"));
  const tabAll = el("button", { type: "button", role: "radio", "aria-checked": "true", "data-snow-backlog-tab": "all" }, "全部");
  const tabFavorites = el("button", { type: "button", role: "radio", "aria-checked": "false", "data-snow-backlog-tab": "favorites" }, "收藏");
  const summary = el("span", { class: "snow-search-summary", role: "status", "aria-live": "polite" });
  const results = el("div", { class: "transcript-content snow-backlog-results", id: "snow-backlog-results", hidden: true });
  const tools = el("div", { class: "snow-backlog-tools", id: "snow-backlog-tools" },
    el("label", { class: "snow-search" }, icon("search"), searchInput, clearButton),
    el("div", { class: "snow-backlog-bar" }, el("div", { class: "snow-seg", role: "radiogroup", "aria-label": "回看内容" }, tabAll, tabFavorites), summary));
  if (panel && content) { panel.querySelector(":scope > header")?.after(tools); content.after(results); }
  let mode = "all", searchTimer = 0, searchVersion = 0;
  function setMode(next) {
    mode = next;
    tabAll.setAttribute("aria-checked", String(next !== "favorites"));
    tabFavorites.setAttribute("aria-checked", String(next === "favorites"));
    const custom = next !== "all";
    results.hidden = !custom;
    if (content) content.hidden = custom;
    if (!custom) { results.replaceChildren(); summary.textContent = ""; }
  }
  function syncFavoriteCount() {
    const count = logic.favorites.list({ characterId: snapshot().selected }).length;
    tabFavorites.textContent = count ? `收藏 ${count}` : "收藏";
  }
  function entryHeader(item) {
    const who = item.role === "user" ? "你" : nameOf(snapshot().selected);
    const when = item.createdAt ? `${dayLabel(dayKey(item.createdAt), snapshot().localDayKey)} ${clock(item.createdAt)}` : "";
    return el("strong", { text: [who, item.channel === "in_person" ? "面对面" : "文字通讯", when].filter(Boolean).join(" · ") });
  }
  async function viewInTranscript(messageId) {
    searchInput.value = "";
    clearButton.hidden = true;
    setMode("all");
    if (!(await host.open.transcript({ messageId }))) notify("这条记录已经不在本机了。");
  }
  async function runSearch() {
    const version = ++searchVersion;
    const query = searchInput.value.trim();
    clearButton.hidden = !query;
    if (!query) { setMode(tabFavorites.getAttribute("aria-checked") === "true" ? "favorites" : "all"); if (mode === "favorites") renderFavorites(); return; }
    setMode("search");
    summary.textContent = "正在搜索……";
    const found = await logic.transcript.search(snapshot().selected, query);
    if (version !== searchVersion) return;
    const hits = found.reduce((total, item) => total + item.ranges.length, 0);
    summary.textContent = found.length ? `“${query}” · ${hits} 处${found.length >= 200 ? "（只显示最近 200 条）" : ""}` : `没有找到“${query}”`;
    const list = el("div", { class: "snow-transcript-list" });
    for (const item of found) {
      const view = el("button", { type: "button", class: "secondary-button snow-small" }, "查看上下文");
      view.addEventListener("click", () => viewInTranscript(item.messageId));
      list.append(el("article", { class: "transcript-entry snow-search-hit" }, entryHeader(item),
        el("p", { class: "snow-hit-text" }, highlight(item.text, item.ranges)), el("div", { class: "snow-fav-actions" }, view)));
    }
    results.replaceChildren(found.length ? list : el("p", { class: "snow-empty", text: "换个关键词试试，比如人名、地点或动作。" }));
  }
  function renderFavorites() {
    const selected = snapshot().selected;
    const favorites = logic.favorites.list({ characterId: selected }).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    summary.textContent = favorites.length ? "只保存在本浏览器" : "";
    if (!favorites.length) { results.replaceChildren(el("p", { class: "snow-empty", text: "还没有收藏。点台词旁的星标，就能把喜欢的句子留在这里。" })); return; }
    const list = el("div", { class: "snow-transcript-list" });
    for (const favorite of favorites) {
      const unstar = el("button", { type: "button", class: "snow-round snow-fav" }, icon("star"));
      setStar(unstar, true);
      unstar.addEventListener("click", () => toggleFavorite(favorite, unstar));
      const view = el("button", { type: "button", class: "secondary-button snow-small" }, "在回看中查看");
      view.addEventListener("click", () => viewInTranscript(favorite.messageId));
      const card = el("button", { type: "button", class: "secondary-button snow-small" }, icon("image-down"), "生成图片卡");
      card.addEventListener("click", async () => {
        try { (await import(CARD_URL)).openFavoriteCard({ host, favorite, icon, message: await message(favorite.messageId) }); }
        catch { notify("图片卡暂时打不开，请稍后再试。"); }
      });
      const meta = [nameOf(favorite.characterId), favorite.locationName, favorite.createdAt ? dayLabel(dayKey(favorite.createdAt), snapshot().localDayKey) : ""].filter(Boolean).join(" · ");
      list.append(el("article", { class: "snow-fav-card", "data-snow-character": favorite.characterId },
        el("blockquote", { text: favorite.text }),
        el("div", { class: "snow-fav-meta" }, el("span", { class: "snow-avatar", "aria-hidden": "true", text: Array.from(nameOf(favorite.characterId))[0] }), el("span", { text: meta })),
        el("div", { class: "snow-fav-actions" }, unstar, view, card)));
    }
    results.replaceChildren(list);
  }
  searchInput.addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(runSearch, 220); });
  searchInput.addEventListener("keydown", event => { if (event.key === "Escape" && searchInput.value) { event.stopPropagation(); searchInput.value = ""; runSearch(); } });
  clearButton.addEventListener("click", () => { searchInput.value = ""; runSearch(); searchInput.focus(); });
  tabAll.addEventListener("click", () => { searchInput.value = ""; clearButton.hidden = true; setMode("all"); });
  tabFavorites.addEventListener("click", () => { searchInput.value = ""; clearButton.hidden = true; setMode("favorites"); renderFavorites(); });
  // 「全部」里每条角色回复右上角放星标；回看列表重绘后会自动补上。
  async function decorateTranscript() {
    for (const entry of content?.querySelectorAll(".transcript-entry[data-message-id]") || []) {
      if (entry.querySelector(":scope > .snow-entry-fav")) continue;
      const id = entry.dataset.messageId;
      const ref = speechRef(await message(id));
      if (!ref || !entry.isConnected || entry.querySelector(":scope > .snow-entry-fav")) continue;
      const star = el("button", { type: "button", class: "snow-round snow-fav snow-entry-fav" }, icon("star"));
      setStar(star, logic.favorites.has(ref));
      star.addEventListener("click", () => toggleFavorite(ref, star));
      entry.dataset.snowCharacter = snapshot().selected || "";
      entry.append(star);
    }
  }

  // ---------- 记忆档案 ----------
  const dialog = el("dialog", { class: "modal snow-feature-modal snow-memory-modal", id: "snow-memory-dialog", "aria-labelledby": "snow-memory-title" });
  const memoryTitle = el("h2", { id: "snow-memory-title" });
  const memoryClose = el("button", { type: "button", class: "icon-button", "aria-label": "关闭记忆档案" }, icon("x"));
  // 两个视图的标签页：她记得的事（签名记忆）/ 通讯器对话（本机历史）。roving tabindex + aria-selected。
  const tabMemories = el("button", { type: "button", role: "tab", id: "snow-memory-tab-remembers", "aria-selected": "true", "aria-controls": "snow-memory-view-remembers", tabindex: "0" }, "她记得的事");
  const tabConvo = el("button", { type: "button", role: "tab", id: "snow-memory-tab-convo", "aria-selected": "false", "aria-controls": "snow-memory-view-convo", tabindex: "-1" }, "通讯器对话");
  const memoryTabs = el("div", { class: "snow-memory-tabs", role: "tablist", "aria-label": "记忆档案视图" }, tabMemories, tabConvo);
  const memoryFilters = el("div", { class: "snow-memory-filters", role: "group", "aria-label": "按类别筛选" });
  const memoryList = el("div", { class: "snow-memory-list", id: "snow-memory-list", role: "tabpanel", "aria-labelledby": "snow-memory-tab-remembers" });
  const convoView = el("div", { class: "snow-memory-list snow-memory-convo", id: "snow-memory-view-convo", role: "tabpanel", "aria-labelledby": "snow-memory-tab-convo", hidden: true });
  const memoryCount = el("span", { role: "status", "aria-live": "polite" });
  const memoryDone = el("button", { type: "button", class: "secondary-button" }, "关闭");
  memoryList.id = "snow-memory-view-remembers";
  dialog.append(
    el("header", {}, el("div", {}, el("span", { class: "panel-kicker", text: "本机记录" }), memoryTitle,
      el("p", { text: "她记得的事，和你们在通讯器里聊过的内容，都只保存在这个浏览器里。删除后，之后的对话不会再用到。" })), memoryClose),
    memoryTabs, memoryFilters, memoryList, convoView,
    el("div", { class: "snow-modal-foot" }, memoryCount, el("div", { class: "modal-actions" }, memoryDone)));
  document.body.append(dialog);
  const memory = { characterId: "", items: [], filter: "all", confirming: "", removed: new Set(), pending: "", view: "remembers" };
  let convo = null;
  async function ensureConvo() {
    if (convo) return convo;
    const module = await import(CONVO_URL);
    convo = module.createMemoryConvoView(host, { el, icon, dayLabel, clock, dayKey, nameOf, openTranscript: viewInTranscript });
    convoView.replaceChildren(convo.node);
    return convo;
  }
  async function setMemoryView(next) {
    memory.view = next;
    const onConvo = next === "convo";
    tabMemories.setAttribute("aria-selected", String(!onConvo));
    tabConvo.setAttribute("aria-selected", String(onConvo));
    tabMemories.tabIndex = onConvo ? -1 : 0;
    tabConvo.tabIndex = onConvo ? 0 : -1;
    memoryList.hidden = onConvo;
    memoryFilters.hidden = onConvo || !memory.items.length;
    convoView.hidden = !onConvo;
    if (onConvo) { memoryCount.textContent = "只保存在本浏览器"; (await ensureConvo()).render(memory.characterId); }
    else { renderMemories(); }
  }
  tabMemories.addEventListener("click", () => { setMemoryView("remembers"); tabMemories.focus(); });
  tabConvo.addEventListener("click", () => { setMemoryView("convo"); tabConvo.focus(); });
  memoryTabs.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "ArrowRight" || event.key === "End" ? "convo" : "remembers";
    setMemoryView(next);
    (next === "convo" ? tabConvo : tabMemories).focus();
  });
  memoryClose.addEventListener("click", () => dialog.close());
  memoryDone.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    logic.memories.clearUndo(memory.characterId);
    memory.removed.clear();
    memory.confirming = "";
    convo?.destroy();
    refreshInfo();
  });
  async function loadMemories() { memory.items = await logic.memories.list(memory.characterId); if (memory.view === "remembers") renderMemories(); }
  async function openMemories(characterId = snapshot().selected) {
    if (!characterId) return;
    Object.assign(memory, { characterId, filter: "all", confirming: "", pending: "", view: "remembers" });
    memory.removed.clear();
    dialog.dataset.snowCharacter = characterId;
    memoryTitle.textContent = `${nameOf(characterId)}记得的事`;
    tabMemories.setAttribute("aria-selected", "true"); tabConvo.setAttribute("aria-selected", "false");
    tabMemories.tabIndex = 0; tabConvo.tabIndex = -1;
    memoryList.hidden = false; convoView.hidden = true;
    memoryList.replaceChildren(el("p", { class: "snow-empty", text: "正在读取……" }));
    if (!dialog.open) dialog.showModal();
    await loadMemories();
  }
  async function removeMemory(id) {
    if (readOnly()) return;
    memory.pending = id;
    renderMemories();
    const result = await logic.memories.remove(memory.characterId, id);
    memory.pending = "";
    memory.confirming = "";
    if (result?.ok) { memory.removed.add(id); memoryCount.textContent = "已删除这条记忆，关闭前可以撤销。"; }
    else notify(result?.reason === "busy" ? "她正在回应，等这一轮结束后再删除。" : "没能删除，请稍后再试。");
    renderMemories(Boolean(result?.ok));
  }
  async function restoreMemory(id) {
    memory.pending = id;
    renderMemories();
    const result = await logic.memories.restore(memory.characterId, id);
    memory.pending = "";
    if (result?.ok) { memory.removed.delete(id); await loadMemories(); memoryCount.textContent = "已恢复这条记忆。"; return; }
    notify("没能恢复，请稍后再试。");
    renderMemories();
  }
  function renderMemories(keepStatus = false) {
    const kept = memory.items.filter(item => !memory.removed.has(item.id));
    const counts = new Map();
    for (const item of kept) counts.set(item.kind, (counts.get(item.kind) || 0) + 1);
    const filters = [["all", "全部", kept.length], ...[...counts].map(([kind, count]) => [kind, KIND_LABELS[kind] || "记忆", count])];
    if (!filters.some(([key]) => key === memory.filter)) memory.filter = "all";
    memoryFilters.replaceChildren(...filters.map(([key, label, count]) => {
      const chip = el("button", { type: "button", class: "snow-chip", "aria-pressed": String(memory.filter === key) }, `${label} ${count}`);
      chip.addEventListener("click", () => { memory.filter = key; renderMemories(); });
      return chip;
    }));
    memoryFilters.hidden = !memory.items.length;
    if (!keepStatus) memoryCount.textContent = memory.items.length ? `共 ${kept.length} 条 · 删除只影响本机` : "";
    if (!memory.items.length) {
      memoryList.replaceChildren(el("p", { class: "snow-empty", text: "她还没有记住什么。面对面聊聊天、一起去个地方，或在通讯器里聊几句，她就会记下来。" }));
      return;
    }
    const today = snapshot().localDayKey;
    const nodes = [];
    let lastDay = null;
    for (const item of memory.items) {
      if (memory.filter !== "all" && item.kind !== memory.filter) continue;
      const key = item.occurredAt ? dayKey(item.occurredAt) : "";
      if (key !== lastDay) { nodes.push(el("h3", { class: "snow-memory-day", text: dayLabel(key, today) })); lastDay = key; }
      if (memory.removed.has(item.id)) {
        const undo = el("button", { type: "button", class: "secondary-button snow-small", disabled: memory.pending === item.id }, icon("undo-2"), "撤销");
        undo.addEventListener("click", () => restoreMemory(item.id));
        nodes.push(el("div", { class: "snow-memory-removed" }, el("span", { text: "已删除这条记忆" }), undo));
        continue;
      }
      const trash = el("button", { type: "button", class: "snow-round", "aria-label": `删除这条记忆：${item.text}`, title: "删除", disabled: Boolean(memory.pending) }, icon("trash-2"));
      trash.addEventListener("click", () => { memory.confirming = item.id; renderMemories(); memoryList.querySelector("[data-snow-confirm-cancel]")?.focus(); });
      const meta = [item.locationName, item.occurredAt ? clock(item.occurredAt) : ""].filter(Boolean).join(" · ");
      const source = MEMORY_SOURCE[item.kind] || "通讯器";
      const node = el("article", { class: `snow-memory-item${memory.confirming === item.id ? " is-confirming" : ""}` },
        el("div", {}, el("p", { class: "snow-memory-text", text: item.text }),
          el("div", { class: "snow-memory-meta" },
            el("span", { class: `snow-source${source === "面对面" ? " is-inperson" : " is-text"}`, text: source }),
            el("span", { class: "snow-kind", text: KIND_LABELS[item.kind] || "记忆" }), meta ? el("span", { text: meta }) : null)),
        trash);
      if (memory.confirming === item.id) {
        const cancel = el("button", { type: "button", class: "secondary-button snow-small", "data-snow-confirm-cancel": "" }, "取消");
        const confirm = el("button", { type: "button", class: "danger-button snow-small", disabled: memory.pending === item.id }, "删除");
        cancel.addEventListener("click", () => { memory.confirming = ""; renderMemories(); });
        confirm.addEventListener("click", () => removeMemory(item.id));
        node.append(el("div", { class: "snow-memory-confirm", role: "group", "aria-label": "确认删除" },
          el("span", { text: "删除这条记忆？之后的对话不会再用到它。" }), cancel, confirm));
      }
      nodes.push(node);
    }
    memoryList.replaceChildren(...nodes);
  }
  // 入口一：面对面菜单；入口二：通讯器「角色与场景」面板。
  const menu = document.querySelector("#stage-menu .scene-menu-popover");
  if (menu && !$("stage-open-memory")) {
    const entry = el("button", { type: "button", id: "stage-open-memory" }, "记忆档案");
    entry.addEventListener("click", () => { $("stage-menu").open = false; openMemories(); });
    menu.prepend(entry);
  }
  const info = $("info-content");
  let infoVersion = 0;
  async function refreshInfo() {
    const characterId = snapshot().selected;
    if (!info || !characterId) return;
    let card = info.querySelector(":scope > .snow-info-entry");
    if (!card) {
      const view = el("button", { type: "button", class: "secondary-button snow-small" }, "查看");
      view.addEventListener("click", () => openMemories());
      card = el("article", { class: "info-card snow-info-entry" }, el("div", {}, el("strong", { text: "她记得的事" }), el("p", { class: "snow-info-count" })), view);
      info.append(card);
    }
    card.dataset.snowCharacter = characterId;
    const version = ++infoVersion;
    const count = await logic.memories.count(characterId);
    if (version === infoVersion && card.isConnected) card.querySelector(".snow-info-count").textContent = count ? `${count} 条 · 只保存在本浏览器` : "还没有记住什么";
  }

  // ---------- 设置：「对话」分类（第二批界面）；「显示每轮用量」从「记录」移到这里 ----------
  const usageInput = el("input", { type: "checkbox", id: "snow-usage-enabled" });
  const usageToday = el("small", { id: "snow-usage-today" });
  const usageSetting = el("label", { class: "setting-toggle", id: "snow-usage-setting" },
    el("span", {}, el("strong", { text: "显示每轮用量" }), usageToday), usageInput);
  function syncSettings() {
    usageInput.checked = showUsage();
    const today = usageCopy(logic.usage.today());
    usageToday.textContent = `${today ? `今天（本机统计）：${today.long.replace(/^本轮 /, "")}` : "今天还没有模型调用"}。以厂商账单为准。`;
  }
  usageInput.addEventListener("change", async () => {
    if (readOnly()) { usageInput.checked = showUsage(); return; }
    await logic.usage.setPreferences({ showUsage: usageInput.checked });
    syncSettings();
    refreshUsage();
  });
  const conversation = logic.reading && logic.reminders
    ? installConversationUi(host, { icon, el, notify, readOnly, nameOf, usageSetting, onUsagePreference: syncSettings })
    : null;
  // 第二批逻辑不在时（旧宿主），每轮用量仍放回「记录」。
  if (!conversation) $("auto-summary-enabled")?.closest(".setting-toggle")?.after(usageSetting);
  const settings = $("settings-dialog");
  if (settings) new MutationObserver(() => { if (settings.open) syncSettings(); }).observe(settings, { attributes: true, attributeFilter: ["open"] });

  // ---------- 刷新时机 ----------
  let frame = 0;
  function refreshUsage() { paintUsage(stageUsage, $("stage-open-feedback")?.dataset.messageId || ""); decorateTimeline(); }
  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; refreshStage(); decorateTimeline(); decorateTranscript(); });
  }
  const timeline = $("timeline");
  if (timeline) new MutationObserver(schedule).observe(timeline, { childList: true, subtree: true });
  if (content) new MutationObserver(schedule).observe(content, { childList: true, subtree: true });
  if (info) new MutationObserver(() => { if (!info.querySelector(":scope > .snow-info-entry")) refreshInfo(); }).observe(info, { childList: true });
  host.on("message-saved", item => { if (item?.id) messages.delete(item.id); schedule(); });
  host.on("history-changed", () => { messages.clear(); schedule(); syncFavoriteCount(); refreshInfo(); });
  host.on("reply-presented", schedule);
  host.on("selected-changed", () => {
    closePopover(false);
    syncFavoriteCount();
    refreshInfo();
    if (mode === "search") runSearch(); else if (mode === "favorites") renderFavorites();
    schedule();
  });
  host.on("channel-changed", () => { closePopover(false); schedule(); });
  host.on("usage-saved", () => { refreshUsage(); syncSettings(); });
  syncFavoriteCount();
  refreshInfo();
  schedule();
  return { openMemories, refresh: schedule, pager, conversation };
}
