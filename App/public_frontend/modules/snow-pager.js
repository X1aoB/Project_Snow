// 面对面长台词分页器（按需加载）。只负责排版和绘制：逐字节奏、翻页时机、自动计时和已读都在
// host.logic.reading（modules/snow-reading-logic.js）。说明见 docs/snow-adv-ui-notes.md「第二批界面」。
const SENTENCE_END = /[。！？!?…]/u;
const CLOSERS = /[」』”’）)】》〉"'\]]/u;
const SOFT_BREAK = /[，、；：,;:—～]/u;
const SPACE = /\s/u;
// 断点优先级：放得下的范围里，句末标点至少用到 60% 才在句末断；否则在逗号等处断（至少 45%）；
// 再不行退回到 30% 以上的句末；都没有就按字切。这样既尽量整句翻页，又不会出现只用了半框的页。
const BREAK_RULES = [[SENTENCE_END, 0.6], [SOFT_BREAK, 0.45], [SENTENCE_END, 0.3]];

function segments(text) {
  const Segmenter = globalThis.Intl?.Segmenter;
  const parts = typeof Segmenter === "function"
    ? [...new Segmenter(undefined, { granularity: "grapheme" }).segment(text)].map(row => row.segment) : Array.from(text);
  const ends = [];
  let offset = 0;
  for (const part of parts) { offset += part.length; ends.push(offset); }
  return { parts, ends };
}

// 纯函数：给定「放得下的最远位置」，挑一个更自然的断点。ends 是每个字素结束处的 UTF-16 偏移。
export function chooseBreak(parts, ends, first, last) {
  if (last >= parts.length - 1) return parts.length - 1;
  const span = last - first + 1;
  const after = index => {
    let end = index;
    while (end + 1 < parts.length && CLOSERS.test(parts[end + 1])) end += 1;
    return end;
  };
  for (const [test, fill] of BREAK_RULES) {
    const floor = first + Math.ceil(span * fill) - 1;
    for (let index = last; index >= floor; index -= 1) {
      if (!test.test(parts[index])) continue;
      const end = after(index);
      if (end <= last) return end;
    }
  }
  return last;
}

export function installSnowPager(host, { icon, onPage = () => {} }) {
  const $ = id => document.getElementById(id);
  const dialogue = $("stage-dialogue"), speech = $("stage-speech"), surface = $("in-person-surface");
  if (!dialogue || !speech || typeof host.stage?.setPager !== "function") return null;
  const copy = speech.parentElement;
  const advanceListeners = new Set(), autoListeners = new Set();
  let geometry = null, last = null, frame = 0, disposed = false, pageKey = "";

  surface?.setAttribute("data-snow-pager", "1");
  if (!dialogue.hasAttribute("tabindex")) dialogue.tabIndex = 0;
  const hint = Object.assign(document.createElement("span"), { id: "snow-pager-hint", className: "sr-only",
    textContent: "台词较长时会分页。有下一页时，点对白框或按 Enter、空格、右方向键继续。" });
  dialogue.setAttribute("aria-describedby", hint.id);
  const meter = Object.assign(document.createElement("span"), { className: "snow-page-meter is-single" });
  meter.setAttribute("aria-hidden", "true");
  const progress = document.createElement("span");
  progress.className = "snow-auto-progress";
  progress.hidden = true;
  progress.setAttribute("aria-hidden", "true");
  progress.append(document.createElement("i"));
  dialogue.append(hint, meter, progress);

  const page = document.createElement("p");
  page.className = "snow-page";
  const spacer = Object.assign(document.createElement("span"), { className: "snow-reserve-spacer" });
  const reserve = Object.assign(document.createElement("span"), { className: "snow-reserve" });
  const words = document.createTextNode("");
  page.append(spacer, reserve, words);

  // ---------- 快捷菜单里的「自动」 ----------
  const autoButton = document.createElement("button");
  autoButton.type = "button";
  autoButton.dataset.stageQuick = "auto";
  autoButton.title = "自动翻页";
  autoButton.setAttribute("aria-label", "自动");
  autoButton.setAttribute("aria-pressed", String(host.logic.reading.autoEnabled()));
  const autoLabel = Object.assign(document.createElement("span"), { className: "stage-quick-label", textContent: "自动" });
  autoButton.append(icon("play"), autoLabel);
  autoButton.addEventListener("click", event => {
    event.stopPropagation();
    for (const listener of autoListeners) listener();
    autoButton.setAttribute("aria-pressed", String(host.logic.reading.autoEnabled()));
  });
  function mountAuto() {
    const menu = $("stage-quick-menu");
    if (menu && autoButton.parentElement !== menu) menu.prepend(autoButton);
  }
  mountAuto();

  // ---------- 排版测量 ----------
  function px(value) { return Number.parseFloat(value) || 0; }
  function measureBox() {
    const style = getComputedStyle(speech);
    return { width: speech.offsetWidth, height: speech.clientHeight - px(style.paddingTop) - px(style.paddingBottom) };
  }
  // 右下角留给 ▼、页码和快捷菜单的区域（相对台词内容区）；最后一行从它左边绕过去。
  function reservedCorner() {
    const menu = $("stage-quick-menu");
    dialogue.style.setProperty("--snow-menu-w", `${menu?.offsetWidth || 0}px`);
    const style = getComputedStyle(speech), box = speech.getBoundingClientRect();
    const right = box.right - px(style.paddingRight), bottom = box.bottom - px(style.paddingBottom);
    // 页码按最宽的样子（两位页数 + 已读）留位，翻页时正文不会因为页码变宽而重排。
    const shown = { text: meter.textContent, single: meter.classList.contains("is-single") };
    meter.textContent = "10 / 10 · 已读";
    meter.classList.remove("is-single");
    let left = Infinity, top = Infinity;
    for (const node of [menu, $("stage-advance"), meter]) {
      if (!node || !node.getClientRects().length) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.bottom <= box.top) continue;
      left = Math.min(left, rect.left); top = Math.min(top, rect.top);
    }
    meter.textContent = shown.text;
    meter.classList.toggle("is-single", shown.single);
    if (!Number.isFinite(left) || top >= bottom) return { width: 0, height: 0 };
    return { width: Math.max(0, Math.ceil(right - left + 10)), height: Math.max(0, Math.ceil(bottom - top + 4)) };
  }
  function measureGeometry() {
    const rest = dialogue.classList.contains("snow-page-rest");
    speech.classList.add("snow-paged");
    dialogue.classList.remove("snow-page-rest");
    const first = measureBox();
    dialogue.classList.add("snow-page-rest");
    const later = measureBox();
    dialogue.classList.toggle("snow-page-rest", rest);
    const corner = reservedCorner();
    const style = getComputedStyle(speech), lineHeight = px(style.lineHeight) || 32, fontSize = px(style.fontSize) || 18;
    // 浮动只要碰到一行就会把整行变窄：角落上沿按整行对齐。字形不占行高上下的留白，所以允许角落伸进上一行的留白里。
    const slack = Math.max(0, (lineHeight - fontSize * 1.3) / 2);
    const heights = [first.height, later.height];
    const spacers = heights.map(height => corner.height <= 0 ? height
      : Math.min(height, Math.max(0, Math.floor((height - corner.height + slack) / lineHeight)) * lineHeight));
    return { width: first.width, heights, spacers, corner };
  }
  function shapeReserve(fill, corner, height, spacer) {
    fill.style.height = `${spacer}px`;
    corner.style.width = `${geometry.corner.width}px`;
    corner.style.height = `${Math.max(0, height - spacer)}px`;
  }
  function measurer(width, height, spacer) {
    const box = document.createElement("div");
    box.className = "stage-speech snow-paged snow-measure";
    box.setAttribute("aria-hidden", "true");
    Object.assign(box.style, { position: "absolute", left: "0", top: "0", width: `${width}px`, height: "auto",
      boxSizing: "border-box", visibility: "hidden", pointerEvents: "none", overflow: "visible", contain: "layout style" });
    const p = document.createElement("p");
    p.className = "snow-page";
    p.style.height = `${height}px`;
    const fill = document.createElement("span"), corner = document.createElement("span");
    fill.className = "snow-reserve-spacer"; corner.className = "snow-reserve";
    shapeReserve(fill, corner, height, spacer);
    const node = document.createTextNode("");
    p.append(fill, corner, node);
    box.append(p);
    copy.append(box);
    return { fits: value => { node.data = value; return p.scrollHeight <= height + 0.5; }, remove: () => box.remove() };
  }

  function paginate(text) {
    const value = String(text || "");
    if (!value) return [{ start: 0, end: 0 }];
    geometry = measureGeometry();
    const { parts, ends } = segments(value);
    const lineHeight = px(getComputedStyle(speech).lineHeight) || 32;
    // 对白框没有排版（隐藏、未进入面对面）时整段作为一页，显示出来后再由 reflow 重新分页。
    if (geometry.width < 80 || Math.min(...geometry.heights) < lineHeight) { geometry.pending = true; return [{ start: 0, end: value.length }]; }
    const tools = geometry.heights.map((height, index) => measurer(geometry.width, height, geometry.spacers[index]));
    const pages = [];
    try {
      let first = 0;
      while (first < parts.length) {
        const fit = tools[pages.length ? 1 : 0].fits;
        const startOffset = first ? ends[first - 1] : 0;
        let low = first, high = parts.length - 1, best = first;
        if (fit(value.slice(startOffset, ends[high]))) best = high;
        else {
          while (low <= high) {
            const middle = (low + high) >> 1;
            if (fit(value.slice(startOffset, ends[middle]))) { best = middle; low = middle + 1; } else high = middle - 1;
          }
        }
        let end = chooseBreak(parts, ends, first, best);
        // 断点后面的空白和换行并进本页，下一页不以空行开头。
        while (end + 1 < parts.length && SPACE.test(parts[end + 1])) end += 1;
        pages.push({ start: startOffset, end: ends[end] });
        first = end + 1;
      }
    } finally { tools.forEach(tool => tool.remove()); }
    return pages;
  }

  // ---------- 绘制 ----------
  function setReady(ready) { $("stage-advance")?.classList.toggle("is-ready", ready); }
  // 「已读」只标这段台词这次出现之前就读过的页。logic 在一页显示完就记已读，
  // 若照搬 state.read，第一次读的每一页读完也会写「已读」。freshFrom 是这次出现时第一处未读的位置。
  let readText = null, freshFrom = Infinity;
  function render(state) {
    if (disposed) return;
    const current = state.pages[state.index];
    const revealing = state.revealedEnd < current.end;
    if (state.text !== readText) { readText = state.text; freshFrom = Infinity; }
    if (!state.read) freshFrom = Math.min(freshFrom, current.start);
    const readBefore = state.read && current.end <= freshFrom;
    last = { ...state, current };
    mountAuto();
    if (speech.firstElementChild !== page || speech.childNodes.length !== 1) speech.replaceChildren(page);
    speech.classList.add("snow-paged");
    speech.classList.remove("is-typing");
    speech.classList.toggle("is-revealing", revealing);
    page.classList.toggle("is-revealing", revealing);
    speech.setAttribute("aria-busy", String(revealing));
    dialogue.classList.toggle("snow-page-rest", state.index > 0);
    // 本页用的角落尺寸与分页测量时一致（第一页与后续页的台词区高度不同）。
    if (geometry?.spacers) { const kind = state.index > 0 ? 1 : 0; shapeReserve(spacer, reserve, geometry.heights[kind], geometry.spacers[kind]); }
    words.data = state.text.slice(current.start, state.revealedEnd);
    const multi = state.pages.length > 1;
    meter.classList.toggle("is-single", !multi);
    meter.textContent = multi ? `${state.index + 1} / ${state.pages.length}${readBefore ? " · 已读" : ""}` : "";
    setReady(!revealing);
    autoButton.setAttribute("aria-pressed", String(state.auto));
    const showProgress = state.auto && !revealing && state.index < state.pages.length - 1;
    progress.hidden = !showProgress;
    progress.style.setProperty("--snow-auto", String(showProgress ? Math.max(0, Math.min(1, state.autoProgress || 0)) : 0));
    const key = `${state.text.length}:${current.start}:${current.end}`;
    if (key !== pageKey) { pageKey = key; onPage(currentPage()); }
  }
  // 当前页在原文中的位置（canonical 文字的 UTF-16 偏移），给收藏「当前这一页」用。
  function currentPage() {
    if (!last || speech.firstElementChild !== page) return null;
    return { text: last.text, start: last.current.start, end: last.current.end, index: last.index, count: last.pages.length };
  }
  // 宿主改走旧呈现（打字提示、场景占位、没有 pager 的回复）时，收起分页专用的状态。
  function reset() {
    last = null;
    speech.classList.remove("snow-paged");
    dialogue.classList.remove("snow-page-rest");
    meter.classList.add("is-single"); meter.textContent = "";
    progress.hidden = true;
    if (pageKey) { pageKey = ""; onPage(null); }
  }
  const speechObserver = new MutationObserver(() => { if (speech.firstElementChild !== page) reset(); });
  speechObserver.observe(speech, { childList: true });
  const menuObserver = new MutationObserver(mountAuto);
  menuObserver.observe(dialogue, { childList: true });

  // ---------- 翻页输入 ----------
  function advance() { for (const listener of advanceListeners) listener(); }
  function onClick(event) {
    if (event.target.closest("button, a, input, textarea, select, details, summary, label, .snow-pop")) return;
    if (String(getSelection?.() || "").trim()) return;
    advance();
  }
  function onKey(event) {
    if (event.target !== dialogue || event.altKey || event.ctrlKey || event.metaKey) return;
    if (!["Enter", " ", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    advance();
  }
  dialogue.addEventListener("click", onClick);
  dialogue.addEventListener("keydown", onKey);

  // ---------- 尺寸或字体变化后重新分页 ----------
  let size = "";
  function reflowSoon() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (last || geometry?.pending) host.logic.reading.reflow(); });
  }
  const resizeObserver = new ResizeObserver(entries => {
    const rect = entries.at(-1)?.contentRect;
    const next = rect ? `${Math.round(rect.width)}x${Math.round(rect.height)}` : "";
    if (next === size) return;
    size = next;
    reflowSoon();
  });
  resizeObserver.observe(dialogue);
  const fonts = document.fonts;
  fonts?.addEventListener?.("loadingdone", reflowSoon);
  fonts?.ready?.then(reflowSoon).catch(() => {});

  const pager = {
    paginate,
    render,
    onAdvance(listener) { advanceListeners.add(listener); return () => advanceListeners.delete(listener); },
    onToggleAuto(listener) { autoListeners.add(listener); return () => autoListeners.delete(listener); },
  };
  host.stage.setPager(pager);
  return {
    currentPage,
    dispose() {
      disposed = true;
      host.stage.setPager(null);
      speechObserver.disconnect(); menuObserver.disconnect(); resizeObserver.disconnect();
      fonts?.removeEventListener?.("loadingdone", reflowSoon);
      dialogue.removeEventListener("click", onClick);
      dialogue.removeEventListener("keydown", onKey);
      cancelAnimationFrame(frame);
      reset();
      autoButton.remove(); meter.remove(); progress.remove(); hint.remove();
      surface?.removeAttribute("data-snow-pager");
    },
  };
}
