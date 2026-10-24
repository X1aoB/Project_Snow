// Local reading state and timing. Rendering and browser lifecycle belong to the host.
const clone = value => structuredClone(value);
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const array = value => Array.isArray(value) ? value : [];
const text = value => typeof value === "string" ? value : "";
const PREF_KEY = "project-snow-public:reading:v1", AUTO_KEY = "project-snow-public:reading:auto";
const defaults = { textSpeed: "normal", autoDelay: "normal", fastForwardRead: true };
const boundary = (value, index) => !(index > 0 && index < value.length
  && /[\uD800-\uDBFF]/.test(value[index - 1]) && /[\uDC00-\uDFFF]/.test(value[index]));
export function normalizeReadingPreferences(value) {
  return { textSpeed: ["slow", "normal", "fast", "instant"].includes(value?.textSpeed) ? value.textSpeed : "normal",
    autoDelay: ["short", "normal", "long"].includes(value?.autoDelay) ? value.autoDelay : "normal",
    fastForwardRead: value?.fastForwardRead !== false };
}
function stored(storage, key, fallback) {
  try { return JSON.parse(storage?.getItem(key) || "null") ?? fallback; } catch { return fallback; }
}
function store(storage, key, value) { try { storage?.setItem(key, JSON.stringify(value)); } catch {} }
export function importReadingPreferences(storage, value, previous = stored(storage, PREF_KEY, defaults)) {
  const patch = {};
  if (["slow", "normal", "fast", "instant"].includes(value?.textSpeed)) patch.textSpeed = value.textSpeed;
  if (["short", "normal", "long"].includes(value?.autoDelay)) patch.autoDelay = value.autoDelay;
  if (typeof value?.fastForwardRead === "boolean") patch.fastForwardRead = value.fastForwardRead;
  const next = normalizeReadingPreferences({ ...normalizeReadingPreferences(previous), ...patch });
  if (Object.keys(patch).length) store(storage, PREF_KEY, next);
  return next;
}

export function buildReadingText(blocks) {
  let combined = "", previous;
  const spans = [];
  for (const [blockIndex, block] of array(blocks).entries()) {
    if (!["speech", "message"].includes(block?.type)) continue;
    const raw = text(block.text), value = raw.trim();
    if (!value) continue;
    let separator = "";
    if (previous) {
      separator = "\n";
      if (/\s$/.test(combined) || /^\s/.test(value)) separator = "";
      else if (blockIndex === previous.blockIndex + 1) {
        if (/[。！？…]$/.test(previous.value)) separator = /^[A-Za-z0-9]/.test(value) ? " " : "";
        else if (/[.!?]$/.test(previous.value)) separator = " ";
        else if (/[A-Za-z0-9][,;:]?$/.test(previous.value) && /^[A-Za-z0-9]/.test(value)) separator = " ";
      }
    }
    combined += separator;
    const textStart = combined.length;
    combined += value;
    spans.push({ blockIndex, textStart, textEnd: combined.length, blockStart: raw.indexOf(value) });
    previous = { blockIndex, value };
  }
  return { text: combined, spans };
}
function readEntries(record) {
  const entries = new Map();
  for (const item of array(record?.messages)) {
    if (!text(item?.messageId)) continue;
    const blocks = {};
    for (const [index, end] of Object.entries(object(item.blocks))) {
      if (/^\d+$/.test(index) && Number.isSafeInteger(end) && end >= 0) blocks[index] = end;
    }
    entries.set(item.messageId, { messageId: item.messageId, blocks,
      updatedAt: Number.isFinite(item.updatedAt) ? item.updatedAt : 0 });
  }
  return [...entries.values()].sort((a, b) => b.updatedAt - a.updatedAt || a.messageId.localeCompare(b.messageId)).slice(0, 200);
}
function defaultRevealPlan(graphemes, startIndex = 0) {
  const steps = [], batch = Math.max(1, Math.ceil(graphemes.length / 90));
  for (let index = startIndex; index < graphemes.length;) {
    let end = Math.min(graphemes.length, index + batch), delayAfter = 24;
    for (let cursor = index; cursor < end; cursor += 1) {
      const character = graphemes[cursor];
      const pause = /\n/.test(character) ? 180 : /[。！？!?…]/.test(character) ? 320
        : /[；;]/.test(character) ? 220 : /[，,、：:]/.test(character) ? 110 : 24;
      if (pause > 24) { end = cursor + 1; delayAfter = pause; break; }
    }
    steps.push({ end, delayAfter }); index = end;
  }
  const duration = 24 + steps.slice(0, -1).reduce((sum, step) => sum + step.delayAfter, 0), scale = Math.min(1, 5200 / duration);
  return { initialDelay: 24 * scale, steps: steps.map((step, index) => ({ end: step.end,
    delayAfter: index === steps.length - 1 ? 0 : step.delayAfter * scale })) };
}

export function createReadingLogic(adapter) {
  const clock = adapter.clock || { now: () => Date.now(), setTimeout: (fn, delay) => setTimeout(fn, delay), clearTimeout: id => clearTimeout(id) };
  const records = new Map(), messageIndex = new Map(), finishes = new Map();
  let preferences = normalizeReadingPreferences(stored(adapter.localPreferences, PREF_KEY, defaults));
  let auto = stored(adapter.tabPreferences, AUTO_KEY, false) === true;
  let generation = 0, disposed = false, queue = Promise.resolve(), pager = null, session = null, timer = null, autoTimer = null;
  let advanceUnsubscribe, autoUnsubscribe, backUnsubscribe;
  const env = { hidden: false, modalOpen: false, uiHidden: false, typing: false, paused: false };
  const token = () => ({ generation, epoch: adapter.snapshot().historyEpoch });
  const current = value => !disposed && value.generation === generation && value.epoch === adapter.snapshot().historyEpoch;
  const serial = operation => { const result = queue.then(operation); queue = result.catch(() => {}); return result; };
  const notify = (callback, value) => { try { callback?.(value); } catch {} };
  function indexRecords() {
    messageIndex.clear();
    for (const [characterId, record] of records) for (const item of record.messages) messageIndex.set(item.messageId, { characterId, ...item });
  }
  async function refresh() {
    generation += 1; cancel(); records.clear(); messageIndex.clear();
    const captured = token(), values = await (adapter.readStates?.() || []);
    if (!current(captured)) return;
    for (const record of array(values)) {
      const characterId = text(record?.characterId) || text(record?.key).replace(/^stage_read_v1:/, "");
      if (!characterId || !text(record?.key).startsWith("stage_read_v1:")) continue;
      records.set(characterId, { key: `stage_read_v1:${characterId}`, characterId, messages: readEntries(record) });
    }
    indexRecords();
  }
  function readUpTo(messageId, blockIndex) { return messageIndex.get(messageId)?.blocks?.[blockIndex] || 0; }
  function markRead(messageId, blockIndex, end) {
    const captured = token();
    return serial(async () => {
      if (!current(captured) || adapter.snapshot().writerOwned === false || !Number.isInteger(blockIndex) || blockIndex < 0
          || !Number.isSafeInteger(end) || end < 0 || typeof adapter.saveState !== "function") return false;
      const message = await adapter.getMessage(messageId);
      if (!current(captured) || adapter.snapshot().writerOwned === false || !message) return false;
      const block = array(message.contentBlocks || message.content_blocks)[blockIndex], value = text(block?.text);
      const characterId = text(message.characterId || message.character_id);
      if (!characterId || !["speech", "message"].includes(block?.type) || end > value.length || !boundary(value, end)) return false;
      if (readUpTo(messageId, blockIndex) >= end) return true;
      const existing = records.get(characterId) || { key: `stage_read_v1:${characterId}`, characterId, messages: [] };
      const item = existing.messages.find(row => row.messageId === messageId);
      const next = { ...clone(existing), messages: readEntries({ messages: [...existing.messages.filter(row => row.messageId !== messageId),
        { messageId, blocks: { ...item?.blocks, [blockIndex]: end }, updatedAt: clock.now() }] }) };
      await adapter.saveState(next.key, clone(next));
      if (!current(captured) || adapter.snapshot().writerOwned === false) return false;
      records.set(characterId, next); indexRecords(); return true;
    });
  }
  function autoDelayMs(chars) {
    return Math.max(1500, Math.min(9000, ({ short: 0.7, normal: 1, long: 1.4 })[preferences.autoDelay]
      * (900 + Math.max(0, Number(chars) || 0) * 55)));
  }
  function clearReveal() { if (timer !== null) clock.clearTimeout(timer); timer = null; }
  function stopAuto() {
    if (autoTimer !== null) clock.clearTimeout(autoTimer); autoTimer = null;
    if (session?.autoBegan !== null && session?.autoBegan !== undefined) {
      session.autoElapsed += Math.max(0, clock.now() - session.autoBegan); session.autoBegan = null;
    }
  }
  function paused() { return Object.values(env).some(Boolean); }
  // 旁白页：动作描写放不进第一页时由 pager 排在台词前面，start = end = 0，不占台词偏移、不记已读；
  // 已读跟着第一页台词走（重放读过的回复时旁白页同样标「已读」）。
  const pageText = (item, index) => { const page = item.pages[index];
    return page.lead ? item.lead.slice(page.lead.start, page.lead.end) : item.text.slice(page.start, page.end); };
  function pageRead(item, index) {
    const page = item.pages[index];
    if (page.lead) { const first = item.pages.findIndex(row => !row.lead); return first >= 0 && pageRead(item, first); }
    return item.spans.filter(span => span.textStart < page.end && span.textEnd > page.start).every(span => {
      const end = span.blockStart + Math.min(page.end, span.textEnd) - span.textStart;
      return readUpTo(item.message.id, span.blockIndex) >= end;
    });
  }
  function draw() {
    if (!session || !pager || disposed) return;
    const item = session, page = item.pages[item.index], duration = autoDelayMs(Array.from(pageText(item, item.index)).length);
    const elapsed = item.autoElapsed + (item.autoBegan === null ? 0 : Math.max(0, clock.now() - item.autoBegan));
    pager.render({ text: item.text, lead: item.lead, pages: clone(item.pages), index: item.index, revealedEnd: item.revealedEnd,
      read: pageRead(item, item.index), auto, autoProgress: auto ? Math.min(1, elapsed / duration) : 0 });
    if (!item.painted && (page.lead || item.revealedEnd > page.start)) { item.painted = true; notify(item.onFirstPaint); }
  }
  function finish(reason) {
    const item = session;
    if (!item || item.finished) return;
    item.finished = true; item.resolve(reason === "complete");
    notify(item.onComplete, { reason });
  }
  function completePage() {
    if (!session) return;
    const item = session, page = item.pages[item.index];
    item.revealedEnd = page.end; clearReveal(); draw();
    for (const span of item.spans.filter(span => span.textStart < page.end && span.textEnd > page.start)) {
      void markRead(item.message.id, span.blockIndex, span.blockStart + Math.min(page.end, span.textEnd) - span.textStart)
        .then(() => { if (session === item) draw(); }).catch(() => {});
    }
    if (item.index === item.pages.length - 1) finish("complete");
    else scheduleAuto();
  }
  function scheduleAuto() {
    stopAuto();
    if (!session || !auto || paused() || session.finished || session.revealedEnd < session.pages[session.index].end) return;
    const item = session, duration = autoDelayMs(Array.from(pageText(item, item.index)).length);
    item.autoBegan = clock.now();
    const tick = () => {
      autoTimer = null;
      if (session !== item || paused() || !auto) return;
      if (item.autoElapsed + clock.now() - item.autoBegan >= duration) { stopAuto(); advance(false); return; }
      draw(); autoTimer = clock.setTimeout(tick, Math.min(100, duration - item.autoElapsed - (clock.now() - item.autoBegan)));
    };
    autoTimer = clock.setTimeout(tick, Math.min(100, Math.max(0, duration - item.autoElapsed)));
  }
  function revealPage(preservedEnd = null) {
    clearReveal(); stopAuto();
    if (!session) return;
    const item = session, page = item.pages[item.index]; item.autoElapsed = 0; item.autoBegan = null;
    item.revealedEnd = preservedEnd === null ? page.start : Math.max(page.start, Math.min(page.end, preservedEnd));
    notify(adapter.announce, pageText(item, item.index));
    if (item.revealedEnd >= page.end || !item.animate || preferences.textSpeed === "instant" || pageRead(item, item.index)) { completePage(); return; }
    const value = item.text.slice(page.start, page.end), Segmenter = globalThis.Intl?.Segmenter;
    const graphemes = typeof Segmenter === "function"
      ? [...new Segmenter(undefined, { granularity: "grapheme" }).segment(value)].map(row => row.segment) : Array.from(value);
    let offset = page.start, startIndex = 0;
    while (startIndex < graphemes.length && offset < item.revealedEnd) offset += graphemes[startIndex++].length;
    const plan = adapter.revealPlan?.(graphemes, startIndex) || defaultRevealPlan(graphemes, startIndex), speed = { slow: 1.6, normal: 1, fast: 0.55 }[preferences.textSpeed];
    let index = 0; draw();
    const step = () => {
      timer = null; if (session !== item) return;
      const next = plan.steps[index++];
      if (!next) { completePage(); return; }
      item.revealedEnd = page.start + graphemes.slice(0, next.end).join("").length; draw();
      if (item.revealedEnd >= page.end) { completePage(); return; }
      timer = clock.setTimeout(step, Math.max(0, next.delayAfter * speed));
    };
    timer = clock.setTimeout(step, Math.max(0, plan.initialDelay * speed));
  }
  function advance(skipping) {
    if (!session) return false;
    const item = session;
    if (item.revealedEnd < item.pages[item.index].end) { completePage(); return true; }
    if (item.index === item.pages.length - 1) return false;
    let next = item.index + 1;
    if (skipping && preferences.fastForwardRead) while (next < item.pages.length - 1 && pageRead(item, next)) next += 1;
    item.index = next; revealPage(); return true;
  }
  // 回到上一页：那一页已经显示过，直接整页显示，不再逐字。
  function back() {
    if (!session || session.index === 0) return false;
    const item = session;
    item.index -= 1; revealPage(item.pages[item.index].end); return true;
  }
  function cancel(reason = "cancelled") { clearReveal(); stopAuto(); finish(reason); session = null; }
  function setPager(value) {
    if (disposed) return;
    if (value && ["paginate", "render", "onAdvance", "onToggleAuto"].some(key => typeof value[key] !== "function")) throw new TypeError("reading_pager_callbacks_required");
    if (value === pager) return;
    cancel(); if (typeof advanceUnsubscribe === "function") advanceUnsubscribe();
    if (typeof autoUnsubscribe === "function") autoUnsubscribe();
    if (typeof backUnsubscribe === "function") backUnsubscribe(); pager = value;
    advanceUnsubscribe = autoUnsubscribe = backUnsubscribe = undefined;
    if (pager) {
      advanceUnsubscribe = pager.onAdvance(() => advance(false));
      if (typeof pager.onBack === "function") backUnsubscribe = pager.onBack(() => back());
      autoUnsubscribe = pager.onToggleAuto(value => setAuto(typeof value === "boolean" ? value : !auto));
    }
  }
  function setAuto(value) { if (disposed) return auto; auto = value === true; store(adapter.tabPreferences, AUTO_KEY, auto); stopAuto(); if (session) session.autoElapsed = 0; scheduleAuto(); draw(); return auto; }
  function pagesFor(value, lead = "") {
    let all;
    try { all = pager.paginate(value, lead ? { lead } : undefined); } catch { return null; }
    if (!Array.isArray(all)) return null;
    let count = 0;
    while (all[count]?.lead) count += 1;
    const leads = all.slice(0, count).map(page => page.lead), pages = all.slice(count);
    // 旁白页必须按顺序整段覆盖动作描写，不切开代理对。
    if (count && (!lead || leads[0]?.start !== 0 || leads.at(-1)?.end !== lead.length
        || leads.some((range, index) => !Number.isInteger(range?.start) || !Number.isInteger(range?.end) || range.start >= range.end
          || (index && range.start !== leads[index - 1].end) || !boundary(lead, range.start) || !boundary(lead, range.end)))) return null;
    if (!pages.length || pages[0]?.start !== 0 || pages.at(-1)?.end !== value.length
        || pages.some((page, index) => !page || !Number.isInteger(page.start) || !Number.isInteger(page.end) || page.start >= page.end
          || (index && page.start !== pages[index - 1].end) || !boundary(value, page.start) || !boundary(value, page.end))) return null;
    return [...leads.map(({ start, end }) => ({ start: 0, end: 0, lead: { start, end } })), ...pages.map(({ start, end }) => ({ start, end }))];
  }
  function reflow() {
    if (!session || !pager || disposed) return false;
    const item = session, pages = pagesFor(item.text, item.lead);
    if (!pages) return false;
    const showing = item.pages[item.index].lead;
    if (showing) {
      // 停在旁白页时，换到新排版里含同一处旁白的那页；新排版不再需要旁白页时回到第一页。
      item.pages = pages; item.index = Math.max(0, pages.findIndex(page => page.lead && page.lead.start <= showing.start && showing.start < page.lead.end));
      revealPage(); return true;
    }
    const offset = item.revealedEnd, atStart = offset === item.pages[item.index].start;
    const index = pages.findIndex(page => atStart && offset < item.text.length
      ? page.start <= offset && offset < page.end : page.start < offset && offset <= page.end);
    item.pages = pages; item.index = Math.max(0, index); revealPage(offset); return true;
  }
  function present(options) {
    if (!pager || disposed || !options?.message?.id) return false;
    const message = clone(options.message), built = buildReadingText(message.contentBlocks || message.content_blocks), value = options.text ?? built.text;
    if (typeof value !== "string" || !value || (!options.spans && value !== built.text)) return false;
    const lead = text(options.lead), spans = clone(options.spans || built.spans), pages = pagesFor(value, lead);
    if (!pages) return false;
    const blocks = array(message.contentBlocks || message.content_blocks);
    if (spans.some(span => !Number.isInteger(span.blockIndex) || !Number.isInteger(span.blockStart) || span.blockStart < 0
        || !Number.isInteger(span.textStart) || !Number.isInteger(span.textEnd) || span.textStart < 0 || span.textEnd <= span.textStart
        || span.textEnd > value.length || value.slice(span.textStart, span.textEnd) !== text(blocks[span.blockIndex]?.text).slice(span.blockStart,
          span.blockStart + span.textEnd - span.textStart))) return false;
    const key = text(options.key) || `${message.characterId || message.character_id || ""}:${message.requestId || message.request_id || ""}:${message.id}:complete-speech`;
    if (session?.key === key && session.text === value && session.lead === lead) { draw(); return true; }
    cancel("superseded");
    let resolve; const done = new Promise(res => { resolve = res; }); finishes.set(key, done);
    if (finishes.size > 200) finishes.delete(finishes.keys().next().value);
    session = { key, message, text: value, lead, spans, pages: clone(pages), index: 0, revealedEnd: 0, animate: options.animate !== false,
      onFirstPaint: options.onFirstPaint, onComplete: options.onComplete, painted: false, finished: false,
      autoElapsed: 0, autoBegan: null, done, resolve };
    revealPage(); return true;
  }
  function wait(key, signal) {
    if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
    const promise = finishes.get(key) || Promise.resolve(false);
    if (!signal) return promise;
    return new Promise((resolve, reject) => {
      const abort = () => { cleanup(); reject(new DOMException("Aborted", "AbortError")); }, cleanup = () => signal.removeEventListener("abort", abort);
      signal.addEventListener("abort", abort, { once: true });
      promise.then(value => { cleanup(); resolve(value); }, error => { cleanup(); reject(error); });
    });
  }
  const unsubscribe = adapter.on?.("history-changed", () => { void refresh().catch(() => {}); });
  const ready = refresh();
  return { ready, refresh, readUpTo, markRead, autoDelayMs, setPager, present, reflow, wait, skip: () => advance(true), back, cancel,
    enabled: () => !disposed && pager !== null, owns: key => !disposed && finishes.has(key), active: () => !disposed && session !== null && !session.finished,
    preferences: () => clone(preferences),
    async setPreferences(patch) { if (!disposed) { preferences = normalizeReadingPreferences({ ...preferences, ...object(patch) }); store(adapter.localPreferences, PREF_KEY, preferences); } return clone(preferences); },
    async importPreferences(value) { if (!disposed) preferences = importReadingPreferences(adapter.localPreferences, value, preferences); return clone(preferences); },
    autoEnabled: () => auto, setAuto,
    environment(patch) { stopAuto(); for (const key of Object.keys(env)) if (typeof patch?.[key] === "boolean") env[key] = patch[key]; scheduleAuto(); draw(); },
    dispose() { disposed = true; generation += 1; cancel(); unsubscribe?.(); if (typeof advanceUnsubscribe === "function") advanceUnsubscribe();
      if (typeof autoUnsubscribe === "function") autoUnsubscribe();
      if (typeof backUnsubscribe === "function") backUnsubscribe(); pager = null; records.clear(); messageIndex.clear(); finishes.clear(); },
  };
}
