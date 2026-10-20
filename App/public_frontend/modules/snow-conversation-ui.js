// 雪境第二批界面（按需加载）：约定提醒（邀请时设定、约定卡补设、到点提醒卡、锁屏通知样式），以及设置「对话」分类
// （文字速度、自动翻页停留、已读快进、每轮用量、约定提醒、角色主动来消息与费用确认）。
// 调度、已读和收费请求都在 host.logic（reminders / reading / proactive）；这里只负责界面。说明见 docs/snow-adv-ui-notes.md。

const HK_OFFSET = 8 * 3600000; // 香港时间固定 UTC+8，没有夏令时
const SPEEDS = [["slow", "慢"], ["normal", "标准"], ["fast", "快"], ["instant", "立即显示"]];
const DELAYS = [["short", "短"], ["normal", "标准"], ["long", "长"]];
const SPEED_FACTOR = { slow: 1.6, normal: 1, fast: 0.55, instant: 0 };
const SAMPLE = "雪停了。要不要一起去观景区看看？那边现在应该能看到整片星空。";
const CHOICES = [["none", "不提醒"], ["30", "30 分钟后"], ["60", "1 小时后"], ["custom", "指定时间"]];
const REQUEST_STATUS = Object.freeze({ prepared: "没有发出，未调用模型", pending: "结果未确认", unknown: "结果未确认",
  completed: "已送达", failed: "没有送达" });
const ENABLE_ERRORS = Object.freeze({ server_disabled: "服务器暂未开放这项功能。", model_required: "请先在「模型」里连接你的模型。",
  model_changed: "模型刚刚变了，请重新确认一次。", cost_confirmation_required: "需要先确认费用说明。" });
const RECOVER_COPY = Object.freeze({ completed: "已找到这条消息，已放进通讯记录。", failed: "这条消息没有送达。",
  not_dispatched: "这条请求没有发出，没有调用模型。", result_unknown: "仍未确认结果，可以稍后再查。", missing: "本机已经没有这条请求的记录。" });

function hkParts(time) {
  return Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(time)).map(part => [part.type, part.value]));
}
function hkDayStart(now) { const p = hkParts(now); return Date.UTC(+p.year, +p.month - 1, +p.day) - HK_OFFSET; }
export function hkClock(time) {
  const date = new Date(time);
  if (Number.isNaN(date.getTime())) return "";
  const p = hkParts(date);
  return `${p.hour}:${p.minute}`;
}
// 提醒时刻：只能是今天（香港日期）且晚于现在。返回 { at } 或 { error }。
export function reminderTime(choice, now, custom = "") {
  const midnight = hkDayStart(now) + 86400000;
  let at = null;
  if (choice === "30" || choice === "60") at = now + Number(choice) * 60000;
  else if (choice === "custom") {
    const match = /^(\d{2}):(\d{2})$/.exec(custom || "");
    if (!match) return { error: "请选择提醒时间。" };
    at = hkDayStart(now) + (Number(match[1]) * 60 + Number(match[2])) * 60000;
  } else return { at: null };
  if (at >= midnight) return { error: "提醒只在今天有效，请选一个今天之内的时间。" };
  if (at <= now + 30000) return { error: "提醒时间要晚于现在。" };
  return { at };
}

export function installConversationUi(host, { icon, el, notify, readOnly, nameOf, usageSetting, onUsagePreference }) {
  const { logic } = host;
  const $ = id => document.getElementById(id);
  const snapshot = () => host.snapshot();
  const reminders = logic.reminders, reading = logic.reading;
  const proactive = () => logic.proactive || null;
  const remindersOn = () => reminders.preferences()?.enabled !== false;
  const cleanups = [];
  const on = (type, listener) => { const off = host.on(type, listener); if (typeof off === "function") cleanups.push(off); };

  // ---------- 提醒时间选择（邀请弹窗和约定卡共用） ----------
  function picker({ id, withNone }) {
    const label = `${id}-label`;
    const buttons = new Map();
    const seg = el("div", { class: "snow-seg", role: "radiogroup", "aria-labelledby": label });
    const time = el("input", { type: "time", id: `${id}-time`, step: "60" });
    const timeRow = el("label", { class: "snow-remind-time", hidden: true }, "提醒时间", time);
    const error = el("p", { class: "snow-remind-error", role: "alert", hidden: true });
    const node = el("div", { class: "snow-remind-pick", id, role: "group", "aria-labelledby": label },
      el("div", { class: "snow-remind-head", id: label }, icon("bell-ring"), el("span", { text: "到点提醒" }),
        el("small", { text: "只在今天有效 · 页面开着时提醒 · 不调用模型" })),
      seg, timeRow, error);
    let choice = withNone ? "none" : "30";
    function select(next, focus = false) {
      choice = next;
      for (const [key, button] of buttons) {
        button.setAttribute("aria-checked", String(key === next));
        button.tabIndex = key === next ? 0 : -1;
      }
      timeRow.hidden = next !== "custom";
      // 指定时间默认一小时后；快到午夜时停在 23:59，并由校验提示「只在今天有效」。
      if (next === "custom" && !time.value) time.value = hkClock(Math.min(Date.now() + 3600000, hkDayStart(Date.now()) + 86400000 - 60000));
      validate();
      if (focus) buttons.get(next)?.focus();
    }
    function availability() {
      const now = Date.now();
      for (const [key, button] of buttons) {
        const blocked = (key === "30" || key === "60") && Boolean(reminderTime(key, now).error);
        button.disabled = blocked;
        button.title = blocked ? "已经快到午夜，提醒只在今天有效" : "";
      }
      if (buttons.get(choice)?.disabled) select(withNone ? "none" : "custom");
    }
    function validate() {
      const result = reminderTime(choice, Date.now(), time.value);
      error.hidden = !result.error;
      error.textContent = result.error || "";
      return result;
    }
    for (const [key, text] of CHOICES) {
      if (key === "none" && !withNone) continue;
      const button = el("button", { type: "button", role: "radio", "aria-checked": "false", "data-snow-remind": key }, text);
      button.addEventListener("click", () => select(key));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        const keys = [...buttons.keys()].filter(item => !buttons.get(item).disabled);
        const step = ["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1;
        select(keys[(keys.indexOf(choice) + step + keys.length) % keys.length], true);
      });
      buttons.set(key, button);
      seg.append(button);
    }
    time.addEventListener("input", validate);
    return {
      node,
      reset(value = withNone ? "none" : "30", custom = "") { time.value = custom; availability(); select(buttons.get(value)?.disabled ? (withNone ? "none" : "custom") : value); },
      value: () => ({ choice, ...validate() }),
    };
  }

  // ---------- 一、「一起去哪里？」里设定提醒 ----------
  const movementDialog = $("movement-dialog"), movementForm = $("movement-form");
  const movementPick = picker({ id: "snow-movement-reminder", withNone: true });
  let intent = null;
  const planned = new Set();
  if (movementForm) {
    (movementForm.querySelector(".modal-actions") || null)?.before(movementPick.node);
    new MutationObserver(() => {
      if (!movementDialog.open) return;
      movementPick.node.hidden = !remindersOn() || snapshot().writerOwned === false;
      movementPick.reset();
    }).observe(movementDialog, { attributes: true, attributeFilter: ["open"] });
    movementForm.addEventListener("submit", () => {
      intent = null;
      if (movementPick.node.hidden) return;
      const value = movementPick.value();
      if (value.choice === "none") return;
      if (value.error || !value.at) { notify(`提醒没有设定：${value.error || "时间无效"}`); return; }
      const state = snapshot(), locationId = movementDialog.dataset.locationId || "";
      if (!locationId || !state.selected) return;
      const location = (state.movementCatalog || []).find(item => item.location_id === locationId);
      intent = { characterId: state.selected, locationId, locationName: location?.display_name || "", remindAt: value.at, since: Date.now() };
    });
  }
  // 邀请真正发出（用户消息带上这次请求的 UUID）后才登记提醒；同一请求只登记一次。
  on("message-saved", async item => {
    if (!intent || item?.role !== "user" || !item.requestId || planned.has(item.requestId)) return;
    if (Date.now() - intent.since > 120000) { intent = null; return; }
    if (item.characterId !== intent.characterId || item.movementLocationId !== intent.locationId) return;
    const plan = { ...intent, requestId: item.requestId };
    intent = null;
    planned.add(item.requestId);
    const result = await reminders.plan(plan).catch(() => ({ ok: false }));
    notify(result?.ok ? `她答应后，会在 ${hkClock(plan.remindAt)} 提醒你。` : "提醒没能设定，可以稍后在约定卡里再设。");
  });

  // ---------- 二、约定卡上补设提醒 ----------
  const remindDialog = el("dialog", { class: "modal snow-feature-modal snow-remind-modal", id: "snow-reminder-dialog", "aria-labelledby": "snow-reminder-title" });
  const remindTitle = el("h2", { id: "snow-reminder-title" });
  const remindPick = picker({ id: "snow-rendezvous-reminder", withNone: false });
  const remindClear = el("button", { type: "button", class: "secondary-button", hidden: true }, "取消提醒");
  const remindCancel = el("button", { type: "button", class: "secondary-button" }, "取消");
  const remindSave = el("button", { type: "button", class: "primary-button" }, "设定提醒");
  remindDialog.append(
    el("header", {}, el("div", {}, el("span", { class: "panel-kicker", text: "约定提醒" }), remindTitle,
      el("p", { text: "到点时在通讯器和锁屏提醒你；点「去见她」后仍会先请你确认，不会自动出发。" })),
      el("button", { type: "button", class: "icon-button", "aria-label": "关闭约定提醒", "data-snow-close": "" }, icon("x"))),
    remindPick.node,
    el("div", { class: "snow-modal-foot" }, remindClear, el("div", { class: "modal-actions" }, remindCancel, remindSave)));
  document.body.append(remindDialog);
  let remindTarget = null;
  remindDialog.querySelector("[data-snow-close]").addEventListener("click", () => remindDialog.close());
  remindCancel.addEventListener("click", () => remindDialog.close());
  function pendingRendezvous(characterId = snapshot().selected) {
    const world = snapshot().rendezvous;
    if (!world?.authoritative) return null;
    return world.items.find(item => item.character_id === characterId && item.rendezvous_id
      && (item.schedule_date || world.dayKey) === world.dayKey) || null;
  }
  function reminderFor(characterId, rendezvousId) {
    return reminders.active().find(item => item.characterId === characterId && item.rendezvousId === rendezvousId) || null;
  }
  function openRemind(rendezvous) {
    if (readOnly()) return;
    const existing = reminderFor(rendezvous.character_id, rendezvous.rendezvous_id);
    remindTarget = { characterId: rendezvous.character_id, rendezvousId: rendezvous.rendezvous_id, existing };
    remindDialog.dataset.snowCharacter = rendezvous.character_id;
    remindTitle.textContent = `${nameOf(rendezvous.character_id)} · ${rendezvous.location_name || "约定地点"}`;
    remindClear.hidden = !existing;
    remindSave.textContent = existing ? "改为这个时间" : "设定提醒";
    if (existing) remindPick.reset("custom", hkClock(existing.remindAt));
    else remindPick.reset("30");
    remindDialog.showModal();
  }
  remindSave.addEventListener("click", async () => {
    if (!remindTarget || readOnly()) return;
    const value = remindPick.value();
    if (value.error || !value.at) return;
    remindSave.disabled = true;
    try {
      const result = await reminders.set({ characterId: remindTarget.characterId, rendezvousId: remindTarget.rendezvousId, remindAt: value.at });
      if (result?.ok) { remindDialog.close(); notify(`会在 ${hkClock(value.at)} 提醒你。`); }
      else notify(result?.reason === "limit" ? "今天的提醒太多了，先关掉几条再设。" : "这个约定已经变了，没能设定提醒。");
    } catch { notify("提醒没能保存，请稍后再试。"); }
    finally { remindSave.disabled = false; }
  });
  remindClear.addEventListener("click", async () => {
    if (!remindTarget?.existing || readOnly()) return;
    await reminders.clear(remindTarget.existing.id).catch(() => null);
    remindDialog.close();
    notify("已取消这个约定的提醒。");
  });
  const rendezvousRoot = $("current-rendezvous");
  function decorateRendezvous() {
    if (!rendezvousRoot || !remindersOn()) { rendezvousRoot?.querySelector(".snow-remind-button")?.remove(); return; }
    const go = rendezvousRoot.querySelector('[data-current-rendezvous="go"]');
    const rendezvous = go ? pendingRendezvous() : null;
    let button = rendezvousRoot.querySelector(".snow-remind-button");
    if (!rendezvous) { button?.remove(); return; }
    if (!button) {
      button = el("button", { type: "button", class: "secondary-button snow-remind-button" }, icon("bell-ring"), el("span"));
      button.addEventListener("click", () => { const current = pendingRendezvous(); if (current) openRemind(current); });
      rendezvousRoot.querySelector('[data-current-rendezvous="cancel"]')?.before(button);
    }
    const existing = reminderFor(rendezvous.character_id, rendezvous.rendezvous_id);
    const text = existing ? (existing.status === "fired" ? "提醒已到" : `提醒 ${hkClock(existing.remindAt)}`) : "到点提醒";
    if (button.lastChild.textContent !== text) button.lastChild.textContent = text;
    const label = existing ? `约定提醒：${hkClock(existing.remindAt)}，点此修改` : "设定约定提醒";
    if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
    // 观察器也在看 disabled：只在真正变化时写，避免自己触发自己。
    if (button.disabled !== Boolean(go.disabled)) button.disabled = Boolean(go.disabled);
  }
  if (rendezvousRoot) new MutationObserver(decorateRendezvous).observe(rendezvousRoot, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });

  // ---------- 三、到点提醒卡（通讯器顶部）与锁屏通知 ----------
  const cards = el("section", { class: "snow-reminders", id: "snow-reminder-cards", "aria-label": "约定提醒", hidden: true });
  const textSurface = $("text-surface");
  textSurface?.insertBefore(cards, $("timeline"));
  function renderCards() {
    const due = remindersOn() ? reminders.active().filter(item => item.status === "fired") : [];
    cards.hidden = !due.length;
    const focused = cards.contains(document.activeElement) ? document.activeElement.dataset.snowReminderAction : "";
    cards.replaceChildren(...due.map(item => {
      const name = nameOf(item.characterId);
      const go = el("button", { type: "button", class: "primary-button snow-small", "data-snow-reminder-action": `go:${item.id}` }, "去见她");
      const later = el("button", { type: "button", class: "secondary-button snow-small", "data-snow-reminder-action": `later:${item.id}` }, "10 分钟后再提醒");
      const close = el("button", { type: "button", class: "snow-round", "aria-label": `关闭和${name}的约定提醒`, title: "关闭提醒", "data-snow-reminder-action": `close:${item.id}` }, icon("x"));
      go.addEventListener("click", async () => {
        if (!(await host.open.presence(item.characterId))) notify("她正在回应，等这一轮结束后再去。");
      });
      later.addEventListener("click", async () => {
        if (readOnly()) return;
        const result = await reminders.snooze(item.id, 10).catch(() => ({ ok: false }));
        notify(result?.ok ? `会在 ${hkClock(Date.now() + 600000)} 再提醒你。` : "这个约定已经变了，没能延后。");
      });
      close.addEventListener("click", async () => { if (!readOnly()) await reminders.clear(item.id).catch(() => null); });
      return el("article", { class: "snow-reminder", "data-snow-character": item.characterId, "data-reminder-id": item.id },
        el("span", { class: "snow-reminder-glyph", "aria-hidden": "true" }, icon("bell-ring")),
        el("strong", { text: `约定提醒 · ${item.locationName || "约定地点"}` }),
        close,
        el("p", { text: `和${name}约好的时间到了（${hkClock(item.remindAt)}）。` }),
        el("div", { class: "snow-reminder-actions" }, go, later));
    }));
    if (focused) (cards.querySelector(`[data-snow-reminder-action="${focused}"]`) || $("message-input"))?.focus({ preventScroll: true });
  }
  // 锁屏的约定提醒卡由宿主渲染；这里补上角色色，让它和普通公告区分开。
  function decorateLock() {
    const notes = document.getElementById("snow-lock-notes");
    if (!notes) return;
    for (const card of notes.querySelectorAll('.snow-note-card[data-snow-note="reminder"]')) {
      if (card.dataset.characterId && card.dataset.snowCharacter !== card.dataset.characterId) card.dataset.snowCharacter = card.dataset.characterId;
    }
  }
  const lockObserver = new MutationObserver(decorateLock);
  lockObserver.observe(document.body, { childList: true, subtree: true });
  cleanups.push(() => lockObserver.disconnect());
  on("reminder-due", ({ reminder } = {}) => {
    renderCards(); decorateRendezvous();
    if (reminder) notify(`约定提醒：和${nameOf(reminder.characterId)}约好的时间到了。`);
  });
  on("reminders-changed", () => { renderCards(); decorateRendezvous(); syncSettings(); });
  on("world-changed", () => { renderCards(); decorateRendezvous(); });
  on("selected-changed", decorateRendezvous);
  on("history-changed", () => { renderCards(); decorateRendezvous(); });

  // ---------- 四、设置：「对话」分类 ----------
  const historyTab = document.querySelector('[data-settings-tab="history"]');
  const historyPanel = $("settings-panel-history");
  const tab = el("button", { type: "button", role: "tab", id: "settings-tab-conversation", "data-settings-tab": "conversation",
    "aria-controls": "settings-panel-conversation", "aria-selected": "false", tabindex: "-1" }, "对话");
  const panel = el("section", { class: "settings-section snow-conversation-settings", "data-settings-panel": "conversation", role: "tabpanel",
    id: "settings-panel-conversation", "aria-labelledby": "settings-tab-conversation", hidden: true });
  tab.addEventListener("click", () => host.open.settings("conversation"));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-settings-tab]")], current = tabs.indexOf(tab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    host.open.settings(tabs[next].dataset.settingsTab);
    tabs[next].focus();
  });
  function segment(name, options, labelId) {
    const group = el("div", { class: "snow-seg", role: "radiogroup", "aria-labelledby": labelId });
    for (const [value, text] of options) {
      const button = el("button", { type: "button", role: "radio", "aria-checked": "false", [`data-snow-${name}`]: value }, text);
      group.append(button);
    }
    group.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...group.querySelectorAll("button")], index = buttons.findIndex(button => button.getAttribute("aria-checked") === "true");
      const next = buttons[(index + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + buttons.length) % buttons.length];
      next.click(); next.focus();
    });
    return group;
  }
  function markSegment(group, value) {
    for (const button of group.querySelectorAll("button")) {
      const checked = Object.values(button.dataset)[0] === value;
      button.setAttribute("aria-checked", String(checked));
      button.tabIndex = checked ? 0 : -1;
    }
  }
  const speedGroup = segment("speed", SPEEDS, "snow-speed-label");
  const sample = el("p", { class: "snow-speed-sample", "aria-hidden": "true", text: SAMPLE });
  const delayGroup = segment("delay", DELAYS, "snow-delay-label");
  const fastInput = el("input", { type: "checkbox", id: "snow-fast-forward-read" });
  const reminderInput = el("input", { type: "checkbox", id: "snow-reminders-enabled" });
  const proactiveInput = el("input", { type: "checkbox", id: "snow-proactive-enabled" });
  const proactiveNote = el("small", { id: "snow-proactive-note" });
  const requestList = el("div", { class: "snow-proactive-requests", id: "snow-proactive-requests", hidden: true });
  panel.append(
    el("p", { text: "面对面的台词显示方式、约定提醒和角色主动来消息。都只保存在这个浏览器里。" }),
    el("div", { class: "snow-setting-row" }, el("strong", { id: "snow-speed-label", text: "文字速度" }), speedGroup, sample,
      el("small", { class: "snow-hint", text: "只影响逐字显示的快慢，不改变她回复的时间。" })),
    el("div", { class: "snow-setting-row" }, el("strong", { id: "snow-delay-label", text: "自动翻页停留" }), delayGroup,
      el("small", { class: "snow-hint", text: "打开对白框里的「自动」后，每页读完停多久再翻页。台词越长停得越久。" })),
    el("label", { class: "setting-toggle" }, el("span", {}, el("strong", { text: "跳过时略过已读的页" }),
      el("small", { text: "按「跳过」时，直接跳到还没读过的那一页。" })), fastInput),
    usageSetting || null,
    el("label", { class: "setting-toggle" }, el("span", {}, el("strong", { text: "约定提醒" }),
      el("small", { text: "答应的约定到点时，在通讯器和锁屏提醒你。只在页面开着时提醒，不调用模型。" })), reminderInput),
    el("label", { class: "setting-toggle snow-proactive-setting", id: "snow-proactive-setting" }, el("span", {},
      el("strong", {}, "角色主动来消息", el("span", { class: "snow-cost-tag" }, icon("coins"), "会产生模型费用")), proactiveNote), proactiveInput),
    requestList);
  if (historyTab && historyPanel) { historyTab.after(tab); historyPanel.after(panel); }

  let sampleTimer = 0;
  function playSample() {
    window.clearTimeout(sampleTimer);
    const factor = SPEED_FACTOR[reading.preferences().textSpeed] ?? 1;
    const quiet = factor === 0 || document.documentElement.dataset.motionLevel === "off"
      || matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const chars = Array.from(SAMPLE);
    if (quiet) { sample.textContent = SAMPLE; return; }
    let shown = 0;
    const step = () => {
      shown += 1;
      sample.textContent = chars.slice(0, shown).join("");
      if (shown < chars.length) sampleTimer = window.setTimeout(step, (/[。？！，]/u.test(chars[shown - 1]) ? 180 : 42) * factor);
    };
    step();
  }
  speedGroup.addEventListener("click", async event => {
    const value = event.target.closest("button")?.dataset.snowSpeed;
    if (!value) return;
    await reading.setPreferences({ textSpeed: value });
    markSegment(speedGroup, value);
    playSample();
  });
  delayGroup.addEventListener("click", async event => {
    const value = event.target.closest("button")?.dataset.snowDelay;
    if (!value) return;
    await reading.setPreferences({ autoDelay: value });
    markSegment(delayGroup, value);
  });
  fastInput.addEventListener("change", async () => { await reading.setPreferences({ fastForwardRead: fastInput.checked }); });
  reminderInput.addEventListener("change", async () => {
    if (readOnly()) { syncSettings(); return; }
    try { await reminders.setPreferences({ enabled: reminderInput.checked }); }
    catch { notify("没能保存，请稍后再试。"); }
    if (!reminderInput.checked && proactive()?.preferences().enabled) await proactive().disable().catch(() => null);
    syncSettings(); renderCards(); decorateRendezvous();
  });

  // ---------- 五、角色主动来消息：开启前确认费用 ----------
  const costDialog = el("dialog", { class: "modal snow-feature-modal snow-cost-modal", id: "snow-proactive-dialog", "aria-labelledby": "snow-proactive-title" });
  const costError = el("p", { class: "snow-remind-error", role: "alert", hidden: true });
  const costCancel = el("button", { type: "button", class: "secondary-button" }, "暂不开启");
  const costConfirm = el("button", { type: "button", class: "primary-button" }, "我知道会产生费用，开启");
  costDialog.append(
    el("header", {}, el("div", {}, el("span", { class: "panel-kicker", text: "费用确认" }),
      el("h2", { id: "snow-proactive-title", text: "开启角色主动来消息？" })),
      el("button", { type: "button", class: "icon-button", "aria-label": "关闭费用确认", "data-snow-close": "" }, icon("x"))),
    el("div", { class: "snow-cost-notice" }, el("span", { class: "snow-cost-tag" }, icon("coins"), "会产生模型费用"),
      el("ul", {},
        el("li", { text: "约定提醒到点时，她会先给你发一条文字消息。" }),
        el("li", { text: "每条消息会调用一次你的模型，产生费用；使用订阅连接时会消耗订阅额度。" }),
        el("li", { text: "每天最多 3 条，每个约定最多 1 条；只在页面开着时发送，不会离线补发。" }),
        el("li", { text: "更换模型、重新连接或重新打开页面后，需要再确认一次。" }))),
    costError,
    el("div", { class: "snow-modal-foot" }, el("span", { text: "随时可以在这里关闭。" }), el("div", { class: "modal-actions" }, costCancel, costConfirm)));
  document.body.append(costDialog);
  costDialog.querySelector("[data-snow-close]").addEventListener("click", () => costDialog.close());
  costCancel.addEventListener("click", () => costDialog.close());
  costDialog.addEventListener("close", () => syncSettings());
  costConfirm.addEventListener("click", async () => {
    const client = proactive();
    if (!client || readOnly()) return;
    costConfirm.disabled = true; costError.hidden = true;
    try {
      const result = await client.enable({ acceptedCostNotice: true });
      if (result?.ok) { costDialog.close(); notify("已开启。约定提醒到点时，她会先给你发消息。"); }
      else { costError.textContent = ENABLE_ERRORS[result?.reason] || "没能开启，请稍后再试。"; costError.hidden = false; }
    } catch { costError.textContent = "没能开启，请检查网络后再试。"; costError.hidden = false; }
    finally { costConfirm.disabled = false; syncSettings(); }
  });
  proactiveInput.addEventListener("change", async () => {
    const client = proactive();
    if (!client || readOnly()) { syncSettings(); return; }
    if (proactiveInput.checked) {
      proactiveInput.checked = false;
      costError.hidden = true;
      costDialog.showModal();
      costConfirm.focus();
      return;
    }
    await client.disable().catch(() => null);
    notify("已关闭角色主动来消息。");
    syncSettings();
  });
  function proactiveState() {
    const client = proactive(), state = client?.preferences() || {}, snap = snapshot();
    if (!client) return { disabled: true, checked: false, note: "这项功能暂时不可用。" };
    if (!state.serverEnabled) return { disabled: true, checked: false, note: "服务器暂未开放这项功能。约定提醒照常可用，不调用模型。" };
    if (!remindersOn()) return { disabled: true, checked: false, note: "需要先开启上面的「约定提醒」。" };
    if (state.enabled) return { disabled: Boolean(state.busy), checked: true, note: "已开启：约定提醒到点时，她会先发一条消息，每条调用一次模型。每天最多 3 条。" };
    if (!snap.modelReady) return { disabled: true, checked: false, note: "需要先在「模型」里连接你的模型。" };
    return { disabled: Boolean(state.busy), checked: false,
      note: "约定提醒到点时，她会先给你发一条消息。默认关闭；开启前会说明费用。更换模型或重新打开页面后需要再确认一次。" };
  }
  function syncProactive() {
    const value = proactiveState();
    proactiveInput.checked = value.checked;
    proactiveInput.disabled = value.disabled;
    proactiveNote.textContent = value.note;
  }
  let requestVersion = 0;
  async function renderRequests() {
    const client = proactive(), version = ++requestVersion;
    if (!client) { requestList.hidden = true; return; }
    const rows = (await client.requests().catch(() => [])).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0)).slice(0, 5);
    if (version !== requestVersion) return;
    requestList.hidden = !rows.length;
    requestList.replaceChildren(el("strong", { text: "最近的主动消息" }), ...rows.map(row => {
      const status = REQUEST_STATUS[row.status] || "状态未知";
      const calls = row.usage?.provider_calls, tokens = row.usage?.total_tokens;
      const usage = [calls == null ? "" : `${calls} 次调用`, tokens == null ? "" : `${tokens.toLocaleString("zh-CN")} tokens`].filter(Boolean).join(" · ");
      const line = el("div", { class: `snow-request-row is-${row.status}`, "data-snow-character": row.characterId },
        el("span", {}, el("b", { text: nameOf(row.characterId) }), el("small", { text: [hkClock(row.createdAt), usage].filter(Boolean).join(" · ") })),
        el("span", { class: "snow-request-status", text: status }));
      if (["pending", "unknown"].includes(row.status)) {
        const check = el("button", { type: "button", class: "secondary-button snow-small" }, "查询原请求");
        check.title = "只查询同一个请求的结果，不会重新生成，也不会再次收费";
        check.addEventListener("click", async () => {
          check.disabled = true;
          const result = await client.recover(row.requestId).catch(() => ({ ok: false, reason: "result_unknown" }));
          notify(RECOVER_COPY[result?.ok ? "completed" : result?.status === "failed" ? "failed" : result?.reason] || RECOVER_COPY.result_unknown);
          renderRequests();
        });
        line.append(check);
      }
      return line;
    }));
  }
  function syncSettings() {
    const prefs = reading.preferences();
    markSegment(speedGroup, prefs.textSpeed);
    markSegment(delayGroup, prefs.autoDelay);
    fastInput.checked = prefs.fastForwardRead !== false;
    reminderInput.checked = remindersOn();
    onUsagePreference?.();
    syncProactive();
  }
  async function openPanel() {
    syncSettings(); playSample(); renderRequests();
    await proactive()?.refresh().catch(() => null);
    syncProactive();
  }
  new MutationObserver(() => { if (!panel.hidden) openPanel(); else window.clearTimeout(sampleTimer); })
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  on("proactive-changed", syncProactive);
  on("usage-saved", () => { if (!panel.hidden) renderRequests(); });

  renderCards();
  decorateRendezvous();
  syncSettings();
  return {
    renderCards, decorateRendezvous, syncSettings,
    dispose() { cleanups.forEach(cleanup => cleanup()); window.clearTimeout(sampleTimer); },
  };
}
