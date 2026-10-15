import { loadStageRelease, writeDraftsDatabase, MAX_VISIBLE_MESSAGES, historyWindow, reconcileMarkup, focusWithin, createHttpClient, pruneHistoryDatabase, mergeHistoryDatabase, clearHistoryDatabase, createSafeStorage, compareMessageCursor, beforeMessageCursor, buildIdentity, portableCopy, validateHistoryBackup, PRESENTATION_TIMING, remainingNaturalWait, segmentWait, CommittedReply, MotionScope, anonymousServerTimings } from "/modules/runtime.js";
import { DIALOGUE_PROTOCOL, SCENE_PROTOCOL, dialogueRecordKey, decodeSidecar, mergeDialogueReceipts, relevantDialogueReceipts, fitDialogueContext, isPureRendezvousJoin, sceneVisualKey } from "/dialogue-context.js";
import { createSnowIcon } from "/modules/snow-icons.js";
import { mountSnowWorld, activeSnowLoops } from "/modules/snow-world.js";

const localPreferences = createSafeStorage(() => window.localStorage);
const tabPreferences = createSafeStorage(() => window.sessionStorage);
// 雪境 ADV P6a：开关随 /public/v1/config 下发，皮肤只能在 config 解析后才切换。为避免
// 旧界面先闪一帧，仅当本机存在「上次见过雪境皮肤」标记时，才在 config 解析前把界面保持中性
// （data-snow-booting，由 loadConfig 解析后删除）。首访用户没有该标记，不做保持，直接接受切换。
const SNOW_LAST_SEEN_KEY = "project-snow-public:snow-adv:last-seen";
// 仅当存在标记时，用内联属性（非 <style> 元素，CSP 安全）把根节点暂设为不可见，直到
// config 解析后恢复，避免返回用户看到旧界面闪一帧。首访无标记则不保持，直接接受切换。
// 兜底：即使 config 迟迟不来，也在 1s 后恢复可见，绝不长时间卡白。
try {
  if (localPreferences.getItem(SNOW_LAST_SEEN_KEY) === "1") {
    document.documentElement.dataset.snowBooting = "1";
    document.documentElement.style.visibility = "hidden";
    window.setTimeout(() => { document.documentElement.style.visibility = ""; }, 1000);
  }
} catch { /* 存储不可用则不保持 */ }
function snowClearFirstFlashHold() {
  document.documentElement.style.visibility = "";
  delete document.documentElement.dataset.snowBooting;
}
const apiRoot = "/public/v1";
const publicHttp = createHttpClient(apiRoot);
const dialogueHttp = createHttpClient("/public/v2");
const dbName = "project-snow-public";
const dbVersion = 4;
const SUBSCRIPTION_PROVIDER = "codex_subscription";
const SUBSCRIPTION_MODEL = "gpt-5.6-luna";
const SUBSCRIPTION_EFFORT = "max";
const SUBSCRIPTION_EFFORT_LABELS = Object.freeze({ none: "无推理", minimal: "极低", low: "低", medium: "中", high: "高", xhigh: "更高", max: "极高", ultra: "最高" });
const SUBSCRIPTION_SESSION_KEY = "project-snow-public:subscription-pair";
const SCENE_ASSET_URLS = Object.freeze(JSON.parse(document.querySelector('meta[name="snow-scene-assets"]')?.content || "{}"));
const SCENE_KEYS = new Set(["generic", "quarters", "lounge", "training", "archive", "canteen", "observation", "medical", "corridor"]);
// Reuse the approved neutral face crop without changing the original media attribution.
const CHARACTER_AVATAR_OVERRIDES = Object.freeze({
  "447ed3c401c9": Object.freeze({
    thumbnail_src: "/assets/avatars/longyan-neutral-96.webp",
    src: "/assets/avatars/longyan-neutral-200.webp",
    portrait_focus_x: 50,
    portrait_focus_y: 50,
    portrait_scale: 1,
  }),
});
const state = {
  config: null,
  credential: "",
  credentialExpiresAt: 0,
  provider: "",
  model: "",
  reasoningEffort: "",
  modelSetupMode: "api",
  apiProviderChoice: "",
  subscription: createSubscriptionState(),
  characters: [],
  stickers: [],
  stickerCatalog: new Map(),
  stickerLoadPromise: null,
  stickerCursor: null,
  stickerHasMore: true,
  stickerSection: "character",
  stickerQuery: "",
  favoriteStickerIds: new Set(),
  favoriteStickers: new Map(),
  recentStickerIds: [],
  rendezvousDismissals: new Set(),
  selectedSticker: null,
  actionComposerOpen: false,
  selected: "",
  threads: new Map(),
  worldPackage: "",
  scene: null,
  sceneByCharacter: new Map(),
  dialogueByCharacter: new Map(),
  sceneActionByCharacter: new Map(),
  latest: new Map(),
  typingByCharacter: new Map(),
  presentationByCharacter: new Map(),
  chatRequestByCharacter: new Map(),
  retrySnapshots: new Map(),
  presenceResolvePending: 0,
  deferredPresenceCharacter: "",
  deferredPresenceRunning: false,
  modeTransitionSequence: 0,
  modeTransitionPending: false,
  modeTransitionController: null,
  requestStatusByCharacter: new Map(),
  requestRecoveryByCharacter: new Map(),
  requestStatusOwnerByCharacter: new Map(),
  feedbackMessageId: "",
  arrivalPending: false,
  autoSummaryEnabled: true,
  dayContinuityPreference: "start_today",
  selectionSequence: 0,
  selectionController: null,
  stageArtRequestSequence: 0,
  stageRelease: null,
  stageArtController: null,
  stageArtPending: null,
  stageArtTransition: null,
  stageArtSurface: null,
  stagePresentationCache: new Map(),
  stageRenderSequence: 0,
  stageSpeechGate: null,
  stageWarmController: null,
  expressionManifestCache: new Map(),
  expressionBytesCache: new Map(),
  stageMotionRequestSequence: 0,
  stageMotionAnimation: null,
  stageMotionCharacterId: "",
  stageEnterAnimation: null,
  snowVeilLocation: null,
  snowVeilArmed: false,
  snowVeilSeen: false,
  snowVeilTimers: [],
  snowVeilAnimation: null,
  // 雪境 ADV P6a 开场与通讯器世界层。
  snowOpening: null,        // { phone, kind: 'boot'|'lock'|'none' } 页面加载时判定一次
  snowBeenInPerson: false,  // 本页会话内是否已亲身进过室内（决定首次入室镜头推进）
  snowPushPlayed: false,    // 首次入室镜头推进是否已经播放过
  snowEntryConsumed: false, // 首次入室已展示；light/off 后切回 auto 不补播推进
  snowBootDone: false,      // 开机层是否已消隐
  playedStageMotionKeys: new Set(),
  replyTimingByRequest: new Map(),
  streamingPresentationByCharacter: new Map(),
  completedPresentationByCharacter: new Map(),
  readyReplyIds: new Set(),
  paintedMessageIds: new Set(),
  seenMessageEntries: new WeakSet(),
  typewriter: { key: "", timer: 0, fullText: "", displayedText: "" },
  summaryInFlight: new Set(),
  summaryControllers: new Map(),
  persistedMessages: new Map(),
  historyEpoch: 0,
  storageAvailable: true,
  memoryStores: { threads: new Map(), messages: new Map(), app_state: new Map() },
  drafts: new Map(),
  composerDraftKey: "",
  draftTimer: 0,
  pinnedCharacters: new Set(),
  timelineVisibleLimit: 60,
  timelineEndId: "",
  transcriptVisibleLimit: 200,
  transcriptEndId: "",
  historyPageLoading: false,
  drawerReturnFocus: null,
  contactReturnFocus: null,
  onboardingStep: 0,
  historyRetentionDays: 0,
  writerReady: false,
  writerOwned: true,
  writerId: "",
  writerRelease: null,
  writerChannel: null,
  writerRenewTimer: 0,
};

// A request may outlive the character currently visible in the UI.  Keeping
// the owner fields together makes every render/cleanup decision explicit and
// prevents a late response from borrowing another character's stage.
function TypingIndicatorState({ channel, characterId, requestId, phase = "typing" }) {
  this.channel = channel === "in_person" ? "in_person" : "text";
  this.characterId = plain(characterId);
  this.requestId = plain(requestId);
  this.phase = plain(phase || "typing");
}

let dbPromise = null;
let storageWarningShown = false;

const ANNOUNCEMENT_READ_KEY = "project-snow-public:announcements:read:v1";
const announcements = {
  feed: null, pending: null, error: false, ready: false, tab: "updates",
  read: new Set(), attempted: new Set(), visibleKeys: new Set(), lastFetch: 0, day: "", renderSignature: "",
};

function announcementDay(timestamp = Date.now()) {
  return new Date(timestamp + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}
function announcementDateLabel(day) {
  const [, month, date] = day.split("-").map(Number);
  return `${month}月${date}日`;
}
function announcementUpdateKey(update) {
  return `update:${update.id}:${update.updated_at || update.published_at}`;
}
function readAnnouncementHistory() {
  try {
    const stored = JSON.parse(localPreferences.getItem(ANNOUNCEMENT_READ_KEY) || "[]");
    if (Array.isArray(stored)) stored.filter((key) => typeof key === "string" && key.length < 256).forEach((key) => announcements.read.add(key));
  } catch { /* The in-memory receipt still prevents repeat prompts this visit. */ }
}
function acknowledgeAnnouncements() {
  announcements.visibleKeys.forEach((key) => announcements.read.add(key));
  announcements.visibleKeys.clear();
  try { localPreferences.setItem(ANNOUNCEMENT_READ_KEY, JSON.stringify([...announcements.read].slice(-200))); } catch { /* Retain receipts in memory when storage is unavailable. */ }
  renderAnnouncementBadges();
}
function normalizeAnnouncementFeed(payload) {
  if (payload?.schema_version !== 1 || payload.timezone !== "Asia/Shanghai" || !Array.isArray(payload.updates) || !Array.isArray(payload.birthdays)) throw new Error("invalid_announcement_feed");
  const updates = payload.updates.map((item) => {
    if (!item?.id || typeof item.title !== "string" || !Array.isArray(item.items) || !item.items.every((line) => typeof line === "string") || !Number.isFinite(Date.parse(item.published_at)) || (item.updated_at && !Number.isFinite(Date.parse(item.updated_at)))) throw new Error("invalid_announcement_update");
    return { ...item, id: plain(item.id), summary: plain(item.summary) };
  }).sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at));
  const birthdays = payload.birthdays.map((item) => {
    const date = new Date(Date.UTC(2024, item.month - 1, item.day));
    if (!item?.character_id || typeof item.display_name !== "string" || !Number.isInteger(item.month) || !Number.isInteger(item.day) || date.getUTCMonth() + 1 !== item.month || date.getUTCDate() !== item.day) throw new Error("invalid_announcement_birthday");
    return { ...item, character_id: plain(item.character_id), source_url: safeExternalUrl(item.source_url) };
  });
  if (new Set(updates.map((item) => item.id)).size !== updates.length || new Set(birthdays.map((item) => item.character_id)).size !== birthdays.length) throw new Error("duplicate_announcement_entry");
  return { updates, birthdays };
}
function announcementView() {
  const day = announcementDay();
  const [year, month, date] = day.split("-").map(Number);
  const todayTime = Date.UTC(year, month - 1, date);
  const updates = (announcements.feed?.updates || []).filter((item) => Date.parse(item.published_at) <= Date.now());
  const birthdays = (announcements.feed?.birthdays || []).map((item) => {
    let nextYear = year;
    let nextDate;
    do {
      nextDate = new Date(Date.UTC(nextYear++, item.month - 1, item.day));
    } while (nextDate.getUTCMonth() + 1 !== item.month || nextDate.getUTCDate() !== item.day || nextDate.getTime() < todayTime);
    return { ...item, days: Math.round((nextDate.getTime() - todayTime) / 86400000) };
  }).sort((a, b) => a.days - b.days || a.display_name.localeCompare(b.display_name, "zh-CN"));
  const today = birthdays.filter((item) => item.days === 0);
  const birthdayKeys = today.map((item) => `birthday:${day}:${item.character_id}`);
  const updateKey = updates[0] ? announcementUpdateKey(updates[0]) : "";
  const unread = [updateKey, ...birthdayKeys].filter((key) => key && !announcements.read.has(key));
  return { day, updates, birthdays, today, birthdayKeys, updateKey, unread };
}
function renderAnnouncementBadges(view = announcementView()) {
  document.querySelectorAll(".announcement-entry").forEach((button) => {
    button.querySelector(".announcement-unread-dot").hidden = !view.unread.length;
    button.setAttribute("aria-label", view.unread.length ? `公告与生日，有 ${view.unread.length} 条新消息` : "公告与生日");
  });
  $("announcement-update-badge").hidden = !view.updateKey || announcements.read.has(view.updateKey);
  $("announcement-birthday-badge").hidden = !view.today.length;
  $("announcement-birthday-badge").textContent = String(view.today.length);
}
function birthdayRow(item, { today = false, calendar = false } = {}) {
  const name = escapeHtml(item.display_name);
  const source = item.source_url ? `<a class="birthday-source" href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer" aria-label="查看${name}的生日资料">资料 ↗</a>` : "";
  const countdown = today ? "生日快乐" : item.days === 1 ? "明天" : `还有 ${item.days} 天`;
  return `<article class="birthday-row${today ? " birthday-today" : ""}" data-birthday-character="${escapeHtml(item.character_id)}"><span class="birthday-monogram" aria-hidden="true">${escapeHtml(Array.from(item.display_name)[0])}</span><div class="birthday-row-copy"><strong>${name}</strong><span>${item.month}月${item.day}日${calendar ? "" : ` · ${countdown}`}</span></div>${source}${calendar ? `<span class="birthday-countdown">${countdown}</span>` : ""}</article>`;
}
function announcementRenderSignature(view = announcementView()) {
  return JSON.stringify([announcements.feed, view.day, view.updateKey, announcements.error]);
}
function renderAnnouncementStatus() {
  const loading = Boolean(announcements.pending);
  $("announcement-status").hidden = Boolean(announcements.feed) && !announcements.error;
  $("announcement-status-text").textContent = loading ? "正在读取公告…" : !announcements.error ? "" : announcements.feed ? "暂时无法更新公告，下面显示已加载的内容。" : "公告暂时无法加载，请稍后重试。";
  $("retry-announcements").hidden = loading || !announcements.error;
}
function renderAnnouncements() {
  const view = announcementView();
  announcements.day = view.day;
  announcements.renderSignature = announcementRenderSignature(view);
  renderAnnouncementBadges(view);
  $("announcements-date").textContent = `${view.day.slice(0, 4)}年${announcementDateLabel(view.day)} · 北京时间`;
  renderAnnouncementStatus();
  document.querySelectorAll("[data-announcement-tab]").forEach((button) => {
    const selected = button.dataset.announcementTab === announcements.tab;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  $("announcement-panel-updates").hidden = !announcements.feed || announcements.tab !== "updates";
  $("announcement-panel-birthdays").hidden = !announcements.feed || announcements.tab !== "birthdays";
  $("announcement-birthday-highlight").innerHTML = view.today.length ? `<button class="birthday-highlight" type="button"><span class="birthday-highlight-mark" aria-hidden="true">✧</span><span><strong>今天是${view.today.map((item) => escapeHtml(item.display_name)).join("、")}的生日</strong><small>留一份生日祝福</small></span><span aria-hidden="true">↗</span></button>` : "";
  $("announcement-birthday-highlight").querySelector("button")?.addEventListener("click", () => selectAnnouncementTab("birthdays", true));
  $("announcement-updates").innerHTML = view.updates.length ? view.updates.map((update, index) => `<article class="announcement-update${index === 0 ? " is-latest" : ""}"><div class="announcement-update-meta"><span>${index === 0 ? "最新更新" : "过往更新"}</span><time datetime="${escapeHtml(update.published_at)}">${announcementDateLabel(announcementDay(Date.parse(update.published_at)))}</time></div><h3>${escapeHtml(update.title)}</h3><p>${escapeHtml(update.summary)}</p><ul>${update.items.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul></article>`).join("") : '<p class="announcement-empty">暂时没有站点更新。新的公告会出现在这里。</p>';
  $("announcement-birthdays-today").innerHTML = view.today.length ? `<div class="birthday-section-heading"><span>今天的寿星</span><strong>${announcementDateLabel(view.day)}</strong></div>${view.today.map((item) => birthdayRow(item, { today: true })).join("")}` : '<div class="birthday-quiet"><span aria-hidden="true">✧</span><p>今天没有角色生日</p><small>下一个特别的日子，也值得期待。</small></div>';
  $("announcement-birthdays-upcoming").innerHTML = view.birthdays.filter((item) => item.days > 0).slice(0, 3).map((item) => birthdayRow(item)).join("") || '<p class="announcement-empty">暂无即将到来的生日。</p>';
  $("birthday-calendar-label").textContent = `查看全部 ${view.birthdays.length} 位角色的生日`;
  $("announcement-birthday-calendar").innerHTML = [...view.birthdays].sort((a, b) => a.month - b.month || a.day - b.day).map((item) => birthdayRow(item, { today: item.days === 0, calendar: true })).join("");
  if ($("announcements-dialog").open) {
    view.birthdayKeys.forEach((key) => announcements.visibleKeys.add(key));
    if (announcements.tab === "updates" && view.updateKey) announcements.visibleKeys.add(view.updateKey);
  }
}
function selectAnnouncementTab(tab, focus = false) {
  announcements.tab = tab === "birthdays" ? "birthdays" : "updates";
  renderAnnouncements();
  if (focus) $(`announcement-tab-${announcements.tab}`).focus();
}
function openAnnouncements({ automatic = false } = {}) {
  if ($("announcements-dialog").open) return;
  const view = announcementView();
  announcements.tab = automatic && (!view.updateKey || announcements.read.has(view.updateKey)) && view.today.length ? "birthdays" : "updates";
  announcements.visibleKeys.clear();
  view.unread.forEach((key) => announcements.attempted.add(key));
  $("announcements-dialog").showModal();
  renderAnnouncements();
  if (!announcements.feed && !announcements.pending) void refreshAnnouncements();
}
function maybeShowAnnouncements() {
  if (!announcements.ready || !announcements.feed || document.hidden || document.querySelector("dialog[open]") || globalRequestBusy()) return;
  // P6a：开机/锁屏层占屏时不自动弹公告（<dialog> 走顶层会盖在开场层之上）。
  if (snowOpeningActive()) return;
  if (document.activeElement?.matches("input, textarea") && document.activeElement.value) return;
  if (announcementView().unread.some((key) => !announcements.attempted.has(key))) openAnnouncements({ automatic: true });
}
async function refreshAnnouncements() {
  if (announcements.pending) return announcements.pending;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  announcements.error = false;
  announcements.pending = (async () => {
    try {
      const response = await fetch("/announcements.json", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("announcement_fetch_failed");
      announcements.feed = normalizeAnnouncementFeed(await response.json());
    } catch { announcements.error = true; }
    finally {
      window.clearTimeout(timeout);
      announcements.pending = null;
      announcements.lastFetch = Date.now();
      // An unchanged background refresh must preserve keyboard focus and
      // the reader's place inside the open announcement or birthday list.
      if (announcementRenderSignature() !== announcements.renderSignature) renderAnnouncements();
      else { renderAnnouncementStatus(); renderAnnouncementBadges(); }
      maybeShowAnnouncements();
      // P6a：锁屏仍在时，公告就绪后补上「未读公告」通知卡。
      snowRefreshLockNotes();
    }
  })();
  renderAnnouncementStatus();
  return announcements.pending;
}
function announcementTick() {
  if (!announcements.ready || document.hidden) return;
  if (announcements.day !== announcementDay()) renderAnnouncements();
  renderAnnouncementBadges();
  maybeShowAnnouncements();
  if (Date.now() - announcements.lastFetch >= 5 * 60 * 1000) void refreshAnnouncements();
}
function startAnnouncements() {
  readAnnouncementHistory();
  announcements.ready = true;
  void refreshAnnouncements();
  window.setInterval(announcementTick, 30000);
}

function useMemoryStorage() {
  state.storageAvailable = false;
  dbPromise = Promise.resolve(null);
  if (!storageWarningShown) {
    storageWarningShown = true;
    showBanner("浏览器本地存储不可用，本次聊天不会保存；仍可继续使用。");
  }
}

const $ = (id) => document.getElementById(id);
const errorMessages = {
  invalid_request: "请求内容不完整或格式不正确。",
  provider_not_enabled: "该模型厂商尚未启用，请选择其他厂商。",
  provider_credential_rejected: "API Key 被模型厂商拒绝，请检查密钥、余额和模型权限。",
  provider_model_discovery_failed: "无法获取模型列表；你仍可手工填写正确的模型 ID。",
  provider_network_error: "暂时无法连接模型厂商，请稍后重试。",
  provider_timeout: "模型厂商响应超时，请稍后重试。",
  provider_rate_limited: "模型厂商触发了频率限制，请稍后重试。",
  provider_request_failed: "模型厂商拒绝了本次请求，请检查模型 ID 和账户权限。",
  subscription_disabled: "本站尚未启用订阅连接。你仍可阅读教程，或使用 API Key。",
  subscription_not_connected: "个人连接器尚未连接或已离线。请打开模型设置，重新配对你自己的 Codex 订阅。",
  subscription_disconnected: "个人连接器已断开，请在模型设置中重新配对。",
  subscription_pairing_expired: "配对码已过期，请重新生成。",
  subscription_model_unavailable: "当前账号或连接器不支持所选模型与推理强度，请重新选择；不会自动替换为其他模型。",
  subscription_effort_unavailable: "当前账号不支持所选推理强度，请在模型设置中重新选择强度并保存。",
  subscription_result_unknown: "请求已经交给个人连接器，但结果尚未确认。请检查连接器状态，避免重复发送这条消息。",
  subscription_login_required: "请在自己的电脑上运行 codex login，使用有订阅的 ChatGPT 账号登录，再重新连接。",
  subscription_rate_limited: "你的 Codex 订阅目前已触发额度或频率限制，请查看自己的账号用量并稍后再试。",
  subscription_generation_failed: "个人连接器未能生成回复，请查看本机连接器提示。",
  subscription_protocol_error: "个人连接器版本不兼容，请下载本站提供的连接器并重新配对。",
  subscription_connector_stopped: "个人连接器已停止，请在本机重新启动并检查连接状态。",
  subscription_capacity_reached: "本站当前连接数已满，请稍后再配对。",
  subscription_busy: "个人连接器仍在处理上一条请求，请等待结果，避免重复发送。",
  subscription_request_too_large: "本轮对话过长，请开始新的连续性段后再试。",
  character_unavailable: "当前角色数据尚未正确发布，请稍后重试。",
  public_database_unavailable: "服务端数据库暂时不可用，请稍后重试。",
  generation_queue_full: "当前生成请求较多，请稍后重试。",
  generation_queue_timeout: "等待生成超时，请稍后重试。",
  upstream_invalid_response: "模型没有返回可用正文，本次不会写入角色回复，请重试。",
  role_guard_rejected: "本次生成未通过角色校验，请重试。",
  rate_limit_exceeded: "当前额度已用完，位置切换已保留，但没有生成到场对白。",
  credential_invalid: "模型会话已失效，请重新输入 API Key。",
  request_in_progress: "同一请求仍在处理中，请稍后重试。",
  request_id_conflict: "请求编号冲突，请重新操作。",
  invalid_presence_transition: "场景转换参数不正确。",
  invalid_presence_request: "当前场景请求无效。",
  request_result_unavailable: "原请求的恢复窗口已结束，无法找回结果。不会自动重新生成，请确认后发送新消息。",
  session_context_invalid: "会话凭证已失效。历史仍保留，请从今天重新开始场景。",
  session_context_subject_mismatch: "当前会话凭证不属于此浏览器会话。历史仍保留，请重新建立场景。",
  session_context_conflict: "场景已更新，请重新读取当前场景后继续。",
  invalid_scene_action: "这个约定已变更，请重新查看当前约定。",
  invalid_dialogue_context: "这次邀请需要澄清，请确认地点和时间后再发送。",
  turnstile_required: "人机验证未完成，请稍后重试。",
  turnstile_expired: "人机验证已过期，请重新验证。",
  turnstile_timeout: "人机验证超时，请再次点击按钮重试。",
  turnstile_unavailable: "人机验证组件加载失败，请刷新页面后重试。",
  chat_failed: "对话请求失败，请稍后重试。",
  request_failed: "请求失败，请稍后重试。",
  request_timeout: "提交超时，反馈未能确认是否送达；请稍后重试。",
  request_too_large: "本轮上下文过长，已无法在安全请求上限内发送；请先整理摘要或开始新的连续性段。",
  request_cancelled: "已停止等待。模型供应商仍可能完成本次生成或产生费用。",
  stream_disconnected: "连接中断，未能恢复本次回复。你可以使用原消息重新发起请求。",
  experience_notice_required: "请先阅读并确认体验说明。",
  sticker_unavailable: "这个表情暂时不可用，请换一个或稍后再试。",
  state_subject_mismatch: "本地场景凭证仍然无效，请重新发送本条消息。",
  state_invalid: "本地场景状态仍然无效，请重新发送本条消息。",
};

const PUBLIC_ERROR_STAGES = new Set([
  "content_validation",
  "state_validation",
  "character_data",
  "generation_queue",
  "retrieval",
  "provider",
  "generation_validation",
  "idempotency_store",
  "transport",
  "unknown",
]);
const errorStageMessages = {
  content_validation: "请求内容无法通过校验，请检查动作和对白后重试。",
  state_validation: "本地场景状态已变化，请重新发送本条消息。",
  character_data: "当前角色资料暂时不可用，请稍后重试。",
  generation_queue: "当前生成请求较多，请稍后重试。",
  retrieval: "角色资料暂时无法读取，请稍后重试。",
  provider: "模型厂商暂时无法响应，请稍后重试。",
  generation_validation: "角色回复未通过校验，请稍后重试。",
  idempotency_store: "请求状态暂时无法保存，请稍后重试。",
  transport: "连接暂时中断，请稍后重试。",
  unknown: "请求失败，请稍后重试。",
};
const errorStageByCode = {
  invalid_request: "content_validation",
  sticker_unavailable: "content_validation",
  invalid_movement_location: "content_validation",
  invalid_presence_request: "content_validation",
  request_too_large: "content_validation",
  request_read_timeout: "transport",
  request_timeout: "transport",
  request_failed: "transport",
  stream_disconnected: "transport",
  service_draining: "generation_queue",
  subject_generation_busy: "generation_queue",
  generation_queue_full: "generation_queue",
  generation_queue_timeout: "generation_queue",
  generation_interrupted: "unknown",
  generation_failed: "unknown",
  provider_not_enabled: "provider",
  provider_credential_rejected: "provider",
  provider_model_discovery_failed: "provider",
  provider_network_error: "provider",
  provider_timeout: "provider",
  provider_rate_limited: "provider",
  provider_request_failed: "provider",
  upstream_invalid_response: "generation_validation",
  role_guard_rejected: "generation_validation",
  public_database_unavailable: "idempotency_store",
  request_id_conflict: "idempotency_store",
  request_in_progress: "idempotency_store",
  state_invalid: "state_validation",
  state_subject_mismatch: "state_validation",
  invalid_presence_transition: "state_validation",
  character_unavailable: "character_data",
};

const STATE_PACKAGE_RECOVERY_CODES = new Set(["state_subject_mismatch", "state_invalid"]);
const MIA_CHARACTER_ID = "702f4375675b";
const MIA_BUNDLED_EXPRESSION_MANIFEST = "/assets/expressions/mia/manifest.json";
// The same approved bundled JSON in Git/LF and a Windows/CRLF checkout.
// Advertised release manifests always use their one exact catalogue digest.
const MIA_BUNDLED_EXPRESSION_MANIFEST_HASHES = Object.freeze([
  "89bb5eac7185dc24bb3af84d1f97200fdbe6037d4f7d97111866c8447d13c1aa",
  "2593b7b1f8a519b7eae7189fd1779099afc7a9e4f220d5f7dad1653d88c29023",
]);
const EXPRESSION_FETCH_TIMEOUT_MS = 12000;
const EXPRESSION_DECODE_TIMEOUT_MS = 8000;
const EXPRESSION_UPDATE_TIMEOUT_MS = 20000;
const EXPRESSION_IMAGE_MAX_BYTES = 64 * 1024 * 1024;
const STAGE_PRESENTATION_CACHE_LIMIT = 12;
const STAGE_SPEECH_SYNC_WAIT_MS = 240;
const EXPRESSION_STATES = new Set([
  "neutral", "gentle_smile", "happy", "amused", "teasing", "relieved",
  "serious", "focused", "thinking", "confused", "skeptical", "concerned",
  "surprised", "embarrassed", "sad", "disappointed", "annoyed", "angry",
]);
const STAGE_MOTIONS = new Set(["none", "lean_in", "tremble", "recoil", "startle"]);
const ACTIVE_STAGE_RENDER_STRATEGIES = new Set(["single_sprite", "layered_sprite"]);

function normalizeExpressionState(value, communicationChannel = "text") {
  if (communicationChannel !== "in_person" || typeof value !== "string") return "neutral";
  const normalized = value.trim();
  return EXPRESSION_STATES.has(normalized) ? normalized : "neutral";
}

function normalizeStageMotion(value, communicationChannel = "text") {
  if (communicationChannel !== "in_person" || typeof value !== "string") return "none";
  const normalized = value.trim();
  return STAGE_MOTIONS.has(normalized) ? normalized : "none";
}

function normalizePerformanceId(value, communicationChannel = "text") {
  if (communicationChannel !== "in_person" || typeof value !== "string") return "";
  const cue = value.trim();
  return /^[a-z][a-z0-9_]{0,79}$/.test(cue) ? cue : "";
}

function id() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function plain(value) { return String(value ?? ""); }
function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = plain(value);
  return span.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function normalizedErrorStage(value) {
  const candidate = plain(value).trim();
  return PUBLIC_ERROR_STAGES.has(candidate) ? candidate : "";
}
function _errorStageForClientCode(code) {
  return errorStageByCode[plain(code)] || "unknown";
}
function chatError(code = "request_failed", detail = {}, status = 0) {
  const error = new Error(plain(code) || "request_failed");
  const payload = detail && typeof detail === "object" ? detail : {};
  error.requestId = plain(payload.request_id);
  error.errorStage = normalizedErrorStage(payload.stage);
  error.retryable = typeof payload.retryable === "boolean" ? payload.retryable : null;
  const retryAfter = Number(payload.retry_after_seconds);
  error.retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter > 0
    ? Math.min(300, Math.floor(retryAfter))
    : 0;
  error.httpStatus = Number(status) || 0;
  return error;
}
function displayError(error) {
  const code = error instanceof Error ? error.message : plain(error);
  if (code === "credential_invalid" && state.provider === SUBSCRIPTION_PROVIDER) return errorMessages.subscription_not_connected;
  const stage = normalizedErrorStage(error instanceof Error ? error.errorStage : "");
  if (stage && ["request_failed", "chat_failed", "generation_failed"].includes(code)) {
    return errorStageMessages[stage] || errorMessages.request_failed;
  }
  return errorMessages[code] || (/^[a-z][a-z0-9_]*$/.test(code) ? errorMessages.request_failed : code);
}

const PUBLIC_REQUEST_LIMIT_BYTES = 64 * 1024;
const PUBLIC_REQUEST_TARGET_BYTES = 63 * 1024;
const requestTextEncoder = new TextEncoder();
function jsonBodyBytes(payload) {
  return requestTextEncoder.encode(JSON.stringify(payload)).byteLength;
}
function safePrefix(value, length) {
  let result = plain(value).slice(0, Math.max(0, length));
  const finalCodeUnit = result ? result.charCodeAt(result.length - 1) : 0;
  if (finalCodeUnit >= 0xD800 && finalCodeUnit <= 0xDBFF) result = result.slice(0, -1);
  return result;
}
function fitPublicRequestPayload(payload, { arrays = [], texts = [], targetBytes = PUBLIC_REQUEST_TARGET_BYTES } = {}) {
  const fitted = structuredClone(payload);
  const target = Math.min(PUBLIC_REQUEST_LIMIT_BYTES, Math.max(1024, Number(targetBytes) || PUBLIC_REQUEST_TARGET_BYTES));
  for (const { key, minimum = 0 } of arrays) {
    const values = Array.isArray(fitted[key]) ? fitted[key] : [];
    fitted[key] = values;
    while (values.length > minimum && jsonBodyBytes(fitted) > target) values.shift();
  }
  for (const key of texts) {
    if (jsonBodyBytes(fitted) <= target) break;
    const original = plain(fitted[key]);
    let low = 0;
    let high = original.length;
    let best = "";
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const candidate = safePrefix(original, middle);
      fitted[key] = candidate;
      if (jsonBodyBytes(fitted) <= target) {
        best = candidate;
        low = middle + 1;
      } else high = middle - 1;
    }
    fitted[key] = best;
  }
  if (jsonBodyBytes(fitted) > target) throw new Error("request_too_large");
  return fitted;
}
if (["127.0.0.1", "localhost", "[::1]"].includes(window.location.hostname)) {
  Object.defineProperty(window, "__projectSnowTest", {
    value: Object.freeze({ escapeHtml, fitPublicRequestPayload, deriveDisplayBlocks, statePackageOrder, expressionStateForMessage, normalizeExpressionState, normalizeStageMotion, expressionManifestUrlForCharacter, normalizeExpressionManifest, stagePresentationRenderer, preloadStagePresentation, clearStagePresentationCache, warmCharacterStage, updateStageCharacterArt, playStageMotion, cancelStageMotion, renderStage,
      // P6a 探针：读取开场/世界层状态，并可强制切换世界场景以单测镜头推进/veil。
      snowProbe: () => ({ opening: state.snowOpening, beenInPerson: state.snowBeenInPerson, pushPlayed: state.snowPushPlayed, bootDone: state.snowBootDone,
        worldScene: snowWorld ? snowWorld.scene : null,
        backdropScene: snowBackdrop ? snowBackdrop.scene : null,
        pushing: snowBackdrop ? snowBackdrop.isPushing() : false,
        veilArmed: state.snowVeilArmed,
        activeLoops: activeSnowLoops() }),
      snowRenderSceneWith: (sceneState) => { state.scene = sceneState; renderScene(); },
      snowSetBeenInPerson: (value) => { state.snowBeenInPerson = Boolean(value); } }),
    configurable: false,
    writable: false,
  });
}
function showError(target, error) { $(target).textContent = error ? displayError(error) : ""; }
function toast(message) {
  const root = $("toast");
  root.textContent = message;
  root.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { root.hidden = true; }, 3200);
}
function showBanner(message) {
  const root = $("system-banner");
  root.textContent = message;
  root.hidden = !message;
  if (message) window.setTimeout(() => { if (root.textContent === message) root.hidden = true; }, 7000);
}
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}
function delay(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }
function abortableDelay(milliseconds, signal) {
  if (!milliseconds || signal?.aborted) {
    return signal?.aborted ? Promise.reject(new DOMException("Aborted", "AbortError")) : Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(finish, milliseconds);
    function finish() {
      signal?.removeEventListener("abort", cancel);
      resolve();
    }
    function cancel() {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal?.addEventListener("abort", cancel, { once: true });
  });
}
function reducedMotion() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches || localPreferences.getItem("project-snow-public:motion") === "off"; }
function featureEnabled(name) { return state.config?.features?.[name] === true; }
function quietPresentation() { return reducedMotion() || document.hidden; }
// 雪境 ADV 演出 (P4)：是否处于雪境皮肤。以 <html data-ui> 为唯一真源（syncInterfaceSkin
// 设置、CSS 与动态档位都读它），这样测试只需切 data-ui 即可驱动，无需等 config。
function snowAdvActive() { return document.documentElement.dataset.ui === "snow-adv"; }
// P4 立绘/动作档位：auto=完整（位移+缩放+呼吸）、light=仅淡入淡出、off=无动画（含减少动态）。
function stageMotionTier() {
  if (reducedMotion()) return "off";
  return document.documentElement.dataset.motionLevel === "light" ? "light"
    : document.documentElement.dataset.motionLevel === "off" ? "off" : "auto";
}
const surfaceMotion = new MotionScope();
const transientMotion = new MotionScope();
const completedReplyTimings = [];
function anonymousTiming(timing) {
  const duration = value => Number.isFinite(value) ? Math.max(0, Math.round(value)) : null;
  const serverTimings = anonymousServerTimings(timing.serverTimings);
  return { send_to_first_effective_ms: timing.firstEffectiveAt ? duration(timing.firstEffectiveAt - timing.sentAt) : null,
    ready_to_paint_ms: timing.firstPaintAt && timing.bodyReceivedAt ? duration(timing.firstPaintAt - timing.bodyReceivedAt - (timing.firstNaturalWaitMs || 0)) : null,
    natural_wait_ms: duration(timing.naturalWaitMs), complete_ms: duration(timing.completedAt - timing.sentAt),
    outcome: timing.outcome, recovery: Boolean(timing.recovery), channel: timing.channel, history_band: timing.historyBand, provider: timing.provider, model_profile: timing.modelProfile,
    server_queue_ms: duration(serverTimings.queue_ms), rewrite_ms: duration(serverTimings.guard_rewrite_ms),
    server_timings_ms: serverTimings };
}
Object.defineProperty(window, "__projectSnowPerformance", { value: Object.freeze({ getSamples: () => completedReplyTimings.map(anonymousTiming) }) });
function completeReplyTiming(timing, outcome) {
  if (timing.completedAt) return;
  timing.completedAt = performance.now(); timing.outcome = outcome;
  completedReplyTimings.push(timing);
  if (completedReplyTimings.length > 100) completedReplyTimings.shift();
  const emit = () => document.dispatchEvent(new CustomEvent("project-snow:reply-timing", { detail: anonymousTiming(timing) }));
  if (document.hidden) emit(); else requestAnimationFrame(() => requestAnimationFrame(emit));
}
function syncMotionState() {
  document.documentElement.dataset.motion = featureEnabled("motion_v2") ? "v2" : "legacy";
  document.documentElement.dataset.pageHidden = String(document.hidden);
  const preference = localPreferences.getItem("project-snow-public:motion") || "auto";
  const previousLevel = document.documentElement.dataset.motionLevel;
  document.documentElement.dataset.motionLevel = reducedMotion() ? "off" : preference;
  if (previousLevel !== document.documentElement.dataset.motionLevel || document.hidden) cancelSnowScenePresentation();
  if (snowBackdrop) snowBackdrop.setMotion(document.documentElement.dataset.motionLevel);
  if (snowWorld) snowWorld.setMotion(document.documentElement.dataset.motionLevel);
  for (const opening of document.querySelectorAll("#snow-boot, #snow-lock")) opening.dataset.tier = stageMotionTier();
  if ($("motion-preference")) $("motion-preference").value = preference;
  if ($("motion-preference-setting")) $("motion-preference-setting").hidden = !featureEnabled("motion_v2");
}
function replyTiming(requestId) { return state.replyTimingByRequest.get(requestId); }
// 雪境 ADV skin (P1).  Off unless the server reports features.ui_snow_adv.
// Turning it on only adds html[data-ui="snow-adv"], links the story font and
// swaps the static Unicode control glyphs for SVG icons; turning it off
// restores the original glyphs, so the legacy interface is untouched.
const SNOW_ADV_FONT_STYLESHEET = "/snow-adv-fonts.css";
const SNOW_ADV_STATIC_ICONS = Object.freeze([
  [".contact-search > span[aria-hidden]", "search"],
  ["#open-settings", "settings"],
  ["#open-contacts", "menu"],
  ["#stage-menu > summary", "menu"],
  ["#open-info", "info"],
  [".announcement-entry > span[aria-hidden]:first-child", "bell"],
  ["#toggle-stage-ui", "eye-off"],
  ["#toggle-sticker .composer-tool-icon", "smile"],
  ["#toggle-action .composer-tool-icon", "sparkles"],
  ["#open-movement-shortcuts .composer-tool-icon", "footprints"],
  ["#send-message .composer-tool-icon", "send-horizontal"],
]);
function snowAdvIconTargets() {
  const targets = [];
  for (const [selector, icon] of SNOW_ADV_STATIC_ICONS) document.querySelectorAll(selector).forEach(node => targets.push([node, icon]));
  document.querySelectorAll("button.icon-button").forEach(node => {
    if (node.dataset.snowGlyph === "×" || (!node.dataset.snowGlyph && node.textContent.trim() === "×")) targets.push([node, "x"]);
  });
  return targets;
}
function applySnowAdvIcons(enabled) {
  for (const [node, icon] of snowAdvIconTargets()) {
    if (enabled && !node.dataset.snowGlyph) {
      const glyph = node.textContent.trim();
      // Only a node holding a single glyph is replaced; labelled text stays.
      if (!glyph || glyph.length > 2 || node.children.length) continue;
      node.dataset.snowGlyph = glyph;
      node.replaceChildren(createSnowIcon(icon));
    } else if (!enabled && node.dataset.snowGlyph) {
      node.textContent = node.dataset.snowGlyph;
      delete node.dataset.snowGlyph;
    }
  }
}
function loadSnowAdvFonts() {
  if (document.querySelector("link[data-snow-adv-fonts]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = SNOW_ADV_FONT_STYLESHEET;
  link.dataset.snowAdvFonts = "";
  document.head.append(link);
}
function syncInterfaceSkin() {
  const enabled = featureEnabled("ui_snow_adv");
  if (enabled) document.documentElement.dataset.ui = "snow-adv";
  else delete document.documentElement.dataset.ui;
  if (enabled) loadSnowAdvFonts();
  applySnowAdvIcons(enabled);
  syncSnowWorld();
  syncStageQuickMenuMount();
}
// 雪境 ADV 世界层 (P2)：开关开启且处于面对面界面时，在 #scene-stage 内挂载程序化
// 分层雪景与 Canvas 远近雪；关闭或离开面对面时销毁。立绘仍留在原 DOM 位置。
let snowWorld = null;
// P6a：通讯器背后的世界——一个 body 级 fixed 的「舞台」容器，仅桌面开关开启时存在，
// aria-hidden、pointer-events:none、z-index 低于 #chat-app；文字界面可见时作为雪境背景，
// 面对面可见时暂停（只让一个世界在动）。手机与关闭开关时都不建。
let snowBackdrop = null;
let snowBackdropStage = null;
let snowPushContext = null;
let snowPresentationSequence = 0;
function snowWorldScene() {
  const scene = state.scene || {};
  return SCENE_KEYS.has(scene.visual_key) ? scene.visual_key : "generic";
}
function currentMotionLevel() {
  return document.documentElement.dataset.motionLevel || "auto";
}
// 背后世界应显示的场景：首次入室镜头推进播放前恒为 generic 外景（推进从外景推入）；
// 推进播放后（snowPushPlayed）跟随当前室内地点。关闭推进（light/off）时入室即跟随室内。
function snowBackdropScene() {
  if (!state.snowBeenInPerson) return "generic";
  if (state.snowEntryConsumed || state.snowPushPlayed || stageMotionTier() !== "auto") return snowWorldScene();
  return "generic";
}
function syncSnowWorld() {
  syncSnowBackdrop();
  // P2 舞台世界：仅面对面可见时挂载在 #scene-stage（保持原状，不受通讯器背后世界影响）。
  const stage = $("scene-stage");
  const wanted = featureEnabled("ui_snow_adv") && stage && !$("in-person-surface").hidden;
  if (wanted && !snowWorld) {
    snowWorld = mountSnowWorld(stage, { visualKey: snowWorldScene(), motionLevel: currentMotionLevel() });
    stage.snowWorld = snowWorld;
  } else if (!wanted && snowWorld) {
    snowWorld.destroy();
    snowWorld = null;
    if (stage) delete stage.snowWorld;
  }
}
// 通讯器背后世界的建/拆与可见性。
function syncSnowBackdrop() {
  const want = featureEnabled("ui_snow_adv") && !snowOpeningState().phone;
  const app = $("chat-app");
  if (want && !snowBackdrop) {
    const host = document.createElement("div");
    host.className = "snow-backdrop";
    host.id = "snow-backdrop";
    host.setAttribute("aria-hidden", "true");
    const stageEl = document.createElement("div");
    stageEl.className = "snow-backdrop-stage";
    stageEl.id = "snow-backdrop-stage";
    host.append(stageEl);
    document.body.prepend(host);  // body 级，位于 #chat-app 之前
    snowBackdropStage = stageEl;
    snowBackdrop = mountSnowWorld(stageEl, { visualKey: snowBackdropScene(), motionLevel: currentMotionLevel(), role: "backdrop" });
    stageEl.snowWorld = snowBackdrop;  // 证据/测试读取句柄
  } else if (!want && snowBackdrop) {
    snowBackdrop.destroy();
    snowBackdrop = null;
    snowBackdropStage = null;
    document.getElementById("snow-backdrop")?.remove();
  }
  if (snowBackdrop) {
    // 背后世界场景跟随「是否已入室」；推进进行中不要打断（pushInto 自行切场景）。
    if (!snowBackdrop.isPushing()) snowBackdrop.setScene(snowBackdropScene());
    snowBackdrop.setMotion(currentMotionLevel());  // 内部按 role 在面对面可见时暂停
  }
  if (app) {
    if (snowBackdrop) app.dataset.snowWorld = state.snowBeenInPerson ? "interior" : "exterior";
    else delete app.dataset.snowWorld;
  }
}

// ---------------------------------------------------------------- P3 面对面主屏
// 全部只在 ui_snow_adv 开启时生效。▼ 推进标记与快捷菜单由 JS 在开关开启时建到对白
// 框里、关闭时移除，所以关闭态不新增任何 DOM、图标或可见元素（旧界面逐字节不变）。

// 到场提示去重：开关开启时，仅保留舞台通知（旁白 / 对白），抑制重复的横幅与状态行；
// 屏幕阅读播报仍只发生一次（来自 #stage-narration / #stage-speech 的 aria-live）。
function suppressArrivalDuplicateNotice() { return featureEnabled("ui_snow_adv"); }

// 立绘中轴：在缩小的同源副本上取头到腰（高度 12%~55%）的不透明像素重心，返回 0~1。
// 每个角色只测一次（按角色 id 缓存），换表情沿用缓存值，避免人物左右跳动。
// 画布被污染、读不到像素或任何错误都退回 0.5。
const stageActorAnchorCache = new Map();
function applyStageActorAnchor(node) {
  if (!node || !featureEnabled("ui_snow_adv")) {
    if (node) { node.style.removeProperty("--actor-anchor-x"); delete node.dataset.actorAnchor; }
    return;
  }
  const characterId = plain(node.dataset.expressionCharacterId);
  const src = node.getAttribute("src") || "";
  if (!characterId || !src || node.hidden) return;
  const cached = stageActorAnchorCache.get(characterId);
  if (cached !== undefined) {
    node.style.setProperty("--actor-anchor-x", cached.toFixed(3));
    node.dataset.actorAnchor = cached.toFixed(3);
    return;
  }
  // 占位：先用 0.5，测出后再更新，避免首帧跳动。
  node.style.setProperty("--actor-anchor-x", "0.5");
  node.dataset.actorAnchor = "0.5";
  measureStageActorAnchor(src).then((anchor) => {
    stageActorAnchorCache.set(characterId, anchor);
    const current = $("stage-character-art");
    if (current && plain(current.dataset.expressionCharacterId) === characterId && !current.hidden) {
      current.style.setProperty("--actor-anchor-x", anchor.toFixed(3));
      current.dataset.actorAnchor = anchor.toFixed(3);
    }
  });
}
function measureStageActorAnchor(src) {
  return new Promise((resolve) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => {
      try {
        const width = 124;
        const height = Math.max(1, Math.round(width * image.naturalHeight / Math.max(1, image.naturalWidth)));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) return resolve(0.5);
        context.drawImage(image, 0, 0, width, height);
        const top = Math.round(height * 0.12);
        const band = Math.max(1, Math.round(height * 0.55) - top);
        const data = context.getImageData(0, top, width, band).data;
        let total = 0;
        let weighted = 0;
        for (let index = 3; index < data.length; index += 4) {
          const alpha = data[index];
          if (alpha > 128) {
            const x = ((index - 3) / 4) % width;
            total += alpha;
            weighted += alpha * (x + 0.5);
          }
        }
        resolve(total ? Math.min(0.7, Math.max(0.3, weighted / total / width)) : 0.5);
      } catch { resolve(0.5); } // 画布被污染（跨源）或任何错误都退回居中
    };
    image.onerror = () => resolve(0.5);
    image.src = src;
  });
}

// 快捷菜单（跳过 / 回看 / 隐藏 / 设置）与 ▼ 推进标记：开关开启且处于面对面时建一次。
const STAGE_QUICK_ACTIONS = Object.freeze([
  { key: "skip", icon: "fast-forward", label: "跳过", title: "立即显示（跳过逐字揭示）" },
  { key: "backlog", icon: "history", label: "回看", title: "回看对话" },
  { key: "hide", icon: "eye-off", label: "隐藏", title: "隐藏界面（H）" },
  { key: "settings", icon: "settings", label: "设置", title: "配置模型与设置" },
]);
function stageQuickAction(key) {
  // Delegate to the existing controls so behaviour stays identical to the legacy UI.
  if (key === "skip") { finishReceivedPresentation(); return; }
  if (key === "backlog") { $("open-transcript").click(); return; }
  if (key === "hide") { $("toggle-stage-ui").click(); return; }
  if (key === "settings") { $("stage-open-settings").click(); return; }
}
function buildStageQuickMenu() {
  const dialogue = $("stage-dialogue");
  if (!dialogue || dialogue.querySelector(".stage-quick-menu")) return;
  const advance = document.createElement("span");
  advance.className = "stage-advance";
  advance.id = "stage-advance";
  advance.setAttribute("aria-hidden", "true");
  advance.append(createSnowIcon("chevron-down"));
  const menu = document.createElement("nav");
  menu.className = "stage-quick-menu";
  menu.id = "stage-quick-menu";
  menu.setAttribute("aria-label", "快捷菜单");
  for (const action of STAGE_QUICK_ACTIONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.stageQuick = action.key;
    button.setAttribute("aria-label", action.label);
    button.title = action.title;
    button.append(createSnowIcon(action.icon));
    const label = document.createElement("span");
    label.className = "stage-quick-label";
    label.textContent = action.label;
    button.append(label);
    button.addEventListener("click", () => stageQuickAction(action.key));
    menu.append(button);
  }
  dialogue.append(advance, menu);
  syncStageQuickMenu();
}
function removeStageQuickMenu() {
  $("stage-dialogue")?.querySelectorAll(".stage-quick-menu, .stage-advance").forEach(node => node.remove());
}
// ▼ 可见 = 当前回复显示完、空闲（非打字 / 揭示 / 等待）；跳过按钮在无内容揭示时禁用。
function syncStageQuickMenu() {
  if (!featureEnabled("ui_snow_adv")) return;
  const advance = $("stage-advance");
  const speech = $("stage-speech");
  const pending = typingStateFor();
  const revealing = Boolean(speech && (speech.classList.contains("is-typing") || speech.classList.contains("is-revealing")));
  const waiting = pending?.channel === "in_person" && ["connecting", "typing", "arrival", "segment", "waiting", "presenting"].includes(pending.phase);
  const queue = presentationFor();
  const hasAssistant = Boolean(latestInPersonMessage("assistant"));
  const inPerson = !$("in-person-surface").hidden;
  if (advance) advance.classList.toggle("is-ready", Boolean(inPerson && hasAssistant && !revealing && !waiting && !queue));
  const skip = $("stage-quick-menu")?.querySelector('[data-stage-quick="skip"]');
  if (skip) skip.disabled = !(revealing || queue || (state.streamingPresentationByCharacter.get(state.selected)?.receivedCount > state.streamingPresentationByCharacter.get(state.selected)?.visibleCount));
}
function syncStageQuickMenuMount() {
  if (featureEnabled("ui_snow_adv")) buildStageQuickMenu();
  else removeStageQuickMenu();
  syncSnowVeilMount();
}
// 雪境 ADV (P4 后续)：白雪转场遮罩。生产环境 motion_v2/scene_actions_v2 开启时，旧的三个
// 转场层根本不显示，所以用这个独立的、仅装饰的覆盖层来呈现地点转场。节点仅在开关开启时建、
// aria-hidden、pointer-events:none；地点播报仍只走既有 aria-live（本层不参与无障碍播报）。
function buildSnowVeil() {
  const stage = $("scene-stage");
  if (!stage || stage.querySelector(".snow-scene-veil")) return;
  const veil = document.createElement("div");
  veil.className = "snow-scene-veil";
  veil.id = "snow-scene-veil";
  veil.setAttribute("aria-hidden", "true");
  veil.dataset.phase = "idle";
  const card = document.createElement("div");
  card.className = "snow-scene-veil-card";
  const kicker = document.createElement("span");
  kicker.className = "snow-scene-veil-kicker";
  const title = document.createElement("strong");
  title.className = "snow-scene-veil-title";
  const sub = document.createElement("span");
  sub.className = "snow-scene-veil-sub";
  card.append(kicker, title, sub);
  veil.append(card);
  stage.append(veil);
}
function removeSnowVeil() {
  cancelSnowScenePresentation();
  $("scene-stage")?.querySelector(".snow-scene-veil")?.remove();
}
function clearSnowVeil() {
  for (const timer of state.snowVeilTimers.splice(0)) window.clearTimeout(timer);
  state.snowVeilAnimation?.cancel();
  state.snowVeilAnimation = null;
  const veil = $("snow-scene-veil");
  for (const animation of veil?.getAnimations?.({ subtree: true }) || []) animation.cancel();
  if (veil) veil.dataset.phase = "idle";
}
function finishSnowCamera() {
  snowPushContext = null;
  delete $("chat-app").dataset.snowPushing;
  if (snowBackdrop) {
    snowBackdrop.setScene(snowBackdropScene());
    snowBackdrop.setMotion(currentMotionLevel());
  }
  if (snowWorld) snowWorld.setMotion(currentMotionLevel());
}
function cancelSnowScenePresentation() {
  snowPresentationSequence += 1;
  snowBackdrop?.cancelPush();
  clearSnowVeil();
  finishSnowCamera();
  if (snowWorld) snowWorld.setScene(snowWorldScene());
}
function syncSnowVeilMount() {
  if (featureEnabled("ui_snow_adv")) buildSnowVeil();
  else removeSnowVeil();
}
// 决定是否在本次 renderScene 播放遮罩，并在遮罩掩护下切换 P2 世界场景（仅视觉延迟，不延迟逻辑）。
// 触发：开关开启、面对面可见、已布防（排除首帧/历史恢复）、地点确实变化。
// 不触发：初始加载/历史恢复、重开通讯器回到同一地点、地点未变、off/减少动态。
function maybePlaySnowVeil(scene, visualKey) {
  const location = plain(scene?.character_location || "");
  const visible = snowAdvActive() && !$("in-person-surface").hidden;
  const tier = stageMotionTier();
  if (snowPushContext && (!visible || document.hidden || tier !== "auto"
      || snowPushContext.characterId !== state.selected || snowPushContext.location !== location
      || snowPushContext.visualKey !== visualKey)) cancelSnowScenePresentation();
  // 遮罩不适用时，P2 舞台世界场景照旧立即切换（舞台世界仅面对面可见时存在）。
  const commitScene = () => { if (snowWorld) snowWorld.setScene(visualKey); };
  if (!visible || !location || tier === "off") {
    if (state.snowVeilTimers.length || state.snowVeilAnimation) cancelSnowScenePresentation();
    commitScene();
    if (visible && location) state.snowVeilLocation = location;
    if (visible && state.snowBeenInPerson && visualKey !== "generic") state.snowEntryConsumed = true;
    armSnowVeilSoon();
    return;
  }
  const changed = state.snowVeilArmed && location !== state.snowVeilLocation;
  state.snowVeilLocation = location;
  if (!changed) { commitScene(); armSnowVeilSoon(); return; }
  // P6a：首次由「外面」进入室内，先镜头推进再 P4 veil；推进接管后不再立即切场景。
  if (snowMaybePushIntoScene(scene, visualKey, commitScene)) return;
  if (state.snowBeenInPerson && visualKey !== "generic") state.snowEntryConsumed = true;
  playSnowVeil(scene, location, tier, commitScene);
}
// 首帧/历史恢复发生在同一帧的同步突发里；用一次 rAF 把布防推到下一帧，排除这段突发。
function armSnowVeilSoon() {
  if (state.snowVeilArmed || state.snowVeilSeen) return;
  state.snowVeilSeen = true;
  requestAnimationFrame(() => { state.snowVeilArmed = true; });
}
function playSnowVeil(scene, location, tier, commitScene) {
  const veil = $("snow-scene-veil");
  if (!veil || typeof veil.animate !== "function" || document.hidden) { commitScene(); return; }
  clearSnowVeil();
  const sequence = snowPresentationSequence;
  const card = veil.querySelector(".snow-scene-veil-card");
  const kicker = veil.querySelector(".snow-scene-veil-kicker");
  const title = veil.querySelector(".snow-scene-veil-title");
  const sub = veil.querySelector(".snow-scene-veil-sub");
  // 标题卡文案：标题=地点名，副标题=角色活动（本层 aria-hidden，不额外播报）。
  if (kicker) kicker.textContent = "场景";
  if (title) title.textContent = location;
  if (sub) sub.textContent = plain(scene?.character_activity || "");
  const fadeIn = tier === "light" ? 180 : 300;
  const hold = 600;
  const fadeOut = tier === "light" ? 180 : 420;
  veil.dataset.phase = "up";
  veil.dataset.tier = tier;
  const inAnim = veil.animate([{ opacity: 0 }, { opacity: 1 }], { duration: fadeIn, easing: "cubic-bezier(0.22, 0.61, 0.36, 1)", fill: "both" });
  state.snowVeilAnimation = inAnim;
  if (tier !== "light" && card) {
    card.animate([{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: fadeIn + 160, easing: "cubic-bezier(0.34, 1.32, 0.5, 1)", fill: "both" });
  }
  // 掩护下切换世界场景（纯视觉延迟）。
  state.snowVeilTimers.push(window.setTimeout(() => { if (sequence === snowPresentationSequence) commitScene(); }, fadeIn));
  state.snowVeilTimers.push(window.setTimeout(() => {
    if (sequence !== snowPresentationSequence) return;
    veil.dataset.phase = "down";
    const outAnim = veil.animate([{ opacity: 1 }, { opacity: 0 }], { duration: fadeOut, easing: "cubic-bezier(0.22, 0.61, 0.36, 1)", fill: "both" });
    state.snowVeilAnimation = outAnim;
    outAnim.finished.catch(() => {}).finally(() => {
      if (state.snowVeilAnimation !== outAnim) return;
      state.snowVeilAnimation = null;
      veil.dataset.phase = "idle";
      try { outAnim.cancel(); } catch {}
    });
  }, fadeIn + hold));
}

// ================================================================ P6a 开场与通讯器世界层
// 全部仅在 ui_snow_adv 开启时生效，节点由 JS 在开关开启时建出、关闭态不存在，所以
// flag-off 的 DOM 与行为逐字节不变。开机/锁屏二选一在页面加载时按屏幕判定一次
// （与手机布局同一 860px 分界），中途改窗口大小不切换。减少动态/off 档无动画。
const SNOW_BOOT_SEEN_KEY = "project-snow-public:snow-adv:boot-seen";   // 同一日历日跳过开机视觉
const SNOW_LOCK_SEEN_KEY = "project-snow-public:snow-adv:lock-seen";  // 同一浏览器会话跳过锁屏
const SNOW_TOD_LABELS = { dawn: "清晨", day: "白天", dusk: "黄昏", night: "夜晚" };

function snowOpeningState() {
  if (!state.snowOpening) {
    const phone = window.matchMedia("(max-width: 860px)").matches;
    state.snowOpening = { phone, kind: phone ? "lock" : "boot" };
  }
  return state.snowOpening;
}
// 开机层或锁屏层是否正占据屏幕——其间不得弹出体验须知、新手引导或任何对话框
// （<dialog>.showModal() 走顶层，会盖在开机/锁屏之上），须在消隐/解锁后按正常顺序出现。
function snowOpeningActive() {
  return Boolean(document.getElementById("snow-boot") || document.getElementById("snow-lock"));
}
// 开场层消隐/解锁后调用：先按既有顺序弹一次性体验须知，再渲染尚未完成的新手引导。
async function snowRunDeferredOpenings() {
  await showExperienceNoticeIfNeeded();
  renderOnboarding();
}
function snowLocalDay(now = new Date()) {
  return `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}`;
}
function snowTimeOfDayLabel() {
  // 复用世界模块的 resolveTime 口径（含 html[data-snow-time] 覆盖，仅测试/截图）。
  const override = document.documentElement.dataset.snowTime;
  const h = new Date().getHours();
  const key = override && SNOW_TOD_LABELS[override] ? override
    : h >= 5 && h < 8 ? "dawn" : h >= 8 && h < 17 ? "day" : h >= 17 && h < 19 ? "dusk" : "night";
  return SNOW_TOD_LABELS[key];
}

// ---- 开机层「终端开机」（桌面） ----
function buildSnowBoot() {
  if ($("snow-boot") || document.body.querySelector(".snow-boot")) return;
  const boot = document.createElement("div");
  boot.className = "snow-boot";
  boot.id = "snow-boot";
  boot.setAttribute("role", "status");
  boot.setAttribute("aria-label", "小吉终端正在启动");
  boot.dataset.phase = "in";
  boot.dataset.tier = stageMotionTier();
  const mark = document.createElement("div");
  mark.className = "snow-boot-mark";
  const img = document.createElement("img");
  img.src = "/shared/brand/xiaoji-icon-192.png";
  img.alt = "";
  const h1 = document.createElement("h1");
  h1.textContent = "小吉终端";
  const bar = document.createElement("div");
  bar.className = "snow-boot-bar";
  bar.setAttribute("aria-hidden", "true");
  bar.append(document.createElement("i"));
  const status = document.createElement("p");
  status.className = "snow-boot-status";
  status.id = "snow-boot-status";
  mark.append(img, h1, bar, status);
  boot.append(mark);
  document.body.append(boot);
}
function snowBootStep(label) {
  const status = $("snow-boot-status");
  if (status) status.textContent = label;
}
function dismissSnowBoot(onRemoved) {
  const boot = $("snow-boot");
  state.snowBootDone = true;
  if (!boot) { if (onRemoved) onRemoved(); return; }
  snowBootStep("就绪");
  // 同一日历日重访直接跳过视觉：记录「上次见过开机」的日期。
  try { localPreferences.setItem(SNOW_BOOT_SEEN_KEY, snowLocalDay()); } catch { /* 存储不可用则下次照常显示 */ }
  const tier = stageMotionTier();
  boot.dataset.tier = tier;
  // 节点真正移除后才允许体验须知/新手引导出现（它们绝不能盖在开机层之上）。
  const finish = () => { boot.remove(); if (onRemoved) onRemoved(); };
  if (tier === "off" || typeof boot.animate !== "function") { finish(); return; }
  boot.dataset.phase = "out";
  // ≤300ms 淡出；不额外拖延。
  window.setTimeout(finish, 320);
}
// 开机层只在：桌面开场、开关开启、且今天还没见过时显示；否则不建（不加任何延迟）。
function maybeStartSnowBoot() {
  if (!featureEnabled("ui_snow_adv")) return false;
  const opening = snowOpeningState();
  if (opening.kind !== "boot") return false;
  let seen = "";
  try { seen = localPreferences.getItem(SNOW_BOOT_SEEN_KEY) || ""; } catch { seen = ""; }
  if (seen === snowLocalDay()) return false;
  buildSnowBoot();
  snowBootStep("读取本地记录");
  return true;
}

// ---- 锁屏层「终端锁屏」（手机） ----
function snowLockNotes() {
  // 通知卡仅由既有状态构造：当前待赴约定、未读公告。各源独立容错——某子系统
  // 尚未就绪时该卡缺省，绝不阻断开场。
  const notes = [];
  try {
    // 当前待赴约定：复用既有签名约定判定；点开走「去见她/赴约」确认流程，绝不自动赴约。
    const pending = sceneActionsV2Enabled() ? signedPendingRendezvous().value : null;
    if (pending) {
      const location = pending.location_name || "约定地点";
      notes.push({ kind: "rendezvous", characterId: pending.character_id || state.selected,
        title: `约定 · ${location}`, detail: "点开确认是否赴约" });
    }
  } catch { /* 约定子系统未就绪则无此卡 */ }
  try {
    if (announcementView().unread.length) {
      notes.push({ kind: "notice", title: "公告与生日", detail: `${announcementView().unread.length} 条新消息` });
    }
  } catch { /* 公告未加载则无此卡 */ }
  return notes;
}
function buildSnowLock() {
  if ($("snow-lock")) return;
  const lock = document.createElement("div");
  lock.className = "snow-lock";
  lock.id = "snow-lock";
  lock.setAttribute("role", "dialog");
  lock.setAttribute("aria-modal", "true");
  lock.tabIndex = 0;
  lock.setAttribute("aria-label", "小吉终端锁屏，点击或按回车解锁");
  lock.dataset.phase = "in";
  lock.dataset.tier = stageMotionTier();
  const bar = document.createElement("div");
  bar.className = "snow-lock-bar";
  const barImg = document.createElement("img");
  barImg.src = "/shared/brand/xiaoji-icon-192.png"; barImg.alt = "";
  const barName = document.createElement("strong"); barName.textContent = "小吉终端";
  const tod = document.createElement("span"); tod.className = "snow-lock-tod"; tod.textContent = snowTimeOfDayLabel();
  bar.append(barImg, barName, tod);
  const main = document.createElement("div");
  main.className = "snow-lock-main";
  const time = document.createElement("div");
  time.className = "snow-lock-time"; time.id = "snow-lock-time";
  const date = document.createElement("div");
  date.className = "snow-lock-date"; date.id = "snow-lock-date";
  const notes = document.createElement("div");
  notes.className = "snow-lock-notes"; notes.id = "snow-lock-notes";
  main.append(time, date, notes);
  const hint = document.createElement("div");
  hint.className = "snow-lock-hint";
  hint.append(createSnowIcon("snowflake"));
  hint.append(document.createTextNode("点击任意处解锁"));
  lock.append(bar, main, hint);
  // 时钟与日期。
  const now = new Date();
  time.textContent = now.toTimeString().slice(0, 5);
  date.textContent = `${now.getMonth() + 1} 月 ${now.getDate()} 日 · 星期${"日一二三四五六"[now.getDay()]}`;
  // 通知卡（公告等异步源就绪后由 snowRefreshLockNotes 重渲染）。
  snowRenderLockNotes(notes);
  document.body.append(lock);
  lock.snowPreviousInert = $("chat-app").inert;
  $("chat-app").inert = true;
  lock.addEventListener("click", () => snowUnlock(null));
  lock.addEventListener("keydown", (event) => {
    // 子通知按钮保留原生 Enter/Space 激活，不能被外层解锁处理吞掉。
    if (["Escape", "Esc"].includes(event.key)
        || event.target === lock && ["Enter", " ", "Spacebar"].includes(event.key)) {
      event.preventDefault(); snowUnlock(null);
    }
    if (event.key === "Tab") {
      const targets = [lock, ...lock.querySelectorAll("button")];
      const index = targets.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0 || !event.shiftKey && index === targets.length - 1) {
        event.preventDefault(); targets[event.shiftKey ? targets.length - 1 : 0].focus();
      }
    }
  });
  window.setTimeout(() => lock.focus({ preventScroll: true }), 0);
}
// 渲染锁屏通知卡（可重入，公告等异步源就绪后重渲染）。
function snowRenderLockNotes(notesEl) {
  if (!notesEl) return;
  const now = new Date();
  const focused = notesEl.contains(document.activeElement) ? document.activeElement : null;
  const focusedKind = focused?.dataset.snowNote;
  const focusedCharacter = focused?.dataset.characterId;
  let restoreFocus = null;
  notesEl.replaceChildren();
  for (const note of snowLockNotes()) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "snow-note-card";
    card.dataset.snowNote = note.kind;
    if (note.characterId) card.dataset.characterId = note.characterId;
    const glyph = document.createElement("span");
    glyph.className = "snow-note-glyph";
    glyph.append(createSnowIcon(note.kind === "rendezvous" ? "footprints" : "bell"));
    const meta = document.createElement("span");
    meta.className = "snow-note-meta";
    const strong = document.createElement("strong"); strong.textContent = note.title;
    const span = document.createElement("span"); span.textContent = note.detail;
    meta.append(strong, span);
    const t = document.createElement("time"); t.textContent = now.toTimeString().slice(0, 5);
    card.append(glyph, meta, t);
    card.addEventListener("click", (event) => { event.stopPropagation(); snowUnlock(note); });
    notesEl.append(card);
    if (note.kind === focusedKind && card.dataset.characterId === focusedCharacter) restoreFocus = card;
  }
  if (focused) (restoreFocus || $("snow-lock"))?.focus({ preventScroll: true });
}
// 公告等异步源就绪时，若锁屏仍在，重渲染其通知卡（新出现的未读公告据此补上卡片）。
function snowRefreshLockNotes() {
  const notesEl = document.getElementById("snow-lock-notes");
  if (notesEl) snowRenderLockNotes(notesEl);
}
let snowLockUnlocking = false;
function snowUnlock(note) {
  const lock = $("snow-lock");
  if (!lock || snowLockUnlocking) return;
  snowLockUnlocking = true;
  try { tabPreferences.setItem(SNOW_LOCK_SEEN_KEY, "1"); } catch { /* 会话存储不可用则本会话内可能重复，无副作用 */ }
  const tier = stageMotionTier();
  const finish = () => {
    $("chat-app").inert = Boolean(lock.snowPreviousInert);
    lock.remove();
    $("open-settings")?.focus({ preventScroll: true });
    // 解锁后再按既有顺序弹出一次性体验须知等（锁屏期间被推迟）。
    void snowAfterUnlock(note);
  };
  if (tier === "off" || typeof lock.animate !== "function") { finish(); return; }
  lock.dataset.phase = "out";
  window.setTimeout(finish, 340);
}
async function snowAfterUnlock(note) {
  // 体验须知与新手引导在解锁后按正常顺序出现（锁屏期间被推迟，且绝不盖在锁屏之上）。
  await snowRunDeferredOpenings();
  if (!note) return;
  // 通知卡复用既有处理，绝不自动赴约 / 自动发消息。
  if (note.kind === "rendezvous") { if (note.characterId && note.characterId !== state.selected) await selectCharacter(note.characterId); await openPresenceDialog(); }
  else if (note.kind === "notice") { openAnnouncements(); }
}
function maybeStartSnowLock() {
  if (!featureEnabled("ui_snow_adv")) return false;
  const opening = snowOpeningState();
  if (opening.kind !== "lock") return false;
  let seen = "";
  try { seen = tabPreferences.getItem(SNOW_LOCK_SEEN_KEY) || ""; } catch { seen = ""; }
  if (seen === "1") return false;
  buildSnowLock();
  return true;
}

// ---- 通讯器背后的世界层（桌面） ----
// 首次由「外面」进入某室内地点时的镜头推进 → 随后交给 P4 veil（显示目的地）。供 renderScene 调用。
// 返回 true 表示本次已用推进接管场景切换（调用方不再播放自己的 veil / 立即 setScene）。
// 推进作用于 body 级的「通讯器背后世界」(snowBackdrop)——它在人入室前显示 generic 外景，
// 其 .push-target 是基地外亮灯入口；推进把外景推近入口 → 白光掩护里切到室内 → veil 显示目的地。
function snowMaybePushIntoScene(scene, visualKey, commitScene) {
  const tier = stageMotionTier();
  const destination = plain(scene?.character_location || "") || visualKey;
  const activity = plain(scene?.character_activity || "");
  // 首次入室 = backdrop 当前仍是 generic 外景、目标是具体室内、尚未推进过、auto 档。
  const canPush = featureEnabled("ui_snow_adv") && !snowOpeningState().phone
    && !state.snowPushPlayed && !state.snowEntryConsumed && tier === "auto"
    && snowBackdrop && typeof snowBackdrop.pushInto === "function" && snowBackdrop.scene === "generic"
    && visualKey && visualKey !== "generic" && !document.hidden;
  if (!canPush) return false;
  state.snowPushPlayed = true;
  state.snowEntryConsumed = true;
  const sequence = ++snowPresentationSequence;
  snowPushContext = { characterId: state.selected, location: destination, visualKey };
  $("chat-app").dataset.snowPushing = "1";
  // 仅把舞台的视觉层暂时让开，逻辑/回复照常推进。外景与入口真实可见，且只运行外景雪花。
  if (snowWorld) snowWorld.setMotion(currentMotionLevel());
  snowBackdrop.setMotion(currentMotionLevel());
  void snowBackdrop.pushInto(visualKey, {
    onWhiteout: () => {
      if (sequence !== snowPresentationSequence || !snowPushContext) return true;
      // veil 真正盖住外景后才显示目的地，避免先出现室内再播放被遮住的推进。
      playSnowVeil({ character_location: destination, character_activity: activity }, destination, tier, () => {
        commitScene();
        finishSnowCamera();
      });
      return true; // 世界切换已由 veil 接管。
    },
  });
  return true;
}
// ================================================================ /P6a

function bodyIsVisible(node) {
  if (!node?.isConnected || node.closest("[hidden], [inert]") || document.hidden) return false;
  if (getComputedStyle(node).visibility !== "visible") return false;
  const bounds = node.getBoundingClientRect();
  const clip = node.closest("#timeline")?.getBoundingClientRect() || { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
  return bounds.width > 0 && bounds.height > 0 && bounds.right > Math.max(0, clip.left) && bounds.left < Math.min(innerWidth, clip.right)
    && bounds.bottom > Math.max(0, clip.top) && bounds.top < Math.min(innerHeight, clip.bottom);
}
function recordReplyPaint(requestId, node) {
  const timing = replyTiming(requestId);
  if (!timing || timing.firstEffectiveAt || !bodyIsVisible(node)) return;
  const stage = node.id === "stage-speech";
  const stageMessage = () => {
    const message = latestInPersonMessage("assistant");
    return message?.requestId === requestId && state.typewriter.key === speechTypewriterKey(message) ? message : null;
  };
  if (stage && !stageMessage()) return;
  // First glyph paint measures rendering; a full confirmed speech block measures
  // an effective reply. Decorative typewriting must not improve the latter.
  requestAnimationFrame(() => {
    if (timing.firstEffectiveAt || !bodyIsVisible(node)) return;
    const message = stage ? stageMessage() : null;
    if (stage && !message) return;
    const firstSpeech = stage ? visibleBlocksFor(message).find(block => block.type === "speech" && plain(block.text).trim()) : null;
    const effective = !stage || Boolean(firstSpeech && node.textContent.startsWith(plain(firstSpeech.text).trim()));
    let changed = false;
    if (!timing.firstPaintAt) {
      timing.firstPaintAt = performance.now();
      timing.firstNaturalWaitMs = timing.naturalWaitMs;
      changed = true;
    }
    if (effective) { timing.firstEffectiveAt = performance.now(); changed = true; }
    if (changed && timing.completedAt) document.dispatchEvent(new CustomEvent("project-snow:reply-visible", { detail: anonymousTiming(timing) }));
  });
}
let replyVisibilityFrame = 0;
function sampleVisibleReplyPaints() {
  if (document.hidden || replyVisibilityFrame) return;
  replyVisibilityFrame = requestAnimationFrame(() => {
    replyVisibilityFrame = 0;
    const pending = new Map([...state.replyTimingByRequest].filter(([, timing]) => !timing.firstEffectiveAt && timing.characterId === state.selected && timing.messageId)
      .map(([requestId, timing]) => [timing.messageId, requestId]));
    if (!$("text-surface").hidden) {
      for (const node of $("timeline").querySelectorAll(".message.assistant[data-message-id]")) {
        const requestId = pending.get(node.dataset.messageId);
        if (requestId) recordReplyPaint(requestId, node.querySelector(".message-content-stack"));
      }
    } else {
      const message = latestInPersonMessage("assistant");
      const speech = $("stage-speech");
      if (pending.has(message?.id) && speech.textContent && !speech.classList.contains("is-typing")) recordReplyPaint(message.requestId, speech);
    }
  });
}
async function waitForNaturalReply(requestId, signal) {
  const timing = replyTiming(requestId);
  if (!timing || timing.presented) return;
  const remaining = remainingNaturalWait(timing.sentAt, performance.now(), quietPresentation() || timing.skip);
  if (remaining) {
    const started = performance.now();
    // Small slices allow a live reduced-motion change or fast-forward to finish promptly.
    try {
      while (!quietPresentation() && !timing.skip && remainingNaturalWait(timing.sentAt, performance.now()) > 0) {
        await abortableDelay(Math.min(32, remainingNaturalWait(timing.sentAt, performance.now())), signal);
      }
    } finally { timing.naturalWaitMs += performance.now() - started; }
  }
  timing.presented = true;
}
function syncFastForward() {
  const button = $("fast-forward-reply");
  if (!button) return;
  const queue = presentationFor();
  const streaming = state.streamingPresentationByCharacter.get(state.selected);
  const revealing = !$("in-person-surface").hidden && state.typewriter.fullText && latestInPersonMessage("assistant");
  button.hidden = !featureEnabled("chat_latency_v2") || !(queue || revealing || state.completedPresentationByCharacter.has(state.selected) || streaming?.receivedCount > streaming?.visibleCount);
  const row = button.closest(".request-status-row");
  if (!button.hidden && row) row.hidden = false;
  syncStageQuickMenu();
}
function finishReceivedPresentation() {
  const currentTiming = replyTiming(typingStateFor()?.requestId);
  if (currentTiming) currentTiming.skip = true;
  const queue = presentationFor();
  if (queue) {
    queue.skip = true;
    queue.visibleCount = queue.blocks.length;
    const timing = replyTiming(queue.requestId);
    if (timing) timing.skip = true;
    renderTimeline();
  }
  if (state.typewriter.fullText) finishTypewriter(state.typewriter.key, state.typewriter.fullText);
  syncFastForward();
}
async function deliverCompletedReply(characterId, requestId, message, visibleCount = 0) {
  const ready = { requestId, messageId: message.id, visibleCount, controller: new AbortController() };
  state.completedPresentationByCharacter.set(characterId, ready);
  syncFastForward();
  try {
    await waitForNaturalReply(requestId, ready.controller.signal);
    state.readyReplyIds.delete(message.id);
    if (!ownsTypingState(characterId, requestId)) return;
    await presentAssistantTurn(characterId, requestId, message, { visibleCount });
  } catch (error) {
    if (error?.name !== "AbortError") {
      cancelPresentationQueue(characterId, requestId, { render: false });
      showBanner("回复已经保存，可在记录中查看。");
    }
  } finally {
    state.readyReplyIds.delete(message.id);
    if (state.completedPresentationByCharacter.get(characterId) === ready) state.completedPresentationByCharacter.delete(characterId);
    clearTypingState(characterId, requestId);
    if (state.selected === characterId) renderAll();
    syncFastForward(); updateComposerAvailability();
  }
}
function prepareSurfaceMotion(channel) {
  const previous = $("chat-app").dataset.channel;
  if (previous === channel) return null;
  surfaceMotion.cancel();
  if (!(featureEnabled("motion_v2") || sceneActionsV2Enabled()) || quietPresentation()) return null;
  const outgoing = $(previous === "in_person" ? "in-person-surface" : "text-surface");
  if (!outgoing || outgoing.hidden) return null;
  const bounds = outgoing.getBoundingClientRect();
  const parent = outgoing.parentElement.getBoundingClientRect();
  const snapshot = outgoing.cloneNode(true);
  snapshot.removeAttribute("id");
  snapshot.querySelectorAll("[id]").forEach(node => node.removeAttribute("id"));
  snapshot.classList.add("surface-transition-snapshot");
  snapshot.inert = true;
  snapshot.setAttribute("aria-hidden", "true");
  Object.assign(snapshot.style, { top: `${bounds.top - parent.top}px`, left: `${bounds.left - parent.left}px`, width: `${bounds.width}px`, height: `${bounds.height}px` });
  outgoing.parentElement.append(snapshot);
  surfaceMotion.cleanup(() => snapshot.remove());
  return snapshot;
}
function animateSurface(channel, snapshot) {
  if (!snapshot) return;
  const incoming = $(channel === "in_person" ? "in-person-surface" : "text-surface");
  const distance = document.documentElement.dataset.motionLevel === "light" ? 0 : channel === "in_person" ? 14 : -14;
  const options = { duration: PRESENTATION_TIMING.mode, easing: "cubic-bezier(.22,.72,.24,1)", fill: "both" };
  void Promise.all([
    surfaceMotion.play(snapshot, [{ opacity: 1, transform: "translateY(0)" }, { opacity: 0, transform: `translateY(${-distance}px)` }], options),
    surfaceMotion.play(incoming, [{ opacity: 0, transform: `translateY(${distance}px)` }, { opacity: 1, transform: "translateY(0)" }], options),
  ]).finally(() => snapshot.remove());
}
const TYPEWRITER_TARGET_STEPS = 90;
const TYPEWRITER_MAX_DURATION_MS = 5200;
function codePointOf(value) { return plain(value).codePointAt(0) || 0; }
function isRegionalIndicator(value) {
  const point = codePointOf(value);
  return point >= 0x1f1e6 && point <= 0x1f1ff;
}
function isEmojiModifier(value) {
  const point = codePointOf(value);
  return point >= 0x1f3fb && point <= 0x1f3ff;
}
function isVariationSelector(value) {
  const point = codePointOf(value);
  return point >= 0xfe00 && point <= 0xfe0f || point >= 0xe0100 && point <= 0xe01ef;
}
function isEmojiTag(value) {
  const point = codePointOf(value);
  return point >= 0xe0020 && point <= 0xe007f;
}
function isCombiningMark(value) { return /^\p{Mark}$/u.test(value); }
function fallbackGraphemeSegments(value) {
  const codePoints = Array.from(plain(value));
  const segments = [];
  let current = "";
  for (const codePoint of codePoints) {
    if (!current) {
      current = codePoint;
      continue;
    }
    const currentCodePoints = Array.from(current);
    const attach = isCombiningMark(codePoint)
      || isVariationSelector(codePoint)
      || isEmojiModifier(codePoint)
      || isEmojiTag(codePoint)
      || codePoint === "\u200d"
      || current.endsWith("\u200d")
      || current === "\r" && codePoint === "\n"
      || isRegionalIndicator(codePoint) && currentCodePoints.length === 1 && isRegionalIndicator(currentCodePoints[0]);
    if (attach) current += codePoint;
    else {
      segments.push(current);
      current = codePoint;
    }
  }
  if (current) segments.push(current);
  return segments;
}
function graphemeSegments(value) {
  const text = plain(value);
  const Segmenter = globalThis.Intl?.Segmenter;
  if (typeof Segmenter === "function") {
    try {
      return Array.from(new Segmenter(undefined, { granularity: "grapheme" }).segment(text), (entry) => entry.segment);
    } catch { /* Fall through to the compatibility segmenter. */ }
  }
  return fallbackGraphemeSegments(text);
}
function typewriterDelayAfter(character, nextCharacter = "", previousCharacter = "") {
  const nextIsCloser = /[”’」』】》）)]/.test(nextCharacter);
  if (character.includes("\n")) return 180;
  if (character === "…" && nextCharacter === "…") return 24;
  if (character === "…" || /[。！？!?]/.test(character)) return nextIsCloser ? 24 : 320;
  if (/[”’」』】》）)]/.test(character) && /[。！？!?…]/.test(previousCharacter)) return 320;
  if (/[；;]/.test(character)) return 220;
  if (/[，,、：:]/.test(character)) return 110;
  return 24;
}
function typewriterRevealPlan(graphemes, startIndex = 0) {
  const remaining = Math.max(0, graphemes.length - startIndex);
  if (!remaining) return { initialDelay: 0, steps: [] };
  const batchSize = Math.max(1, Math.ceil(remaining / TYPEWRITER_TARGET_STEPS));
  const steps = [];
  let index = startIndex;
  while (index < graphemes.length) {
    const limit = Math.min(graphemes.length, index + batchSize);
    let end = limit;
    for (let cursor = index; cursor < limit; cursor += 1) {
      const pause = typewriterDelayAfter(
        graphemes[cursor],
        graphemes[cursor + 1] || "",
        graphemes[cursor - 1] || "",
      );
      if (pause > 24) {
        end = cursor + 1;
        break;
      }
    }
    const last = end - 1;
    steps.push({
      end,
      delayAfter: typewriterDelayAfter(
        graphemes[last],
        graphemes[end] || "",
        graphemes[last - 1] || "",
      ),
    });
    index = end;
  }
  const rawDuration = 24 + steps.slice(0, -1).reduce((total, step) => total + step.delayAfter, 0);
  const scale = Math.min(1, (featureEnabled("chat_latency_v2") ? 2400 : TYPEWRITER_MAX_DURATION_MS) / Math.max(1, rawDuration));
  return {
    initialDelay: Math.max(1, Math.round(24 * scale)),
    steps: steps.map((step, index) => ({
      ...step,
      delayAfter: index === steps.length - 1 ? 0 : Math.max(1, Math.round(step.delayAfter * scale)),
    })),
  };
}
function requestDelay(requestId, salt, minimum, maximum) {
  const value = `${plain(requestId)}:${plain(salt)}`;
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  const span = Math.max(0, maximum - minimum);
  return minimum + ((hash >>> 0) % (span + 1));
}
function normalizedSearch(value) {
  return plain(value).toLocaleLowerCase("zh-CN").replace(/[\s'·-]/g, "");
}
const CHAT_STREAM_TIMEOUT_MS = 165000;
const LOCAL_PINYIN_TOKENS = {
  "78aa7ab99154": ["yqe", "yiqieer"],
  "d5ecfceba959": ["klrn", "keluoruina"],
  "25b23cb64398": ["kxy", "kaixiya"],
  "6455a5dcff6a": ["bb", "bubu"],
  "41b7444e39cc": ["nld", "nailide"],
  "4370a74d6fda": ["nt", "nita"],
  "9f5804761c56": ["akxy", "ankaxiya"],
  "43f05917bfa1": ["ey", "enya"],
  "921f9ef0cc4e": ["q", "qing", "mlq", "minglaiqing"],
  "6862c43d2ac9": ["mxe", "maoxier"],
  "8d5b5c3912bb": ["qn", "qinnuo", "qin nuo"],
  "85b205f6f623": ["srs", "seruisi"],
  "702f4375675b": ["my", "mia", "miya"],
  "cf0569ac6de9": ["y", "yao"],
  "447ed3c401c9": ["ly", "longyan"],
  "a2ffc5b44d7f": ["fty", "futiya"],
  "1b0a6b35719a": ["fn", "fenni"],
  "673ba6851b05": ["ts", "taisi", "tess", "taisiketejin"],
  "daab0f4cceb4": ["mla", "molian"],
  "5157b8972632": ["wdy", "weidiya"],
  "98322bd505f4": ["cx", "chenxing"],
  "ca0144ccd81b": ["lf", "lifu"],
};
const localDayFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit",
});
function localDayKey(timestamp = Date.now()) {
  const parts = localDayFormatter.formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function newConversationSegment() { return id(); }

async function api(path, options = {}) {
  return publicHttp.request(path, options);
}

function openDB() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    let request;
    try {
      request = indexedDB.open(dbName, dbVersion);
    } catch (error) {
      reject(error);
      return;
    }
    request.onupgradeneeded = (event) => {
      const db = request.result;
      const transaction = request.transaction;
      const threads = db.objectStoreNames.contains("threads")
        ? transaction.objectStore("threads")
        : db.createObjectStore("threads", { keyPath: "characterId" });
      if (!db.objectStoreNames.contains("app_state")) db.createObjectStore("app_state", { keyPath: "key" });
      const messages = db.objectStoreNames.contains("messages")
        ? transaction.objectStore("messages")
        : db.createObjectStore("messages", { keyPath: "id" });
      if (!messages.indexNames.contains("by_character_created")) {
        messages.createIndex("by_character_created", ["characterId", "createdAt"], { unique: false });
      }
      if (!messages.indexNames.contains("by_character_segment_created")) {
        messages.createIndex("by_character_segment_created", ["characterId", "conversationSegmentId", "createdAt"], { unique: false });
      }
      if (Number(event.oldVersion || 0) < 4) {
        const cursorRequest = threads.openCursor();
        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (!cursor) return;
          const record = cursor.value || {};
          const legacyMessages = Array.isArray(record.messages) ? record.messages : [];
          for (const message of legacyMessages) {
            const normalized = normalizeMessage({ ...message, characterId: message.characterId || message.character_id || record.characterId });
            messages.put(normalized);
          }
          const metadata = { ...record, messageCount: legacyMessages.length };
          delete metadata.messages;
          cursor.update(metadata);
          cursor.continue();
        };
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("indexeddb_blocked"));
  }).catch(() => {
    useMemoryStorage();
    return null;
  });
  return dbPromise;
}
function safeExternalUrl(value) {
  try {
    const url = new URL(plain(value));
    return url.protocol === "https:" ? url.href : "";
  } catch { return ""; }
}
async function storeGet(storeName, key) {
  const db = await openDB();
  if (!db) return state.memoryStores[storeName]?.get(key) || null;
  try {
    return await new Promise((resolve, reject) => {
    const request = db.transaction(storeName, "readonly").objectStore(storeName).get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
    });
  } catch {
    useMemoryStorage();
    return state.memoryStores[storeName]?.get(key) || null;
  }
}
async function storeAll(storeName) {
  const db = await openDB();
  if (!db) return [...(state.memoryStores[storeName]?.values() || [])];
  try {
    return await new Promise((resolve, reject) => {
    const request = db.transaction(storeName, "readonly").objectStore(storeName).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
    });
  } catch {
    useMemoryStorage();
    return [...(state.memoryStores[storeName]?.values() || [])];
  }
}
async function storePut(storeName, value) {
  if (state.writerReady && !state.writerOwned) return;
  const key = storeName === "messages" ? value.id : storeName === "app_state" ? value.key : value.characterId;
  state.memoryStores[storeName]?.set(key, structuredClone(value));
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, "readwrite");
      tx.objectStore(storeName).put(value);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("indexeddb_write_aborted"));
    });
  } catch {
    useMemoryStorage();
  }
}
async function storeDelete(storeName, key) {
  if (state.writerReady && !state.writerOwned) return;
  state.memoryStores[storeName]?.delete(key);
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, "readwrite");
      tx.objectStore(storeName).delete(key);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("indexeddb_delete_aborted"));
    });
  } catch {
    useMemoryStorage();
  }
}
async function storeClear(storeName) {
  if (state.writerReady && !state.writerOwned) return;
  state.memoryStores[storeName]?.clear();
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, "readwrite");
      tx.objectStore(storeName).clear();
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("indexeddb_clear_aborted"));
    });
  } catch {
    useMemoryStorage();
  }
}

async function messageCount(characterId) {
  const db = await openDB();
  if (!db) {
    return [...state.memoryStores.messages.values()].filter((message) => message.characterId === characterId).length;
  }
  try {
    return await new Promise((resolve, reject) => {
      const index = db.transaction("messages", "readonly").objectStore("messages").index("by_character_created");
      const request = index.count(IDBKeyRange.bound([characterId, 0], [characterId, Number.MAX_SAFE_INTEGER]));
      request.onsuccess = () => resolve(Number(request.result || 0));
      request.onerror = () => reject(request.error);
    });
  } catch {
    useMemoryStorage();
    return [...state.memoryStores.messages.values()].filter((message) => message.characterId === characterId).length;
  }
}

function markMessageEntrySeen(message) {
  state.seenMessageEntries.add(message);
  state.paintedMessageIds.add(message.id);
  while (state.paintedMessageIds.size > 1000) state.paintedMessageIds.delete(state.paintedMessageIds.values().next().value);
}
function rememberStoredMessage(message) {
  const normalized = normalizeMessage(message);
  markMessageEntrySeen(normalized);
  state.persistedMessages.set(normalized.id, JSON.stringify(normalized));
  state.memoryStores.messages.set(normalized.id, structuredClone(normalized));
  return normalized;
}
async function loadMessagePage(characterId, { before = Number.MAX_SAFE_INTEGER, limit = 60 } = {}) {
  const memoryPage = () => [...state.memoryStores.messages.values()]
    .filter((message) => message.characterId === characterId && beforeMessageCursor(message, before))
    .sort((a, b) => compareMessageCursor(b, a)).slice(0, limit).reverse().map(rememberStoredMessage);
  const db = await openDB();
  if (!db) return memoryPage();
  try {
    return await new Promise((resolve, reject) => {
    const values = [];
    const index = db.transaction("messages", "readonly").objectStore("messages").index("by_character_created");
    const timestamp = typeof before === "number" ? before : before.createdAt;
    const upper = Math.max(0, Math.min(Number.MAX_SAFE_INTEGER, timestamp));
    // Include a composite cursor's timestamp: the primary key breaks ties
    // without changing the v4 database or requiring a destructive migration.
    const range = IDBKeyRange.bound([characterId, 0], [characterId, upper], false, typeof before === "number" && timestamp < Number.MAX_SAFE_INTEGER);
    const request = index.openCursor(range, "prev");
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor || values.length >= limit) {
        resolve(values.reverse().map(rememberStoredMessage));
        return;
      }
      if (beforeMessageCursor(cursor.value, before)) values.push(cursor.value);
      cursor.continue();
    };
    request.onerror = () => reject(request.error);
    });
  } catch {
    useMemoryStorage();
    return memoryPage();
  }
}

async function deleteMessagesForCharacter(characterId) {
  for (const [key, message] of state.memoryStores.messages) {
    if (message.characterId === characterId) state.memoryStores.messages.delete(key);
  }
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("messages", "readwrite");
      const index = tx.objectStore("messages").index("by_character_created");
      const request = index.openKeyCursor(IDBKeyRange.bound([characterId, 0], [characterId, Number.MAX_SAFE_INTEGER]));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        tx.objectStore("messages").delete(cursor.primaryKey);
        cursor.continue();
      };
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("indexeddb_delete_aborted"));
    });
  } catch {
    useMemoryStorage();
  }
}

function normalizeBlocks(blocks, channel, fallback = "") {
  const allowed = channel === "text" ? new Set(["message", "sticker"]) : new Set(["speech", "action"]);
  let normalized = Array.isArray(blocks) ? blocks
    .filter((item) => item && allowed.has(item.type) && (item.type === "sticker" || plain(item.text).trim()))
    .slice(0, 8)
    .map((item) => item.type === "sticker"
      ? { type: "sticker", assetId: plain(item.assetId || item.asset_id), asset_id: plain(item.assetId || item.asset_id), caption: plain(item.caption).trim().slice(0, 120), src: plain(item.src), displaySrc: plain(item.displaySrc || item.display_src), thumbnailSrc: plain(item.thumbnailSrc || item.thumbnail_src), animated: Boolean(item.animated), displayAnimated: Boolean(item.displayAnimated ?? item.display_animated ?? item.animated) }
      : { type: item.type, text: plain(item.text).trim() }) : [];
  if (channel === "text" && normalized.length) {
    normalized = [
      ...normalized.filter((item) => item.type !== "sticker"),
      ...normalized.filter((item) => item.type === "sticker").slice(0, 1),
    ];
  }
  if (!normalized.length && plain(fallback).trim()) normalized.push({ type: channel === "text" ? "message" : "speech", text: plain(fallback).trim() });
  return normalized;
}
function renderBlocksText(blocks) { return (blocks || []).map((block) => block.text).filter(Boolean).join("\n"); }
function wireBlocks(blocks) {
  return (blocks || []).map((block) => block.type === "sticker"
    ? { type: "sticker", asset_id: plain(block.assetId || block.asset_id), caption: plain(block.caption).slice(0, 120) }
    : { type: block.type, text: plain(block.text).trim() });
}
function normalizeMessage(message) {
  const channel = (message.communicationChannel || message.communication_channel) === "in_person" ? "in_person" : "text";
  const blocks = normalizeBlocks(message.contentBlocks || message.content_blocks, channel, message.content || "");
  const role = message.role === "assistant" ? "assistant" : "user";
  const storedDisplayBlocks = message.displayBlocks || message.display_blocks;
  const displayBlocks = message.committedBlocks?.length ? blocks : role === "assistant"
    ? deriveDisplayBlocks(storedDisplayBlocks ? normalizeBlocks(storedDisplayBlocks, channel) : blocks, channel)
    : blocks;
  const rawCreatedAt = message.createdAt ?? message.created_at;
  const parsedCreatedAt = Number(rawCreatedAt);
  const hasCreatedAt = Number.isFinite(parsedCreatedAt) && parsedCreatedAt > 0;
  return {
    id: message.id || id(),
    characterId: plain(message.characterId || message.character_id),
    role,
    content: renderBlocksText(blocks),
    contentBlocks: blocks,
    displayBlocks,
    communicationChannel: channel,
    expressionState: normalizeExpressionState(
      message.expressionState ?? message.expression_state,
      channel,
    ),
    stageMotion: normalizeStageMotion(message.stageMotion ?? message.stage_motion, channel),
    performanceId: normalizePerformanceId(message.performanceId ?? message.performance_id, channel),
    createdAt: hasCreatedAt ? parsedCreatedAt : Date.now(),
    createdAtEstimated: Boolean(message.createdAtEstimated || message.created_at_estimated || !hasCreatedAt),
    status: ["sent", "pending", "failed"].includes(message.status) ? message.status : "sent",
    requestId: plain(message.requestId || message.request_id),
    movementLocationId: plain(message.movementLocationId || message.movement_location_id),
    requestSnapshot: message.requestSnapshot && typeof message.requestSnapshot === "object"
      ? structuredClone(message.requestSnapshot)
      : null,
    errorCode: message.errorCode || "",
    partial: Boolean(message.partial),
    committedBlocks: Array.isArray(message.committedBlocks) ? message.committedBlocks : [],
    errorStage: normalizedErrorStage(message.errorStage || message.error_stage),
    source: message.source || "chat",
    evidenceSources: Array.isArray(message.evidenceSources || message.evidence_sources)
      ? (message.evidenceSources || message.evidence_sources).slice(0, 8).map(source => ({
        document_id: plain(source.document_id).slice(0, 160), title: plain(source.title).slice(0, 160), source_type: plain(source.source_type).slice(0, 160),
      })).filter(source => source.title || source.document_id) : [],
    conversationSegmentId: plain(message.conversationSegmentId || message.conversation_segment_id),
    usage: message.usage && typeof message.usage === "object" ? { ...message.usage } : null,
    movementStatus: message.movementStatus && typeof message.movementStatus === "object"
      ? structuredClone(message.movementStatus)
      : message.movement_status && typeof message.movement_status === "object"
        ? structuredClone(message.movement_status)
        : null,
  };
}
function normalizeThread(record, characterId) {
  const segmentId = plain(record?.conversationSegmentId) || id();
  const normalizedMessages = (record?.messages || []).map((message) => normalizeMessage({
    ...message,
    characterId: message.characterId || message.character_id || characterId,
    conversationSegmentId: message.conversationSegmentId || message.conversation_segment_id || segmentId,
  }));
  // Loaded history, including legacy embedded rows, is already established
  // content even when its timestamp is recent or has been estimated.
  normalizedMessages.forEach(markMessageEntrySeen);
  return {
    characterId,
    historyEpoch: state.historyEpoch,
    messages: normalizedMessages,
    summary: plain(record?.summary),
    channel: record?.channel === "in_person" ? "in_person" : "text",
    turnCount: Number(record?.turnCount || 0),
    summarizedThroughMessageId: plain(record?.summarizedThroughMessageId),
    summarizedThroughTurnCount: Number(record?.summarizedThroughTurnCount || 0),
    summaryRequestId: plain(record?.summaryRequestId),
    summaryCheckpointMessageId: plain(record?.summaryCheckpointMessageId),
    summaryUpdatedAt: Number(record?.summaryUpdatedAt || 0),
    conversationSegmentId: segmentId,
    localDayKey: plain(record?.localDayKey),
    continuityDecision: ["continue_previous", "start_today"].includes(record?.continuityDecision) ? record.continuityDecision : "",
    pendingTopics: Array.isArray(record?.pendingTopics) ? record.pendingTopics.map(plain).slice(0, 12) : [],
    lastActiveAt: Number(record?.lastActiveAt || 0),
    legacyStatePackage: plain(record?.statePackage),
    messageCount: Number(record?.messageCount || normalizedMessages.length),
    hasOlderMessages: Boolean(record?.hasOlderMessages),
  };
}
async function dbGetThread(characterId, initialLimit = 60) {
  if (state.threads.has(characterId)) {
    const cached = state.threads.get(characterId);
    if (initialLimit > cached.messages.length && cached.hasOlderMessages) {
      cached.messages = await loadMessagePage(characterId, { limit: initialLimit });
      cached.hasOlderMessages = cached.messages.length < Number(cached.messageCount || 0);
    }
    return cached;
  }
  const record = await storeGet("threads", characterId);
  const persistedCount = await messageCount(characterId);
  const storedMessages = persistedCount
    ? await loadMessagePage(characterId, { limit: initialLimit })
    : (record?.messages || []);
  const thread = normalizeThread({ ...record, messages: storedMessages, messageCount: persistedCount || storedMessages.length }, characterId);
  thread.hasOlderMessages = (persistedCount || storedMessages.length) > storedMessages.length;
  state.threads.set(characterId, thread);
  return thread;
}
async function dbPutThread(thread, { appState = [] } = {}) {
  if (state.writerReady && !state.writerOwned) return;
  if (thread.historyEpoch !== state.historyEpoch) return;
  const epoch = state.historyEpoch;
  state.threads.set(thread.characterId, thread);
  const metadata = {
    characterId: thread.characterId,
    summary: thread.summary,
    channel: thread.channel,
    turnCount: thread.turnCount,
    summarizedThroughMessageId: thread.summarizedThroughMessageId,
    summarizedThroughTurnCount: thread.summarizedThroughTurnCount,
    summaryRequestId: thread.summaryRequestId,
    summaryCheckpointMessageId: thread.summaryCheckpointMessageId,
    summaryUpdatedAt: thread.summaryUpdatedAt,
    conversationSegmentId: thread.conversationSegmentId,
    localDayKey: thread.localDayKey,
    continuityDecision: thread.continuityDecision,
    pendingTopics: thread.pendingTopics,
    lastActiveAt: thread.lastActiveAt,
    messageCount: Math.max(Number(thread.messageCount || 0), thread.messages.length),
  };
  state.memoryStores.threads.set(thread.characterId, structuredClone(metadata));
  const changes = thread.messages.map((message) => ({ message, signature: JSON.stringify(message) }))
    .filter(({ message, signature }) => state.persistedMessages.get(message.id) !== signature);
  for (const { message } of changes) state.memoryStores.messages.set(message.id, structuredClone(message));
  for (const entry of appState) state.memoryStores.app_state.set(entry.key, structuredClone(entry));
  const db = await openDB();
  if (epoch !== state.historyEpoch || thread.historyEpoch !== epoch) return;
  if (!db) {
    await storePut("threads", metadata);
    await Promise.all(changes.map(({ message }) => storePut("messages", message)));
    await Promise.all(appState.map(entry => storePut("app_state", entry)));
  } else {
    try {
      await new Promise((resolve, reject) => {
        const tx = db.transaction(["threads", "messages", ...(appState.length ? ["app_state"] : [])], "readwrite");
        tx.objectStore("threads").put(metadata);
        for (const { message } of changes) tx.objectStore("messages").put(message);
        for (const entry of appState) tx.objectStore("app_state").put(entry);
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error("indexeddb_write_aborted"));
      });
    } catch {
      useMemoryStorage();
    }
  }
  if (epoch === state.historyEpoch) for (const { message, signature } of changes) state.persistedMessages.set(message.id, signature);
  thread.messageCount = metadata.messageCount;
}

function surfaceHistory(surface = "timeline") {
  const messages = (currentThread()?.messages || []).filter(message => !state.readyReplyIds.has(message.id));
  return surface === "timeline" ? messages.filter(message => message.communicationChannel === "text") : messages;
}
async function loadOlderMessages(surface = "timeline") {
  const thread = currentThread();
  if (!thread?.messages.length || state.historyPageLoading) return;
  const root = $(surface === "timeline" ? "timeline" : "transcript-content");
  const limitKey = `${surface}VisibleLimit`;
  const endKey = `${surface}EndId`;
  const previousMessages = surfaceHistory(surface);
  const windowBefore = historyWindow(previousMessages, state[endKey], state[limitKey]);
  const anchorNode = root.querySelector("[data-message-id]");
  const anchorId = anchorNode?.dataset.messageId;
  const anchorTop = anchorNode?.getBoundingClientRect().top;
  if (windowBefore.start > 0) {
    state[endKey] = previousMessages[Math.max(state[limitKey], windowBefore.end - 40) - 1]?.id || "";
  } else if (thread.hasOlderMessages) {
    state.historyPageLoading = true;
    const loadButton = $(surface === "timeline" ? "load-older-messages" : "transcript-older");
    if (loadButton) loadButton.disabled = true;
    try {
      const oldest = thread.messages.reduce((a, b) => compareMessageCursor(a, b) < 0 ? a : b);
      const older = await loadMessagePage(thread.characterId, { before: oldest, limit: 40 });
      if (thread !== currentThread()) return;
      const known = new Set(thread.messages.map(message => message.id));
      const additions = older.filter(message => !known.has(message.id));
      thread.messages = [...additions, ...thread.messages];
      thread.hasOlderMessages = additions.length > 0 && thread.messages.length < Number(thread.messageCount || 0);
      const nextMessages = surfaceHistory(surface);
      const addedCount = nextMessages.length - previousMessages.length;
      state[limitKey] = Math.min(MAX_VISIBLE_MESSAGES, state[limitKey] + addedCount);
      const nextEnd = Math.min(state[limitKey], windowBefore.end + addedCount);
      state[endKey] = nextEnd < nextMessages.length ? nextMessages[nextEnd - 1]?.id || "" : "";
    } finally {
      state.historyPageLoading = false;
      if (loadButton) loadButton.disabled = false;
    }
  } else return;
  if (surface === "timeline") renderTimeline({ preserveScroll: true });
  else renderTranscript();
  const sameAnchor = [...root.querySelectorAll("[data-message-id]")].find(node => node.dataset.messageId === anchorId);
  if (sameAnchor && anchorTop !== undefined) root.scrollTop += sameAnchor.getBoundingClientRect().top - anchorTop;
}
function loadNewerMessages(surface = "timeline", latest = false) {
  const messages = surfaceHistory(surface);
  const windowBefore = historyWindow(messages, state[`${surface}EndId`], state[`${surface}VisibleLimit`]);
  const nextEnd = latest ? messages.length : Math.min(messages.length, windowBefore.end + 40);
  state[`${surface}EndId`] = nextEnd === messages.length ? "" : messages[nextEnd - 1]?.id || "";
  if (surface === "timeline") renderTimeline({ forceScroll: latest });
  else renderTranscript();
}

function decodeStatePackage(token) {
  try {
    const encoded = token.split(".", 1)[0].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded + "=".repeat((4 - encoded.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return {}; }
}
function dialogueV2Enabled() {
  return state.config?.capabilities?.dialogue_protocol === DIALOGUE_PROTOCOL
    && ["dialogue_domains_v2", "scene_actions_v2", "session_memory_v2"].some(featureEnabled);
}
function sceneActionsV2Enabled() {
  return dialogueV2Enabled() && featureEnabled("scene_actions_v2")
    && state.config?.capabilities?.scene_action_protocol === SCENE_PROTOCOL;
}
async function loadDialogueRecord(characterId) {
  if (!state.dialogueByCharacter.has(characterId)) {
    let stored = await storeGet("app_state", dialogueRecordKey(characterId));
    // An older v4 client can delete a character without knowing the sidecar
    // key. Do not revive experiences or a scene whose transcript was erased.
    if ((stored?.memories?.length || stored?.sessionContextPackage) && !(await messageCount(characterId))) {
      const snapshot = stored.sceneActionSnapshot;
      const metadata = snapshot ? await storeGet("threads", characterId) : null;
      const reconcilable = snapshot?.endpoint === "/public/v2/scene/action"
        && snapshot.envelope?.protocol === SCENE_PROTOCOL && snapshot.envelope.character_id === characterId
        && metadata?.conversationSegmentId === snapshot.envelope.segment_id;
      // A newly created empty thread can have its first join in flight. Its
      // real metadata distinguishes it from an older client's deletion; query
      // that immutable UUID before deciding whether a scene was established.
      if (!reconcilable) {
        stored = { ...stored, sessionContextPackage: "", memories: [], sceneActionSnapshot: null };
        if (state.writerOwned) await storePut("app_state", stored);
      }
    }
    state.dialogueByCharacter.set(characterId, stored || { key: dialogueRecordKey(characterId), characterId, sessionContextPackage: "", memories: [] });
  }
  return state.dialogueByCharacter.get(characterId);
}
function dialogueCandidates(characterId, text = "") {
  if (!featureEnabled("session_memory_v2")) return [];
  return relevantDialogueReceipts(state.dialogueByCharacter.get(characterId)?.memories || [], text);
}
function dialoguePackage(thread) {
  const token = state.dialogueByCharacter.get(thread.characterId)?.sessionContextPackage || "";
  const context = decodeSidecar(token);
  return context.segment_id === thread.conversationSegmentId && context.character_id === thread.characterId ? token : "";
}
async function commitDialogueResult(thread, result, owns = () => true) {
  if (!owns() || !state.writerOwned || thread.historyEpoch !== state.historyEpoch) return false;
  const token = plain(result.sessionContextPackage || result.session_context_package);
  const context = decodeSidecar(token);
  if (!token || token.length > 8192 || context.character_id !== thread.characterId || context.segment_id !== thread.conversationSegmentId) return false;
  const previous = await loadDialogueRecord(thread.characterId);
  if (!owns() || !state.writerOwned || thread.historyEpoch !== state.historyEpoch) return false;
  const before = decodeSidecar(previous.sessionContextPackage);
  if (before.segment_id === context.segment_id && (Number(before.scene_epoch) > Number(context.scene_epoch)
    || Number(before.revision) > Number(context.revision)
    || Number(before.revision) === Number(context.revision) && previous.sessionContextPackage !== token)) return false;
  const world = plain(result.statePackage || result.state_package);
  const accepted = world ? await saveWorldPackage(world, { persist: false }) : true;
  if (!accepted || !owns() || thread.historyEpoch !== state.historyEpoch || !state.writerOwned) return false;
  const record = { ...previous, key: dialogueRecordKey(thread.characterId), characterId: thread.characterId,
    sessionContextPackage: token, memories: mergeDialogueReceipts(previous.memories, result.sessionMemories || result.session_memories || []) };
  const appState = [record];
  if (world) { const order = statePackageOrder(world); appState.push({ key: "world", statePackage: world, revision: order.revision, scheduleDate: order.scheduleDate }); }
  await dbPutThread(thread, { appState });
  if (!owns() || thread.historyEpoch !== state.historyEpoch || !state.writerOwned) return false;
  state.dialogueByCharacter.set(thread.characterId, record);
  return true;
}
async function retireRejectedDialogueContext(error, thread, envelope, owns) {
  if (!owns() || error?.terminal || error?.errorStage !== "state_validation"
    || ![409, 422].includes(error.httpStatus)) return false;
  const contextErrors = ["session_context_invalid", "session_context_subject_mismatch", "session_context_conflict"];
  const record = await loadDialogueRecord(thread.characterId);
  if (!owns()) return false;
  if (contextErrors.includes(error.message)) {
    // A rejected historical recovery must not retire a newer active scene.
    if ((record.sessionContextPackage || "") !== (envelope?.session_context_package || "")) return false;
    const retired = { ...record, sessionContextPackage: "" };
    state.dialogueByCharacter.set(thread.characterId, retired);
    await storePut("app_state", retired);
    return true;
  }
  if (STATE_PACKAGE_RECOVERY_CODES.has(error.message)
    && state.worldPackage === (envelope?.chat?.state_package || envelope?.state_package)) {
    await clearPersistedWorldPackage();
    return true;
  }
  return false;
}
function statePackageOrder(token) {
  const decoded = typeof token === "string" ? decodeStatePackage(token) : (token || {});
  const scheduleDate = plain(decoded.schedule_date || decoded.world?.schedule_date || decoded.date).match(/^\d{4}-\d{2}-\d{2}$/)?.[0] || "";
  return { scheduleDate, revision: Number(decoded.revision || decoded.world?.revision || 0) };
}
async function saveWorldPackage(token, { persist = true } = {}) {
  if (!token) return false;
  const incoming = statePackageOrder(token);
  const current = statePackageOrder(state.worldPackage || "");
  // A slow response from a character the user has already left must not roll
  // the shared world back over a newer signed package. A revision identifies
  // exactly one signed payload; response order must not choose between two
  // different tokens carrying the same revision.
  if (state.worldPackage && incoming.scheduleDate && current.scheduleDate && incoming.scheduleDate < current.scheduleDate) return false;
  if (state.worldPackage && incoming.scheduleDate && current.scheduleDate && incoming.scheduleDate > current.scheduleDate) {
    state.worldPackage = token;
    if (persist) await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
    return true;
  }
  if (state.worldPackage && incoming.scheduleDate !== current.scheduleDate) {
    // A dated 0.9.2 package supersedes an undated legacy package. An undated
    // late response must never replace a dated package from the current day.
    if (!incoming.scheduleDate) return false;
    state.worldPackage = token;
    if (persist) await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
    return true;
  }
  if (state.worldPackage && incoming.revision < current.revision) return false;
  if (state.worldPackage && incoming.revision === current.revision) {
    return token === state.worldPackage;
  }
  state.worldPackage = token;
  if (persist) await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
  return true;
}
function statePackageRecoveryError(error) {
  const code = error instanceof Error ? error.message : plain(error);
  return STATE_PACKAGE_RECOVERY_CODES.has(code);
}
async function clearPersistedWorldPackage() {
  state.worldPackage = "";
  state.scene = null;
  state.sceneByCharacter.clear();
  for (const [characterId, record] of state.dialogueByCharacter) {
    const cleaned = { ...record, sessionContextPackage: "" };
    state.dialogueByCharacter.set(characterId, cleaned);
    await storePut("app_state", cleaned);
  }
  await storeDelete("app_state", "world");
  // v3 stored a signed world package inside each thread. The v4 migration
  // intentionally stopped writing it, but old metadata can still survive an
  // upgrade. Strip both spellings so a later boot cannot resurrect a token
  // that was rejected for a different anonymous subject.
  const threadRecords = await storeAll("threads");
  await Promise.all(threadRecords.map(async (record) => {
    const cleaned = { ...record };
    delete cleaned.statePackage;
    delete cleaned.legacyStatePackage;
    await storePut("threads", cleaned);
  }));
  for (const thread of state.threads.values()) thread.legacyStatePackage = "";
}
async function migrateBrowserState() {
  const preferences = await storeGet("app_state", "preferences");
  state.autoSummaryEnabled = preferences?.autoSummaryEnabled !== false;
  state.dayContinuityPreference = preferences?.dayContinuityPreference === "continue_previous" ? "continue_previous" : "start_today";
  const uiPreferences = await storeGet("app_state", "ui_preferences");
  state.pinnedCharacters = new Set(Array.isArray(uiPreferences?.pinnedCharacters) ? uiPreferences.pinnedCharacters.map(plain) : []);
  state.favoriteStickerIds = new Set(Array.isArray(uiPreferences?.favoriteStickerIds) ? uiPreferences.favoriteStickerIds.map(plain) : []);
  state.rendezvousDismissals = new Set(Array.isArray(uiPreferences?.rendezvousDismissals) ? uiPreferences.rendezvousDismissals.map(plain) : []);
  state.recentStickerIds = Array.isArray(uiPreferences?.recentStickerIds) ? uiPreferences.recentStickerIds.map(plain).slice(0, 24) : [];
  state.historyRetentionDays = [30, 90].includes(Number(uiPreferences?.historyRetentionDays)) ? Number(uiPreferences.historyRetentionDays) : 0;
  for (const sticker of Array.isArray(uiPreferences?.favoriteStickers) ? uiPreferences.favoriteStickers : []) {
    if (sticker?.asset_id) state.favoriteStickers.set(plain(sticker.asset_id), sticker);
  }
  const draftRecord = await storeGet("app_state", "drafts");
  state.drafts = new Map(Object.entries(draftRecord?.values || {}).map(([key, value]) => [key, plain(value)]));
  const recovery = await storeGet("app_state", "drafts_recovery_v1");
  if (recovery?.revision && draftRecord?.revision !== recovery.revision) {
    let recovered = false;
    for (const [key, value] of Object.entries(recovery.values || {})) {
      if (plain(value) && !state.drafts.get(key)) { state.drafts.set(key, plain(value)); recovered = true; }
    }
    if (recovered) showBanner("已找回旧版本覆盖的未发送草稿。请检查内容后继续。");
  }
  const saved = await storeGet("app_state", "world");
  if (saved?.statePackage) {
    state.worldPackage = saved.statePackage;
    return;
  }
  const records = await storeAll("threads");
  let best = { token: "", revision: -1 };
  for (const record of records) {
    const token = plain(record.statePackage);
    if (!token) continue;
    const revision = Number(decodeStatePackage(token).revision || 0);
    if (revision >= best.revision) best = { token, revision };
  }
  if (best.token) await saveWorldPackage(best.token);
}
async function storageBytes() {
  try {
    const estimate = await navigator.storage?.estimate?.();
    if (Number.isFinite(estimate?.usage)) return estimate.usage;
  } catch { /* storage estimate is optional */ }
  const payload = { threads: await storeAll("threads"), messages: await storeAll("messages"), appState: await storeAll("app_state") };
  return new Blob([JSON.stringify(payload)]).size;
}

async function saveConversationPreferences() {
  await storePut("app_state", {
    key: "preferences",
    autoSummaryEnabled: state.autoSummaryEnabled,
    dayContinuityPreference: state.dayContinuityPreference,
  });
}
async function saveUiPreferences() {
  await storePut("app_state", {
    key: "ui_preferences",
    pinnedCharacters: [...state.pinnedCharacters],
    favoriteStickerIds: [...state.favoriteStickerIds],
    favoriteStickers: [...state.favoriteStickers.values()].slice(0, 120),
    recentStickerIds: state.recentStickerIds.slice(0, 24),
    rendezvousDismissals: [...state.rendezvousDismissals].slice(-120),
    historyRetentionDays: state.historyRetentionDays,
  });
}

async function pruneExpiredMessages() {
  if (state.writerReady && !state.writerOwned) return;
  if (!state.historyRetentionDays) return;
  const cutoff = Date.now() - state.historyRetentionDays * 86400000;
  cancelBackgroundSummaries();
  state.historyEpoch += 1;
  for (const thread of state.threads.values()) {
    thread.messages = thread.messages.filter((message) => Number(message.createdAt || 0) >= cutoff);
    thread.historyEpoch = state.historyEpoch;
  }
  for (const [key, message] of state.memoryStores.messages) {
    if (Number(message.createdAt || 0) < cutoff) {
      state.memoryStores.messages.delete(key);
      state.persistedMessages.delete(key);
    }
  }
  let counts = new Map();
  const db = await openDB();
  if (db) {
    try { counts = await pruneHistoryDatabase(db, cutoff); }
    catch (error) {
      showBanner("自动清理未能保存，请导出备份后重试。现有记录不会静默删除。");
      throw error;
    }
  } else {
    for (const message of state.memoryStores.messages.values()) counts.set(message.characterId, (counts.get(message.characterId) || 0) + 1);
  }
  for (const [key, record] of state.memoryStores.threads) state.memoryStores.threads.set(key, { ...record, messageCount: counts.get(key) || 0 });
  for (const thread of state.threads.values()) {
    thread.messageCount = counts.get(thread.characterId) || 0;
    thread.hasOlderMessages = thread.messages.length < thread.messageCount;
  }
  for (const stored of await storeAll("app_state")) {
    if (!plain(stored.key).startsWith("dialogue_context_v1:")) continue;
    const cleaned = { ...stored, memories: (stored.memories || []).filter(receipt => {
      const occurred = Date.parse(receipt.memory?.occurred_at || "");
      return Number.isFinite(occurred) && occurred >= cutoff;
    }) };
    if (cleaned.memories.length !== (stored.memories || []).length) cleaned.sessionContextPackage = "";
    await storePut("app_state", cleaned);
    state.dialogueByCharacter.set(cleaned.characterId, cleaned);
  }
  if (state.selected) renderAll();
}

function draftKey(characterId = state.selected, channel = currentThread()?.channel || "text", field = "message") {
  return `${plain(characterId)}:${channel === "in_person" ? "in_person" : "text"}:${field}`;
}
async function persistDrafts() {
  if (state.writerReady && !state.writerOwned) return;
  const values = Object.fromEntries(state.drafts);
  const revision = id();
  for (const key of ["drafts", "drafts_recovery_v1"]) state.memoryStores.app_state.set(key, { key, values: structuredClone(values), revision });
  const db = await openDB();
  if (!db) return;
  try { await writeDraftsDatabase(db, values, revision); }
  catch { useMemoryStorage(); }
}
function captureComposerDraft() {
  if (!state.selected) return false;
  // Persistence can update the thread's channel before its visible composer.
  // Keep edits with the character and channel that still own these controls.
  const ownerKey = state.composerDraftKey || draftKey();
  state.drafts.set(ownerKey, $("message-input").value);
  state.drafts.set(ownerKey.replace(/:message$/, ":action"), $("action-input").value);
  return true;
}
async function saveDraftNow() {
  window.clearTimeout(state.draftTimer);
  if (!captureComposerDraft()) return;
  await persistDrafts();
}
function scheduleDraftSave() {
  captureComposerDraft();
  window.clearTimeout(state.draftTimer);
  state.draftTimer = window.setTimeout(() => { void saveDraftNow(); }, 220);
}
function restoreDraft() {
  if (!state.selected) return;
  const channel = currentThread()?.channel || "text";
  state.composerDraftKey = draftKey(state.selected, channel);
  $("message-input").value = state.drafts.get(draftKey(state.selected, channel, "message")) || "";
  $("action-input").value = state.drafts.get(draftKey(state.selected, channel, "action")) || "";
  updateInputCount();
}

function sessionKey() { return "project-snow-public:byok"; }
function saveCredential() {
  if (!state.credential || !state.credentialExpiresAt) return;
  tabPreferences.setItem(sessionKey(), JSON.stringify({ credential: state.credential, provider: state.provider, model: state.model, ...(state.provider === SUBSCRIPTION_PROVIDER ? { reasoningEffort: state.reasoningEffort } : {}), expiresAt: state.credentialExpiresAt }));
}
function clearCredential() {
  tabPreferences.removeItem(sessionKey());
  state.credential = "";
  state.credentialExpiresAt = 0;
  state.model = "";
  state.reasoningEffort = "";
  refreshCredentialStatus();
  updateComposerAvailability();
}
function configured() {
  if (state.credentialExpiresAt && state.credentialExpiresAt <= Date.now()) clearCredential();
  if (state.provider === SUBSCRIPTION_PROVIDER && (state.subscription.status !== "connected" || !subscriptionCombinationSupported(state.model, state.reasoningEffort))) return false;
  return Boolean(state.credential && state.provider && state.model);
}
function refreshCredentialStatus() {
  const active = Boolean(state.credential && state.credentialExpiresAt > Date.now() && state.provider === $("provider-select").value);
  $("credential-status").hidden = !active;
  if (active) {
    const expiration = new Date(state.credentialExpiresAt);
    const expires = localDayKey(expiration) === localDayKey()
      ? expiration.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : expiration.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    $("credential-status").textContent = state.provider === SUBSCRIPTION_PROVIDER
      ? `个人连接凭证有效至 ${expires}，仅保存在当前标签页。`
      : `加密凭证有效至 ${expires}；获取模型列表和切换角色无需重新输入 Key。`;
  }
}

async function waitForTurnstile(signal) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (window.turnstile) return window.turnstile;
    await abortableDelay(100, signal);
  }
  return null;
}
async function tokenForModel(signal) {
  const sitekey = state.config?.turnstile_site_key;
  if (!sitekey) return "development-bypass";
  const verification = $("model-verification");
  const container = $("model-turnstile");
  verification.hidden = false;
  $("model-verification-status").textContent = "正在加载人机验证…";
  container.setAttribute("aria-busy", "true");
  const turnstile = await waitForTurnstile(signal);
  signal.throwIfAborted();
  if (!turnstile) throw new Error("turnstile_unavailable");
  $("model-verification-status").textContent = "请在此完成验证，成功后会自动继续。";
  return new Promise((resolve, reject) => {
    let widget = null;
    let settled = false;
    let timeout = 0;
    const cleanup = () => {
      if (widget !== null) {
        try { turnstile.remove(widget); } catch { /* Still release the pending request. */ }
        widget = null;
      }
      container.replaceChildren();
      container.setAttribute("aria-busy", "false");
    };
    const settle = (error, token = "") => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", cancel);
      cleanup();
      if (error) reject(error);
      else resolve(token);
    };
    const cancel = () => settle(new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", cancel, { once: true });
    timeout = window.setTimeout(() => settle(new Error("turnstile_timeout")), 120000);
    try {
      // Native modal dialogs make body-level widgets inert and hide them behind
      // the backdrop. Keep the interactive challenge inside the model form.
      widget = turnstile.render(container, {
        sitekey, action: "byok-session", execution: "execute", appearance: "interaction-only",
        size: container.clientWidth < 300 ? "compact" : "normal",
        retry: "never",
        callback: (token) => settle(null, token),
        "error-callback": () => settle(new Error("turnstile_required")),
        "expired-callback": () => settle(new Error("turnstile_expired")),
        "timeout-callback": () => settle(new Error("turnstile_timeout")),
      });
      if (settled) cleanup();
      else {
        turnstile.execute(widget);
        if (!settled) verification.scrollIntoView({ block: "nearest" });
      }
    } catch { settle(new Error("turnstile_unavailable")); }
  });
}
let feedbackTurnstileWidget = null;
let feedbackTurnstileAbort = null;
function removeFeedbackTurnstileWidget() {
  if (feedbackTurnstileWidget !== null && window.turnstile) {
    try { window.turnstile.remove(feedbackTurnstileWidget); } catch { /* Widget may already be gone. */ }
  }
  feedbackTurnstileWidget = null;
  const container = $("feedback-turnstile");
  container?.replaceChildren();
  container?.setAttribute("aria-busy", "false");
}
function prepareFeedbackVerification({ cancelPending = false } = {}) {
  if (cancelPending && feedbackTurnstileAbort) {
    feedbackTurnstileAbort();
  } else {
    removeFeedbackTurnstileWidget();
  }
  feedbackTurnstileAbort = null;
  const verification = $("feedback-verification");
  if (!verification) return;
  verification.hidden = !state.config?.turnstile_site_key;
  $("feedback-verification-status").textContent = "提交时将在这里完成人机验证。";
  $("feedback-retry-verification").hidden = true;
}
async function tokenForFeedback() {
  const sitekey = state.config?.turnstile_site_key;
  if (!sitekey) return "development-bypass";
  const turnstile = await waitForTurnstile();
  if (!turnstile) throw new Error("turnstile_unavailable");
  removeFeedbackTurnstileWidget();
  const container = $("feedback-turnstile");
  if (!container) throw new Error("turnstile_unavailable");
  $("feedback-verification").hidden = false;
  $("feedback-verification-status").textContent = "请在此完成验证；验证成功后会自动提交。";
  $("feedback-retry-verification").hidden = true;
  container.setAttribute("aria-busy", "true");
  return new Promise((resolve, reject) => {
    let settled = false;
    const settle = (callback, value, status) => {
      if (settled) return;
      settled = true;
      feedbackTurnstileAbort = null;
      removeFeedbackTurnstileWidget();
      $("feedback-verification-status").textContent = status;
      callback(value);
    };
    feedbackTurnstileAbort = () => {
      const error = new Error("feedback_verification_cancelled");
      error.name = "AbortError";
      settle(reject, error, "提交已取消。再次提交时会重新验证。");
    };
    try {
      feedbackTurnstileWidget = turnstile.render(container, {
        sitekey,
        action: "feedback",
        execution: "execute",
        appearance: "interaction-only",
        callback: (token) => settle(resolve, token, "验证完成，正在提交…"),
        "error-callback": () => settle(reject, new Error("turnstile_required"), "验证失败，请重新验证。"),
        "expired-callback": () => settle(reject, new Error("turnstile_expired"), "验证已过期，请重新验证。"),
      });
      turnstile.execute(feedbackTurnstileWidget);
    } catch {
      settle(reject, new Error("turnstile_unavailable"), "验证组件加载失败，请重新验证。");
    }
  });
}
function attachTurnstile() {
  if (!state.config?.turnstile_site_key || document.querySelector("script[data-turnstile]")) return;
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  script.async = true;
  script.defer = true;
  script.dataset.turnstile = "true";
  document.head.append(script);
}

function experienceNoticeKey() {
  return `project-snow-public:notice:${state.config?.experience_notice_version || "0.9.2"}`;
}
function experienceNoticeAccepted() { return localPreferences.getItem(experienceNoticeKey()) === "accepted"; }
async function showExperienceNoticeIfNeeded() {
  if (experienceNoticeAccepted()) return;
  const dialog = $("experience-notice-dialog");
  await new Promise((resolve) => {
    let settled = false;
    const finish = async (accepted) => {
      if (settled) return;
      settled = true;
      if (accepted) {
        localPreferences.setItem(experienceNoticeKey(), "accepted");
        state.autoSummaryEnabled = $("notice-auto-summary").checked;
        await saveConversationPreferences();
      }
      if (dialog.open) dialog.close();
      resolve();
    };
    $("accept-experience-notice").onclick = () => finish(true);
    // There is deliberately no silent-dismiss path: an Escape key press must
    // not leave boot waiting forever or allow the user to enter without the
    // one-time product notice. Keep the dialog open until it is acknowledged.
    dialog.addEventListener("cancel", (event) => event.preventDefault());
    dialog.showModal();
  });
}

let subscriptionPollController = null;
let subscriptionPollTimer = 0;
let subscriptionPollSequence = 0;
function apiProviders() { return (state.config?.providers || []).filter((provider) => provider.provider_id !== SUBSCRIPTION_PROVIDER); }
function createSubscriptionState(overrides = {}) {
  return { status: "disconnected", credential: "", expiresAt: 0, pairingExpiresAt: 0, pairingCode: "", error: "", models: [], protocolVersion: 1, draftModel: "", draftEffort: "", selectionInitialized: false, selectionNotice: "", ...overrides };
}
function subscriptionCombinationSupported(model, effort) {
  return Boolean(state.subscription.models.find((item) => item.id === model)?.reasoning_efforts.includes(effort));
}
function subscriptionReasoningPayload() {
  return state.provider === SUBSCRIPTION_PROVIDER ? { reasoning_effort: state.reasoningEffort } : {};
}
function snapshotSubscriptionEffort(payload) {
  if (payload?.provider !== SUBSCRIPTION_PROVIDER) return "";
  return Object.hasOwn(payload, "reasoning_effort") ? plain(payload.reasoning_effort) : payload.model === SUBSCRIPTION_MODEL ? SUBSCRIPTION_EFFORT : "";
}
function subscriptionCatalogue(payload) {
  const version = Number(payload.protocol_version || 1);
  if (![1, 2].includes(version)) throw new Error("subscription_protocol_error");
  if (!Array.isArray(payload.models)) {
    if (version === 1 && payload.model === SUBSCRIPTION_MODEL && payload.effort === SUBSCRIPTION_EFFORT) {
      return [{ id: SUBSCRIPTION_MODEL, display_name: "GPT-5.6 Luna", reasoning_efforts: [SUBSCRIPTION_EFFORT], default_reasoning_effort: SUBSCRIPTION_EFFORT }];
    }
    throw new Error("subscription_model_unavailable");
  }
  const seen = new Set();
  return payload.models.flatMap((item) => {
    const model = plain(item?.id).trim();
    const efforts = [...new Set((Array.isArray(item?.reasoning_efforts) ? item.reasoning_efforts : []).filter((effort) => Object.hasOwn(SUBSCRIPTION_EFFORT_LABELS, effort)))];
    if (!model || model.length > 200 || seen.has(model) || !efforts.length) return [];
    if (version === 1 && (model !== SUBSCRIPTION_MODEL || !efforts.includes(SUBSCRIPTION_EFFORT))) return [];
    seen.add(model);
    const allowed = version === 1 ? [SUBSCRIPTION_EFFORT] : efforts;
    return [{ id: model, display_name: plain(item.display_name).trim() || model, reasoning_efforts: allowed,
      default_reasoning_effort: allowed.includes(item.default_reasoning_effort) ? item.default_reasoning_effort : allowed.length === 1 ? allowed[0] : "" }];
  });
}
function updateSubscriptionSelection(payload) {
  const subscription = state.subscription;
  subscription.models = subscriptionCatalogue(payload);
  subscription.protocolVersion = Number(payload.protocol_version || 1);
  if (!subscription.selectionInitialized) {
    subscription.selectionInitialized = true;
    if (state.provider === SUBSCRIPTION_PROVIDER && state.model) {
      subscription.draftModel = state.model;
      subscription.draftEffort = state.reasoningEffort;
    } else {
      const luna = subscription.models.find((item) => item.id === SUBSCRIPTION_MODEL);
      subscription.draftModel = luna?.id || "";
      subscription.draftEffort = luna?.reasoning_efforts.includes(SUBSCRIPTION_EFFORT) ? SUBSCRIPTION_EFFORT : luna?.default_reasoning_effort || "";
      subscription.selectionNotice = !luna ? "当前账号没有默认的 Luna Max，请明确选择一个可用模型。"
        : subscription.draftEffort !== SUBSCRIPTION_EFFORT ? `当前账号的 Luna 不支持 max，请确认所示默认强度 ${subscription.draftEffort || "（请选择）"}。` : "";
    }
  }
  if (subscription.draftModel && !subscriptionCombinationSupported(subscription.draftModel, subscription.draftEffort)) {
    if (!subscription.models.some((item) => item.id === subscription.draftModel)) subscription.draftModel = "";
    subscription.draftEffort = "";
    subscription.selectionNotice = "账号可用模型或推理强度已改变，请重新选择并保存。当前聊天配置不会自动替换。";
  }
}
function chooseSubscriptionModel(model) {
  const subscription = state.subscription;
  const selected = subscription.models.find((item) => item.id === model);
  subscription.draftModel = selected?.id || "";
  subscription.selectionNotice = "";
  if (!selected) subscription.draftEffort = "";
  else if (!selected.reasoning_efforts.includes(subscription.draftEffort)) {
    subscription.draftEffort = selected.default_reasoning_effort;
    subscription.selectionNotice = subscription.draftEffort ? `已显示该模型默认推理强度：${SUBSCRIPTION_EFFORT_LABELS[subscription.draftEffort]}（${subscription.draftEffort}）。保存后生效。` : "请为此模型选择推理强度。";
  }
  renderSubscriptionSettings();
}
function renderSubscriptionModelControls() {
  const subscription = state.subscription;
  const connected = subscription.status === "connected";
  $("subscription-model-controls").hidden = !connected;
  const model = subscription.models.find((item) => item.id === subscription.draftModel);
  const modelOptions = '<option value="">请选择模型</option>' + subscription.models.map((item) => {
    const label = subscription.models.some((other) => other.id !== item.id && other.display_name === item.display_name) ? `${item.display_name} · ${item.id}` : item.display_name;
    return `<option value="${escapeHtml(item.id)}">${escapeHtml(label)}</option>`;
  }).join("");
  const effortOptions = '<option value="">请选择推理强度</option>' + (model?.reasoning_efforts || []).map((effort) => `<option value="${effort}">${SUBSCRIPTION_EFFORT_LABELS[effort]} · ${effort}</option>`).join("");
  if ($("subscription-model-select").innerHTML !== modelOptions) $("subscription-model-select").innerHTML = modelOptions;
  if ($("subscription-effort-select").innerHTML !== effortOptions) $("subscription-effort-select").innerHTML = effortOptions;
  $("subscription-model-select").value = subscription.draftModel;
  $("subscription-effort-select").value = subscription.draftEffort;
  $("subscription-model-select").disabled = Boolean(modelSetupController);
  $("subscription-effort-select").disabled = Boolean(modelSetupController) || !model;
  $("subscription-model-name").textContent = connected ? model?.display_name || "选择订阅模型" : "默认 Luna Max";
  $("subscription-model-detail").textContent = connected ? model ? `${model.id} · ${subscription.draftEffort || "请选择强度"}` : "使用此账号支持的模型与推理强度" : `${SUBSCRIPTION_MODEL} · ${SUBSCRIPTION_EFFORT}`;
  const saved = state.provider === SUBSCRIPTION_PROVIDER && state.model === subscription.draftModel && state.reasoningEffort === subscription.draftEffort;
  $("subscription-selection-hint").textContent = subscription.selectionNotice || (saved ? "当前已保存此模型与推理强度。" : "选择会在点击“使用所选模型”后生效。");
}
function subscriptionEnabled() {
  const config = state.config?.subscription;
  return config?.enabled === true && config.model === SUBSCRIPTION_MODEL && config.effort === SUBSCRIPTION_EFFORT
    && [1, 2].includes(config.protocol_version) && state.config?.providers?.some((provider) => provider.provider_id === SUBSCRIPTION_PROVIDER);
}
function subscriptionSettingsVisible() {
  return $("settings-dialog").open && !$("settings-panel-models").hidden && state.modelSetupMode === "subscription" && !document.hidden;
}
function stopSubscriptionPolling() {
  subscriptionPollSequence += 1;
  window.clearTimeout(subscriptionPollTimer);
  subscriptionPollTimer = 0;
  subscriptionPollController?.abort();
  subscriptionPollController = null;
}
function storeSubscriptionCredential() {
  const { credential, expiresAt, pairingExpiresAt } = state.subscription;
  if (credential && expiresAt > Date.now()) tabPreferences.setItem(SUBSCRIPTION_SESSION_KEY, JSON.stringify({ credential, expiresAt, pairingExpiresAt }));
  else tabPreferences.removeItem(SUBSCRIPTION_SESSION_KEY);
}
function forgetSubscription(status = "disconnected", error = "") {
  const { draftModel, draftEffort } = state.subscription;
  state.subscription = createSubscriptionState({ status, error, draftModel, draftEffort, selectionInitialized: Boolean(draftModel) });
  tabPreferences.removeItem(SUBSCRIPTION_SESSION_KEY);
  if (state.provider === SUBSCRIPTION_PROVIDER) clearCredential();
}
function renderSubscriptionSettings() {
  const selected = state.modelSetupMode === "subscription";
  const enabled = subscriptionEnabled();
  const subscription = state.subscription;
  const busy = Boolean(modelSetupController);
  $("model-mode-api").setAttribute("aria-pressed", String(!selected));
  $("model-mode-subscription").setAttribute("aria-pressed", String(selected));
  $("api-model-settings").hidden = selected;
  $("subscription-settings").hidden = !selected;
  renderSubscriptionModelControls();
  const actions = $("model-session-actions");
  const subscriptionActions = $("subscription-session-actions");
  if (selected && actions.parentElement !== subscriptionActions) subscriptionActions.append(actions);
  else if (!selected && actions.parentElement === subscriptionActions) $("setup-error").after(actions);
  $("clear-credential").hidden = selected;
  $("save-model").textContent = selected ? "使用所选模型" : "保存本次模型会话";
  $("save-model").disabled = busy || (selected ? !enabled || subscription.status !== "connected" || !subscription.credential || !subscriptionCombinationSupported(subscription.draftModel, subscription.draftEffort) : !apiProviders().length);
  $("pair-subscription").hidden = !enabled || subscription.status === "connected";
  $("pair-subscription").disabled = busy;
  $("pair-subscription").textContent = subscription.credential ? "重新生成配对码" : "生成配对码";
  $("disconnect-subscription").hidden = !enabled || !subscription.credential;
  $("disconnect-subscription").disabled = busy;
  $("subscription-pairing").hidden = !subscription.pairingCode || subscription.status !== "waiting";
  $("subscription-pairing-code").textContent = subscription.pairingCode;
  $("subscription-pairing-expiry").textContent = subscription.pairingExpiresAt
    ? `有效至 ${new Date(subscription.pairingExpiresAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}。配对完成后此页会隐藏配对码。` : "";
  $("subscription-connect-command").textContent = `node snow-codex-connector.mjs --site ${window.location.origin}`;
  const statusCopy = {
    disconnected: "尚未连接个人连接器。请按下方教程完成登录和配对。",
    checking: "正在核对个人连接器状态……",
    waiting: "等待本机连接器配对。生成配对码还没有建立连接，完成教程后会自动更新。",
    connected: "已连接个人连接器。请选择模型与推理强度，再保存本次会话。",
  };
  $("subscription-status").dataset.status = !enabled ? "disabled" : subscription.error ? "error" : subscription.status;
  $("subscription-status").textContent = !enabled ? errorMessages.subscription_disabled
    : subscription.error ? displayError(subscription.error) : statusCopy[subscription.status] || statusCopy.disconnected;
}
function chooseModelMode(mode) {
  stopSubscriptionPolling();
  const select = $("provider-select");
  if (mode === "subscription") {
    if (select.value && select.value !== SUBSCRIPTION_PROVIDER) state.apiProviderChoice = select.value;
    state.modelSetupMode = "subscription";
    if (subscriptionEnabled()) select.value = SUBSCRIPTION_PROVIDER;
  } else {
    state.modelSetupMode = "api";
    select.value = state.apiProviderChoice || (state.provider !== SUBSCRIPTION_PROVIDER ? state.provider : "") || apiProviders()[0]?.provider_id || "";
  }
  syncProviderControls(select.value);
  showError("setup-error", "");
  renderSubscriptionSettings();
  if (mode === "subscription") startSubscriptionPolling();
}
async function refreshSubscriptionStatus(signal) {
  if (!subscriptionEnabled() || !state.subscription.credential) return;
  const payload = await api("/subscription/status", { signal, timeoutMs: 10000 });
  signal?.throwIfAborted();
  if (payload.status === "connected") {
    try { updateSubscriptionSelection(payload); }
    catch (error) { forgetSubscription("disconnected", error.message); throw error; }
    const expiresAt = Date.parse(payload.expires_at);
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) throw new Error("subscription_pairing_expired");
    state.subscription.status = "connected";
    state.subscription.error = "";
    state.subscription.pairingCode = "";
    state.subscription.pairingExpiresAt = 0;
    state.subscription.expiresAt = expiresAt;
    if (state.provider === SUBSCRIPTION_PROVIDER && state.model) {
      state.credentialExpiresAt = expiresAt;
      saveCredential();
    }
    storeSubscriptionCredential();
  } else if (payload.status === "waiting" && (state.subscription.pairingExpiresAt || state.subscription.expiresAt) > Date.now()) {
    state.subscription.status = "waiting";
    state.subscription.error = "";
  } else {
    forgetSubscription("disconnected", payload.status === "waiting" ? "subscription_pairing_expired" : "subscription_not_connected");
  }
  renderSubscriptionSettings();
  updateComposerAvailability();
}
function startSubscriptionPolling() {
  stopSubscriptionPolling();
  if (!subscriptionSettingsVisible() || !subscriptionEnabled() || !state.subscription.credential) return;
  const sequence = subscriptionPollSequence;
  const poll = async () => {
    if (sequence !== subscriptionPollSequence || !subscriptionSettingsVisible()) return;
    const controller = new AbortController();
    subscriptionPollController = controller;
    try { await refreshSubscriptionStatus(controller.signal); }
    catch (error) {
      if (controller.signal.aborted) return;
      state.subscription.error = error?.message === "credential_invalid" ? "subscription_not_connected" : (error?.message || "request_failed");
      if (["credential_invalid", "subscription_pairing_expired", "subscription_not_connected", "subscription_disconnected", "subscription_disabled"].includes(error?.message)) forgetSubscription("disconnected", state.subscription.error);
      renderSubscriptionSettings();
    } finally {
      if (subscriptionPollController === controller) subscriptionPollController = null;
      if (sequence === subscriptionPollSequence && subscriptionSettingsVisible() && state.subscription.credential) subscriptionPollTimer = window.setTimeout(poll, state.subscription.error ? 6000 : 3000);
    }
  };
  void poll();
}
async function pairSubscription() {
  stopSubscriptionPolling();
  await runModelSetup("pair-subscription", async (signal) => {
    if (!subscriptionEnabled()) throw new Error("subscription_disabled");
    if (!experienceNoticeAccepted()) throw new Error("experience_notice_required");
    const payload = await api("/subscription/pair", { method: "POST", signal, timeoutMs: 15000, body: JSON.stringify({ accepted_transit_notice: true, accepted_cost_notice: true, accepted_local_history_notice: true }) });
    signal.throwIfAborted();
    const expiresAt = Date.parse(payload.expires_at);
    const pairingExpiresAt = Date.parse(payload.pairing_expires_at || payload.expires_at);
    if (payload.provider !== SUBSCRIPTION_PROVIDER || !plain(payload.credential) || !plain(payload.pairing_code) || !Number.isFinite(expiresAt) || expiresAt <= Date.now() || !Number.isFinite(pairingExpiresAt) || pairingExpiresAt <= Date.now()) throw new Error("invalid_request");
    const draftModel = state.subscription.draftModel || (state.provider === SUBSCRIPTION_PROVIDER ? state.model : "");
    const draftEffort = state.subscription.draftEffort || (state.provider === SUBSCRIPTION_PROVIDER ? state.reasoningEffort : "");
    if (state.provider === SUBSCRIPTION_PROVIDER) clearCredential();
    state.subscription = createSubscriptionState({ status: "waiting", credential: plain(payload.credential), expiresAt, pairingExpiresAt, pairingCode: plain(payload.pairing_code), draftModel, draftEffort, selectionInitialized: Boolean(draftModel) });
    storeSubscriptionCredential();
  });
  renderSubscriptionSettings();
  startSubscriptionPolling();
}
async function disconnectSubscription() {
  stopSubscriptionPolling();
  await runModelSetup("disconnect-subscription", async (signal) => {
    await api("/subscription/disconnect", { method: "POST", signal, timeoutMs: 10000, body: "{}" });
    signal.throwIfAborted();
    forgetSubscription();
    toast("个人连接器已断开");
  });
  renderSubscriptionSettings();
}
function syncProviderControls(providerId = $("provider-select")?.value || "") {
  const select = $("provider-select");
  if (select) {
    if (providerId && select.value !== providerId) select.value = providerId;
    select.dataset.providerChoice = providerId;
  }
  document.querySelectorAll("[data-provider-option]").forEach((button) => {
    const selected = button.dataset.providerOption === providerId;
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected || (!providerId && button === $("provider-options-mobile")?.firstElementChild) ? 0 : -1;
  });
}
function chooseProvider(providerId) {
  const value = plain(providerId);
  if (!state.config?.providers?.some((provider) => provider.provider_id === value)) return;
  const select = $("provider-select");
  const previousChoice = plain(select.dataset.providerChoice || state.provider);
  const changed = Boolean(previousChoice && previousChoice !== value);
  state.apiProviderChoice = value;
  $("provider-select").value = value;
  syncProviderControls(value);
  if (changed) {
    clearCredential();
    state.model = "";
    $("model-id").value = "";
    $("discovered-models").innerHTML = "";
    $("discovered-models").hidden = true;
  }
  refreshCredentialStatus();
  renderSubscriptionSettings();
}
function renderMobileProviderOptions() {
  const root = $("provider-options-mobile");
  if (!root) return;
  root.innerHTML = apiProviders().map((provider) => `<button type="button" role="radio" data-provider-option="${escapeHtml(provider.provider_id)}" aria-checked="false" tabindex="-1">${escapeHtml(provider.display_name)}</button>`).join("");
  root.querySelectorAll("[data-provider-option]").forEach((button) => {
    button.onclick = () => chooseProvider(button.dataset.providerOption);
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const values = [...root.querySelectorAll("[data-provider-option]")];
      const current = values.indexOf(button);
      const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
      const target = event.key === "Home" ? 0 : event.key === "End" ? values.length - 1 : (current + direction + values.length) % values.length;
      values[target].focus();
      chooseProvider(values[target].dataset.providerOption);
    };
  });
  syncProviderControls($("provider-select").value);
}

async function loadConfig() {
  state.config = await api("/config", { headers: {} });
  syncMotionState();
  syncInterfaceSkin();
  // P6a 首帧处理：config 解析后解除中性保持（见模块顶部的 data-snow-booting 守卫）。
  // 并记录「上次见过雪境皮肤」的标记——仅当存在该标记时，下次才在 config 解析前保持中性，
  // 避免首访用户为了防闪而被挡一帧；关闭时清除标记。
  try {
    if (featureEnabled("ui_snow_adv")) localPreferences.setItem(SNOW_LAST_SEEN_KEY, "1");
    else localPreferences.removeItem(SNOW_LAST_SEEN_KEY);
  } catch { /* 存储不可用则下次按首访处理 */ }
  snowClearFirstFlashHold();
  $("version-badge").textContent = state.config.app_version || "0.9.6";
  $("github-link").href = state.config.source_links.project_snow;
  $("website-github-link").href = state.config.source_links.mywebsite;
  $("releases-link").href = state.config.source_links.releases;
  $("provider-select").innerHTML = state.config.providers.length
    ? state.config.providers.map((provider) => `<option value="${escapeHtml(provider.provider_id)}"${provider.provider_id === SUBSCRIPTION_PROVIDER ? " hidden" : ""}>${escapeHtml(provider.display_name)}</option>`).join("")
    : '<option value="">暂未开放模型厂商</option>';
  renderMobileProviderOptions();
  const providerLinks = [];
  for (const provider of state.config.providers || []) {
    const docs = safeExternalUrl(provider.documentation_url || provider.docs_url);
    const privacy = safeExternalUrl(provider.privacy_url);
    if (docs) providerLinks.push(`<a href="${escapeHtml(docs)}" target="_blank" rel="noreferrer">${escapeHtml(provider.display_name)} 官方文档</a>`);
    if (privacy) providerLinks.push(`<a href="${escapeHtml(privacy)}" target="_blank" rel="noreferrer">${escapeHtml(provider.display_name)} 隐私说明</a>`);
  }
  $("provider-doc-links").innerHTML = providerLinks.join("");
  $("provider-doc-links").hidden = !providerLinks.length;
  $("provider-empty").hidden = Boolean(apiProviders().length);
  $("discover-models").disabled = !state.config.providers.length;
  $("save-model").disabled = !state.config.providers.length;
  if (!state.config.providers.length) showError("setup-error", "当前暂未开放可用的模型厂商。");
  attachTurnstile();
  const stored = tabPreferences.getItem(sessionKey());
  if (stored) {
    try {
      const saved = JSON.parse(stored);
      const available = state.config.providers.some((item) => item.provider_id === saved.provider);
      if (saved.expiresAt > Date.now() && available) {
        state.credential = saved.credential;
        state.credentialExpiresAt = saved.expiresAt;
        state.provider = saved.provider;
        state.model = saved.model || "";
        state.reasoningEffort = saved.provider === SUBSCRIPTION_PROVIDER ? plain(saved.reasoningEffort) || (saved.model === SUBSCRIPTION_MODEL ? SUBSCRIPTION_EFFORT : "") : "";
        if (saved.provider === SUBSCRIPTION_PROVIDER) state.modelSetupMode = "subscription";
        $("provider-select").value = state.provider;
        syncProviderControls(state.provider);
        $("model-id").value = state.model;
      } else clearCredential();
    } catch { clearCredential(); }
  }
  try {
    const pending = JSON.parse(tabPreferences.getItem(SUBSCRIPTION_SESSION_KEY) || "null");
    const credential = state.provider === SUBSCRIPTION_PROVIDER ? state.credential : plain(pending?.credential);
    const expiresAt = state.provider === SUBSCRIPTION_PROVIDER ? state.credentialExpiresAt : Number(pending?.expiresAt);
    if (subscriptionEnabled() && credential && expiresAt > Date.now()) {
      state.subscription = createSubscriptionState({ status: "checking", credential, expiresAt, pairingExpiresAt: Number(pending?.pairingExpiresAt) || 0 });
      await refreshSubscriptionStatus();
    } else if (credential) forgetSubscription();
  } catch (error) {
    state.subscription.status = "disconnected";
    state.subscription.error = error?.message || "subscription_not_connected";
  }
  state.apiProviderChoice = (state.provider !== SUBSCRIPTION_PROVIDER ? state.provider : "") || apiProviders()[0]?.provider_id || "";
  refreshCredentialStatus();
  syncProviderControls($("provider-select").value);
  renderSubscriptionSettings();
}
function normalizeSticker(sticker) {
  if (!sticker?.asset_id) return null;
  return {
    ...sticker,
    asset_id: plain(sticker.asset_id),
    caption: plain(sticker.caption || "表情"),
    src: plain(sticker.src),
    display_src: plain(sticker.display_src || sticker.displaySrc),
    thumbnail_src: plain(sticker.thumbnail_src || sticker.thumbnailSrc),
    display_animated: Boolean(sticker.display_animated ?? sticker.displayAnimated ?? sticker.animated),
    emotion_tags: Array.isArray(sticker.emotion_tags) ? sticker.emotion_tags.map(plain) : [],
    character_ids: Array.isArray(sticker.character_ids) ? sticker.character_ids.map(plain) : [],
  };
}
function localStickerSectionValues(section = state.stickerSection) {
  const ids = section === "recent" ? state.recentStickerIds : [...state.favoriteStickerIds];
  return ids.map((assetId) => state.stickerCatalog.get(assetId) || state.favoriteStickers.get(assetId)).filter(Boolean);
}
function stickerMatchesLoadedFilter(sticker) {
  const query = normalizedSearch(state.stickerQuery);
  if (query) {
    const tokens = [sticker.caption, sticker.category, ...(sticker.emotion_tags || [])].map(normalizedSearch);
    if (!tokens.some((token) => token.includes(query))) return false;
  }
  if (state.stickerSection === "character" && sticker.character_ids?.length && !sticker.character_ids.includes(state.selected)) return false;
  if (state.stickerSection === "generic" && sticker.character_ids?.length) return false;
  return true;
}
async function loadStickers({ reset = false } = {}) {
  if (["recent", "favorites"].includes(state.stickerSection)) {
    state.stickers = localStickerSectionValues().filter(stickerMatchesLoadedFilter);
    state.stickerCursor = null;
    state.stickerHasMore = false;
    renderStickerPicker();
    return state.stickers;
  }
  if (reset) {
    state.stickers = [];
    state.stickerCursor = null;
    state.stickerHasMore = true;
  }
  if (!state.stickerHasMore && !reset) return state.stickers;
  const requestKey = `${state.stickerSection}:${state.selected}:${state.stickerQuery}:${plain(state.stickerCursor)}`;
  if (state.stickerLoadPromise) return state.stickerLoadPromise;
  const params = new URLSearchParams({ limit: "40" });
  if (state.stickerCursor !== null && state.stickerCursor !== "") params.set("cursor", plain(state.stickerCursor));
  if (state.stickerQuery) params.set("q", state.stickerQuery);
  if (state.stickerSection === "character" && state.selected) params.set("character_id", state.selected);
  if (state.stickerSection === "generic") params.set("candidate_scope", "generic");
  state.stickerLoadPromise = (async () => {
    try {
      const payload = await api(`/stickers?${params}`, { headers: {} });
      if (requestKey !== `${state.stickerSection}:${state.selected}:${state.stickerQuery}:${plain(state.stickerCursor)}`) return state.stickers;
      const incoming = (Array.isArray(payload.stickers) ? payload.stickers : []).map(normalizeSticker).filter(Boolean);
      for (const sticker of incoming) state.stickerCatalog.set(sticker.asset_id, sticker);
      const seen = new Set(state.stickers.map((item) => item.asset_id));
      state.stickers.push(...incoming.filter((item) => !seen.has(item.asset_id) && seen.add(item.asset_id) && stickerMatchesLoadedFilter(item)));
      state.stickerCursor = payload.next_cursor ?? null;
      state.stickerHasMore = state.stickerCursor !== null && state.stickerCursor !== undefined && state.stickerCursor !== "";
      return state.stickers;
    } catch {
      if (!state.stickers.length) state.stickerHasMore = false;
      return state.stickers;
    } finally {
      state.stickerLoadPromise = null;
      renderStickerPicker();
    }
  })();
  renderStickerPicker();
  return state.stickerLoadPromise;
}
let modelSetupController = null;
async function runModelSetup(action, task) {
  if (modelSetupController) return;
  const controller = new AbortController();
  modelSetupController = controller;
  const form = document.querySelector(".provider-form");
  const controls = [...form.querySelectorAll("button, input, select")].map((control) => [control, control.disabled]);
  const button = $(action);
  const label = button.textContent;
  controls.forEach(([control]) => { control.disabled = true; });
  form.setAttribute("aria-busy", "true");
  button.textContent = ({ "discover-models": "正在获取模型列表…", "pair-subscription": "正在生成配对码…", "disconnect-subscription": "正在断开连接…" })[action] || "正在保存模型会话…";
  showError("setup-error", "");
  try {
    await task(controller.signal);
  } catch (error) {
    if (error?.name !== "AbortError") {
      showError("setup-error", error?.message === "request_timeout" ? "模型配置请求超时，请再次点击按钮重试。" : error);
    }
  } finally {
    controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    button.textContent = label;
    form.setAttribute("aria-busy", "false");
    $("model-verification").hidden = true;
    $("model-turnstile").setAttribute("aria-busy", "false");
    modelSetupController = null;
    renderSubscriptionSettings();
  }
}
async function issueCredential(signal) {
  const provider = $("provider-select").value;
  if (!provider || !state.config?.providers?.some((item) => item.provider_id === provider)) {
    throw new Error("provider_not_enabled");
  }
  const apiKey = $("api-key").value;
  if (!apiKey) throw new Error("请先输入 API Key。");
  if (!experienceNoticeAccepted()) {
    await showExperienceNoticeIfNeeded();
    if (!experienceNoticeAccepted()) throw new Error("experience_notice_required");
  }
  signal.throwIfAborted();
  const token = await tokenForModel(signal);
  signal.throwIfAborted();
  $("model-verification-status").textContent = "验证完成，正在建立模型会话…";
  const payload = await api("/byok/session", {
    method: "POST",
    signal,
    timeoutMs: 30000,
    body: JSON.stringify({
      provider,
      api_key: apiKey,
      turnstile_token: token,
      accepted_transit_notice: true,
      accepted_cost_notice: true,
      accepted_local_history_notice: true,
    }),
  });
  signal.throwIfAborted();
  state.credential = payload.credential;
  state.provider = provider;
  state.credentialExpiresAt = Date.parse(payload.expires_at);
  $("api-key").value = "";
  saveCredential();
  refreshCredentialStatus();
}
async function discoverModels() {
  return runModelSetup("discover-models", async (signal) => {
    if (state.modelSetupMode === "subscription") throw new Error("subscription_model_unavailable");
    if (!$("provider-select").value) throw new Error("provider_not_enabled");
    if (!state.credential || state.provider !== $("provider-select").value || state.credentialExpiresAt <= Date.now()) await issueCredential(signal);
    const payload = await api("/byok/models", { method: "POST", signal, timeoutMs: 30000, body: JSON.stringify({ provider: state.provider, credential: state.credential, request_id: id() }) });
    signal.throwIfAborted();
    saveCredential();
    const select = $("discovered-models");
    select.innerHTML = `<option value="">选择已发现模型</option>${payload.models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")}`;
    select.hidden = false;
  });
}
async function saveModelSession() {
  return runModelSetup("save-model", async (signal) => {
    if (state.modelSetupMode === "subscription") {
      if (!subscriptionEnabled()) throw new Error("subscription_disabled");
      await refreshSubscriptionStatus(signal);
      signal.throwIfAborted();
      if (state.subscription.status !== "connected" || !state.subscription.credential) throw new Error("subscription_not_connected");
      if (!subscriptionCombinationSupported(state.subscription.draftModel, state.subscription.draftEffort)) throw new Error("subscription_model_unavailable");
      cancelBackgroundSummaries();
      state.provider = SUBSCRIPTION_PROVIDER;
      state.model = state.subscription.draftModel;
      state.reasoningEffort = state.subscription.draftEffort;
      state.credential = state.subscription.credential;
      state.credentialExpiresAt = state.subscription.expiresAt;
      saveCredential();
      refreshCredentialStatus();
      updateComposerAvailability();
      $("settings-dialog").close();
      toast(`已使用你自己的 Codex 订阅 · ${state.model} · ${state.reasoningEffort}`);
      return;
    }
    const model = $("discovered-models").value.trim() || $("model-id").value.trim();
    if (!model) throw new Error("请填写或选择模型 ID。");
    if (!$("provider-select").value) throw new Error("provider_not_enabled");
    if (!state.credential || state.provider !== $("provider-select").value || state.credentialExpiresAt <= Date.now()) await issueCredential(signal);
    signal.throwIfAborted();
    state.provider = $("provider-select").value;
    state.model = model;
    state.reasoningEffort = "";
    saveCredential();
    refreshCredentialStatus();
    updateComposerAvailability();
    $("settings-dialog").close();
    toast("模型会话已保存到当前标签页");
  });
}

function currentCharacter() { return state.characters.find((item) => item.character_id === state.selected) || null; }
function currentThread() { return state.threads.get(state.selected) || null; }
async function ensureContinuityDecision(thread) {
  if (!thread) return true;
  const today = localDayKey();
  if (thread.localDayKey && thread.localDayKey !== today) {
    thread.continuityDecision = state.dayContinuityPreference;
    if (thread.continuityDecision === "start_today") {
      // Keep the local transcript and world state, but retire yesterday's
      // model context, including any summary that has not finished yet.
      state.summaryControllers.get(thread.characterId)?.abort();
      state.summaryControllers.delete(thread.characterId);
      state.summaryInFlight.delete(thread.characterId);
      thread.conversationSegmentId = newConversationSegment();
      thread.summary = "";
      thread.pendingTopics = [];
      thread.summaryRequestId = "";
      thread.summaryCheckpointMessageId = "";
      thread.summarizedThroughMessageId = "";
      thread.summarizedThroughTurnCount = 0;
      thread.summaryUpdatedAt = 0;
    }
  }
  thread.localDayKey = today;
  thread.lastActiveAt = Date.now();
  await dbPutThread(thread);
  return true;
}
function avatarMarkup(character, { thumbnail = true, priority = false, className = "" } = {}) {
  const avatar = CHARACTER_AVATAR_OVERRIDES[character?.character_id] || character?.avatar || null;
  const src = avatar ? (thumbnail ? avatar.thumbnail_src : avatar.src) : "";
  const focus = avatar ? ` style="--portrait-focus-x:${Number(avatar.portrait_focus_x || 50)}%;--portrait-focus-y:${Number(avatar.portrait_focus_y || 50)}%;--portrait-scale:${Number(avatar.portrait_scale || 1)}"` : "";
  const image = src ? `<img src="${escapeHtml(src)}" alt="" loading="${priority ? "eager" : "lazy"}" decoding="async"${priority ? " fetchpriority=\"high\"" : ""} />` : "";
  return `<span class="portrait portrait-text ${className}${src ? " has-image" : ""}"${focus}><span class="portrait-fallback">${escapeHtml(character?.display_name?.slice(0, 1) || "?")}</span>${image}</span>`;
}
function analystAvatarMarkup({ thumbnail = true, priority = false, className = "" } = {}) {
  const avatar = state.config?.analyst_avatar || null;
  const src = avatar ? (thumbnail ? avatar.thumbnail_src : avatar.src) : "";
  const focus = avatar ? ` style="--portrait-focus-x:${Number(avatar.portrait_focus_x || 50)}%;--portrait-focus-y:${Number(avatar.portrait_focus_y || 50)}%;--portrait-scale:${Number(avatar.portrait_scale || 1)}"` : "";
  const image = src ? `<img src="${escapeHtml(src)}" alt="" loading="${priority ? "eager" : "lazy"}" decoding="async"${priority ? " fetchpriority=\"high\"" : ""} />` : "";
  return `<span class="portrait analyst-portrait ${className}${src ? " has-image" : ""}" aria-label="分析员头像"${focus}>${image}</span>`;
}
function characterById(characterId) {
  return state.characters.find((item) => item.character_id === characterId) || null;
}
function typingStateFor(characterId = state.selected) {
  return characterId ? state.typingByCharacter.get(characterId) || null : null;
}
function globalRequestBusy() {
  return Boolean(
    state.chatRequestByCharacter.size
    || [...state.typingByCharacter.values()].some(pending => !pending.networkDone)
    || state.arrivalPending
    || state.modeTransitionPending
    || state.presenceResolvePending,
  );
}
function presentationFor(characterId = state.selected) {
  return characterId ? state.presentationByCharacter.get(characterId) || null : null;
}
function cancelPresentationQueue(characterId = state.selected, requestId = "", { render = true } = {}) {
  const ready = state.completedPresentationByCharacter.get(characterId);
  if (ready && (!requestId || ready.requestId === requestId)) {
    state.readyReplyIds.delete(ready.messageId);
    ready.controller.abort();
    state.completedPresentationByCharacter.delete(characterId);
  }
  const queue = presentationFor(characterId);
  if (!queue) {
    cancelStageMotion(characterId);
    return false;
  }
  if (requestId && queue.requestId !== requestId) return false;
  queue.cancelled = true;
  queue.controller?.abort();
  if (queue.timer) window.clearTimeout(queue.timer);
  cancelStageMotion(characterId);
  state.presentationByCharacter.delete(characterId);
  if (render && characterId === state.selected) {
    window.clearTimeout(state.typewriter.timer);
    state.typewriter = { key: "", timer: 0, fullText: "", displayedText: "" };
    renderTimeline();
    renderStage();
  }
  return true;
}
function naturalSpeechParts(value) {
  const text = plain(value).trim();
  if (!text) return [];
  const parts = [];
  let start = 0;
  const closers = new Set(["”", "’", "」", "』", "】", "》", "）", ")"]);
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    let boundary = /[。！？!?]/.test(character);
    if (character === "…" && text[index + 1] === "…") {
      index += 1;
      boundary = true;
    }
    if (!boundary) continue;
    while (index + 1 < text.length && closers.has(text[index + 1])) index += 1;
    const part = text.slice(start, index + 1).trim();
    if (part) parts.push(part);
    start = index + 1;
  }
  const rest = text.slice(start).trim();
  if (rest) parts.push(rest);
  if (parts.length < 2) return parts.length ? parts : [text];
  const merged = [];
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if ([...part].length < 8 && index + 1 < parts.length) {
      parts[index + 1] = `${part}${parts[index + 1]}`;
    } else if ([...part].length < 8 && merged.length) {
      merged[merged.length - 1] += part;
    } else {
      merged.push(part);
    }
  }
  return merged;
}
function expandedPresentationBlocks(sourceBlocks) {
  const blocks = [];
  for (const block of sourceBlocks || []) {
    if (!block) continue;
    if (block.type === "sticker") {
      blocks.push({ ...block });
      continue;
    }
    const paragraphs = plain(block.text).split(/(?:\r?\n){2,}/).map((text) => text.trim()).filter(Boolean);
    for (const paragraph of paragraphs.length ? paragraphs : [plain(block.text).trim()]) {
      const parts = ["message", "speech"].includes(block.type) ? naturalSpeechParts(paragraph) : [paragraph];
      for (const text of parts) {
        if (text) blocks.push({ type: block.type, text });
      }
    }
  }
  return blocks;
}
function deriveDisplayBlocks(sourceBlocks, channel = "text") {
  const blocks = expandedPresentationBlocks(sourceBlocks);
  while (blocks.length > 4) {
    let mergeAt = -1;
    let shortest = Number.POSITIVE_INFINITY;
    for (let index = 0; index < blocks.length - 1; index += 1) {
      const left = blocks[index];
      const right = blocks[index + 1];
      if (left.type === "sticker" || right.type === "sticker" || left.type !== right.type) continue;
      const length = [...plain(left.text)].length + [...plain(right.text)].length;
      if (length < shortest) { shortest = length; mergeAt = index; }
    }
    if (mergeAt < 0) break;
    const left = blocks[mergeAt];
    const right = blocks[mergeAt + 1];
    const separator = left.type === right.type && channel === "text" ? "" : "\n";
    blocks.splice(mergeAt, 2, { type: left.type, text: `${plain(left.text)}${separator}${plain(right.text)}`.trim() });
  }
  return blocks;
}
function presentationBlocks(message) {
  if (message?.committedBlocks?.length) return message.contentBlocks || [];
  if (message?.communicationChannel === "in_person") {
    return message?.contentBlocks?.length ? message.contentBlocks : message?.displayBlocks || [];
  }
  return deriveDisplayBlocks(message?.displayBlocks?.length ? message.displayBlocks : message?.contentBlocks, message?.communicationChannel || "text");
}
function inPersonSurfaceText(blocks, type) {
  const values = (blocks || []).map((block, index) => ({
    index,
    type: block?.type,
    text: plain(block?.text).trim(),
  })).filter((block) => block.type === type && block.text);
  if (type === "action") return values.map((block) => block.text).join("\n");
  let previous = null;
  return values.reduce((combined, block) => {
    if (!previous) {
      previous = block;
      return block.text;
    }
    let separator = "\n";
    if (/\s$/.test(combined) || /^\s/.test(block.text)) separator = "";
    else if (block.index === previous.index + 1) {
      if (/[。！？…]$/.test(previous.text)) separator = /^[A-Za-z0-9]/.test(block.text) ? " " : "";
      else if (/[.!?]$/.test(previous.text)) separator = " ";
      else if (/[A-Za-z0-9][,;:]?$/.test(previous.text) && /^[A-Za-z0-9]/.test(block.text)) separator = " ";
    }
    previous = block;
    return `${combined}${separator}${block.text}`;
  }, "");
}
function visibleBlocksFor(message) {
  const queue = message ? presentationFor(message.characterId || state.selected) : null;
  if (queue && queue.messageId === message?.id) return queue.blocks.slice(0, queue.visibleCount);
  const ready = state.completedPresentationByCharacter.get(message?.characterId);
  if (ready?.messageId === message?.id) return presentationBlocks(message).slice(0, ready.visibleCount);
  const streaming = state.streamingPresentationByCharacter.get(message?.characterId);
  if (streaming?.requestId === message?.requestId) return presentationBlocks(message).slice(0, streaming.visibleCount);
  if (message?.communicationChannel === "in_person") {
    return message?.contentBlocks?.length ? message.contentBlocks : message?.displayBlocks || [];
  }
  return message?.displayBlocks?.length ? message.displayBlocks : message?.contentBlocks || [];
}
function speechTypewriterKey(message) {
  return [
    plain(message?.characterId),
    plain(message?.requestId),
    plain(message?.id),
    "complete-speech",
  ].join(":");
}
function timelineTypingMarkup(characterId = state.selected) {
  const pending = typingStateFor(characterId);
  if (!pending || pending.channel !== "text" || !["typing", "segment"].includes(pending.phase)) return "";
  const character = characterById(pending.characterId);
  return `<article class="message assistant timeline-typing-row" aria-label="${escapeHtml(character?.display_name || "角色")}正在输入"><div class="message-avatar">${avatarMarkup(character, { thumbnail: true, priority: true })}</div><div class="message-body">${typingIndicatorMarkup(character, { includeAvatar: false })}</div></article>`;
}
function stickerDisplaySource(block) {
  return plain(block?.displaySrc || block?.display_src || block?.thumbnailSrc || block?.thumbnail_src || block?.src);
}
async function preloadStickerBlock(block, timeoutMs = 900, signal = undefined) {
  const src = stickerDisplaySource(block);
  if (!src) return;
  const loading = new Promise((resolve) => {
    const image = new Image();
    image.onload = async () => {
      try { if (image.decode) await image.decode(); } catch { /* decoded fallback remains usable */ }
      resolve();
    };
    image.onerror = resolve;
    image.src = src;
  });
  await Promise.race([loading, abortableDelay(timeoutMs, signal)]);
}
async function presentAssistantTurn(characterId, requestId, message, { visibleCount = 0 } = {}) {
  if (!ownsTypingState(characterId, requestId)) return false;
  const blocks = presentationBlocks(message);
  const inPerson = message.communicationChannel === "in_person";
  cancelPresentationQueue(characterId, "", { render: false });
  if (!blocks.length || !ownsTypingState(characterId, requestId)) return false;
  const queue = {
    characterId,
    requestId,
    messageId: message.id,
    blocks,
    visibleCount: inPerson ? blocks.length : Math.max(1, Math.min(blocks.length, visibleCount)),
    timer: 0,
    cancelled: false,
    controller: new AbortController(),
    skip: false,
  };
  const ownsPresentation = () => !queue.cancelled && state.presentationByCharacter.get(characterId) === queue
    && (!typingStateFor(characterId) || ownsTypingState(characterId, requestId));
  const revealCounts = blocks.length <= 4
    ? blocks.map((_block, index) => index + 1)
    : [1, 2, 3, blocks.length];
  state.presentationByCharacter.set(characterId, queue);
  updateTypingPhase(characterId, requestId, "presenting");
  syncFastForward();
  if (characterId === state.selected) {
    renderTimeline();
    renderStage();
  }
  try {
    if (inPerson) {
      const completeSpeech = inPersonSurfaceText(blocks, "speech");
      if (characterId === state.selected && completeSpeech) {
        await waitForTypewriterCompletion(
          speechTypewriterKey(message),
          completeSpeech,
          queue.controller.signal,
        );
      }
    } else {
      const metrics = replyTiming(requestId);
      let spent = metrics?.segmentSpent || 0;
      for (let step = 1; step < revealCounts.length && queue.visibleCount < blocks.length; step += 1) {
        if (!ownsPresentation()) return false;
        const previousCount = revealCounts[step - 1];
        const visibleCount = revealCounts[step];
        if (visibleCount <= queue.visibleCount) continue;
        const revealed = blocks.slice(previousCount, visibleCount);
        const hasSticker = revealed.some((item) => item.type === "sticker");
        const previousTextLength = [...plain(blocks[Math.max(0, previousCount - 1)]?.text)].length;
        const totalWait = featureEnabled("chat_latency_v2") ? segmentWait(previousTextLength, spent, quietPresentation() || queue.skip) : hasSticker
          ? requestDelay(requestId, `sticker:${step}`, 2200, 3000)
          : Math.max(2800, Math.min(4200, 2200 + previousTextLength * 35));
        const readingWait = featureEnabled("chat_latency_v2") ? 0 : Math.min(totalWait, requestDelay(requestId, `reading:${step}`, 800, 1400));
        updateTypingPhase(characterId, requestId, "reading");
        if (!reducedMotion()) await abortableDelay(readingWait, queue.controller.signal);
        if (!ownsPresentation()) return false;
        updateTypingPhase(characterId, requestId, "segment");
        const preload = hasSticker
          ? Promise.all(revealed.filter((item) => item.type === "sticker").map((item) => preloadStickerBlock(item, 1200, queue.controller.signal)))
          : Promise.resolve();
        if (featureEnabled("chat_latency_v2")) {
          // Artwork decoding never holds back already available text.
          void preload.catch(() => {});
          const began = performance.now();
          const wait = Math.max(0, totalWait - (metrics?.lastBodyPaintAt ? began - metrics.lastBodyPaintAt : 0));
          while (!queue.skip && !quietPresentation() && performance.now() - began < wait) {
            await abortableDelay(Math.min(32, wait - (performance.now() - began)), queue.controller.signal);
          }
          if (metrics) metrics.naturalWaitMs += performance.now() - began;
          spent += totalWait;
          if (metrics) metrics.segmentSpent = spent;
        } else if (!reducedMotion()) await Promise.all([abortableDelay(totalWait - readingWait, queue.controller.signal), preload]);
        else await preload;
        if (!ownsPresentation()) return false;
        queue.visibleCount = queue.skip ? blocks.length : visibleCount;
        if (metrics) metrics.lastBodyPaintAt = performance.now();
        updateTypingPhase(characterId, requestId, "presenting");
        if (characterId === state.selected) {
          renderTimeline();
          renderStage();
        }
      }
    }
  } catch (error) {
    if (error?.name === "AbortError") return false;
    throw error;
  }
  if (characterId === state.selected) {
    renderTimeline();
    renderStage();
  }
  if (state.presentationByCharacter.get(characterId) === queue) {
    state.presentationByCharacter.delete(characterId);
  }
  syncFastForward();
  if (characterId === state.selected) {
    renderTimeline();
    renderStage();
  }
  return true;
}
function ownsTypingState(characterId, requestId) {
  const pending = typingStateFor(characterId);
  return Boolean(pending && pending.requestId === requestId);
}
function typingIndicatorMarkup(character, { includeAvatar = true } = {}) {
  const avatar = includeAvatar ? `<span class="typing-indicator-avatar">${avatarMarkup(character, { thumbnail: true, priority: true })}</span>` : "";
  return `<span class="typing-indicator" role="status" aria-live="polite" aria-busy="true" aria-label="角色正在输入">${avatar}<span class="typing-indicator-bubble" aria-hidden="true"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span><span class="sr-only">角色正在输入</span></span>`;
}
function renderRequestStatus() {
  const target = $("request-status");
  if (!target) return;
  const thread = currentThread();
  const channel = thread?.channel || "text";
  const pendingState = typingStateFor();
  const pending = pendingState?.channel === channel ? pendingState : null;
  const statusKey = requestStatusKey(state.selected, channel);
  const owner = state.requestStatusOwnerByCharacter.get(statusKey);
  const belongsToSegment = !owner?.segmentId || owner.segmentId === thread?.conversationSegmentId;
  target.className = "request-status";
  target.setAttribute("aria-busy", pending ? "true" : "false");
  target.textContent = belongsToSegment ? state.requestStatusByCharacter.get(statusKey) || "" : "";
  if (belongsToSegment && state.requestRecoveryByCharacter.get(statusKey) === "refresh_scene") {
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "request-recovery";
    refresh.textContent = "重新读取场景";
    refresh.disabled = globalRequestBusy();
    refresh.onclick = async () => {
      if (globalRequestBusy()) return;
      refresh.disabled = true;
      try {
        await resolvePresence();
        state.requestRecoveryByCharacter.delete(statusKey);
        setRequestStatus(state.selected, "场景已重新读取");
      } catch (error) {
        setRequestStatus(state.selected, `重新读取失败：${displayError(error)}`, "", "refresh_scene");
      }
    };
    target.append(" ", refresh);
  }
  // The face-to-face surface intentionally hides the text timeline. Keep the
  // same retry action reachable from its status row when the last user turn
  // failed, so the preserved action/speech draft is not the only recovery
  // path. Text mode already renders this action beside the failed message.
  if (!pending && currentThread()?.channel === "in_person") {
    const failedMessage = [...(currentThread()?.messages || [])]
      .reverse()
      .find((message) => message.role === "user" && message.communicationChannel === channel
        && (!thread.conversationSegmentId || message.conversationSegmentId === thread.conversationSegmentId));
    if (failedMessage?.status === "failed" && failedMessage.errorCode !== "request_result_unavailable") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "request-retry";
      retry.dataset.retryMessage = failedMessage.id;
      retry.textContent = "重试";
      retry.disabled = globalRequestBusy();
      retry.onclick = () => retryMessage(failedMessage.id);
      target.append(" ", retry);
    }
  }
  const stop = $("stop-waiting");
  if (stop) stop.hidden = !pending || ["waiting", "arrival", "presenting"].includes(pending.phase);
  const row = target.closest(".request-status-row");
  if (row) row.hidden = !target.textContent.trim() && (!stop || stop.hidden);
  syncFastForward();
}
function requestStatusKey(characterId, channel = currentThread()?.channel || "text") {
  return `${characterId}:${channel === "in_person" ? "in_person" : "text"}`;
}
function setRequestStatus(characterId, message, requestId = "", recoveryAction = "") {
  if (!characterId) return;
  if (requestId && !ownsTypingState(characterId, requestId)) return;
  const thread = state.threads.get(characterId);
  const requestMessage = requestId ? thread?.messages.find(item => item.requestId === requestId) : null;
  const channel = requestMessage?.communicationChannel || typingStateFor(characterId)?.channel || thread?.channel || "text";
  const key = requestStatusKey(characterId, channel);
  if (message) {
    state.requestStatusByCharacter.set(key, plain(message));
    state.requestStatusOwnerByCharacter.set(key, { requestId, segmentId: requestMessage?.conversationSegmentId || thread?.conversationSegmentId || "" });
  } else {
    state.requestStatusByCharacter.delete(key);
    state.requestStatusOwnerByCharacter.delete(key);
  }
  if (recoveryAction) state.requestRecoveryByCharacter.set(key, recoveryAction);
  else state.requestRecoveryByCharacter.delete(key);
  if (characterId === state.selected) renderRequestStatus();
}
function setTypingState({ characterId, requestId, channel, phase = "typing" }) {
  if (!characterId || !requestId) return;
  const current = typingStateFor(characterId);
  if (current && current.requestId !== requestId) {
    cancelPresentationQueue(characterId, current.requestId);
  }
  state.typingByCharacter.set(
    characterId,
    new TypingIndicatorState({ characterId, requestId, channel, phase }),
  );
  if ($("stop-waiting")) $("stop-waiting").disabled = false;
  if (characterId === state.selected) {
    renderRequestStatus();
    if (channel === "text") renderTimeline();
    if (channel === "in_person") renderStage();
  }
  updateComposerAvailability();
}
function updateTypingPhase(characterId, requestId, phase) {
  const current = typingStateFor(characterId);
  if (!current || current.requestId !== requestId) return;
  current.phase = phase;
  state.typingByCharacter.set(characterId, current);
  if (characterId === state.selected) {
    renderRequestStatus();
    if (current.channel === "text") renderTimeline();
    if (current.channel === "in_person") renderStage();
  }
}
function clearTypingState(characterId, requestId) {
  const current = typingStateFor(characterId);
  if (!current || (requestId && current.requestId !== requestId)) return;
  state.typingByCharacter.delete(characterId);
  if (characterId === state.selected) {
    renderRequestStatus();
    if (current.channel === "text") renderTimeline();
    if (current.channel === "in_person") renderStage();
  }
  updateComposerAvailability();
}
function bindAvatarImages(root = document) {
  root.querySelectorAll(".portrait img").forEach((image) => {
    if (image.dataset.bound) return;
    image.dataset.bound = "1";
    image.addEventListener("error", () => {
      image.closest(".portrait")?.classList.remove("has-image");
      image.remove();
    }, { once: true });
    image.addEventListener("load", () => image.closest(".portrait")?.classList.add("has-image"), { once: true });
  });
  root.querySelectorAll(".content-sticker img").forEach((image) => {
    if (image.dataset.bound) return;
    image.dataset.bound = "1";
    image.addEventListener("error", () => image.remove(), { once: true });
  });
  bindStickerImages(root);
}
const activeStickerGifs = new Set();
let stickerObserver = null;
function refreshAnimatedStickers() {
  activeStickerGifs.clear();
  const limit = quietPresentation() || document.documentElement.dataset.motionLevel === "light" ? 0 : 3;
  for (const image of document.querySelectorAll('.content-sticker img[data-animated="true"]')) {
    const animate = image.dataset.visible === "true" && activeStickerGifs.size < limit;
    const source = animate ? image.dataset.animatedSrc : image.dataset.staticSrc;
    if (source && image.getAttribute("src") !== source) image.src = source;
    if (animate) activeStickerGifs.add(image);
  }
}
function bindStickerImages(root = document) {
  const images = [...root.querySelectorAll('.content-sticker img[data-animated="true"]')];
  for (const image of activeStickerGifs) {
    if (!document.contains(image)) activeStickerGifs.delete(image);
  }
  if (!images.length || !("IntersectionObserver" in window)) return;
  if (!stickerObserver) {
    stickerObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const image = entry.target;
        image.dataset.visible = entry.isIntersecting ? "true" : "false";
      }
      refreshAnimatedStickers();
    }, { rootMargin: "120px 0px" });
  }
  images.forEach((image) => {
    if (image.dataset.stickerObserved) return;
    image.dataset.stickerObserved = "true";
    stickerObserver.observe(image);
  });
}
function renderCharacters() {
  const query = normalizedSearch($("character-search").value);
  const orderedCharacters = [...state.characters].sort((left, right) => {
    const pinned = Number(state.pinnedCharacters.has(right.character_id)) - Number(state.pinnedCharacters.has(left.character_id));
    if (pinned) return pinned;
    return Number(state.threads.get(right.character_id)?.lastActiveAt || 0) - Number(state.threads.get(left.character_id)?.lastActiveAt || 0);
  });
  const tokenRows = orderedCharacters.map((character) => ({
    character,
    tokens: [
      character.display_name,
      ...(character.aliases || []),
      ...(character.search_tokens || []),
      ...(LOCAL_PINYIN_TOKENS[character.character_id] || []),
    ].map(normalizedSearch).filter(Boolean),
  }));
  // An exact pinyin/initial token is more intentional than a substring. For
  // example, `kxy` selects 凯茜娅 without also matching 安卡希雅's `akxy`.
  const exactMatches = new Set(
    query
      ? tokenRows.filter((row) => row.tokens.includes(query)).map((row) => row.character.character_id)
      : [],
  );
  $("character-list").innerHTML = orderedCharacters
    .filter((character) => {
      if (!query) return true;
      const row = tokenRows.find((item) => item.character === character);
      if (exactMatches.size) return exactMatches.has(character.character_id);
      return row.tokens.some((token) => token.includes(query));
    })
    .map((character) => {
      const thread = state.threads.get(character.character_id);
      const last = [...(thread?.messages || [])].reverse().find((message) => message.status === "sent");
      const summary = last?.content
        ? last.content.replace(/\s+/g, " ").slice(0, 28)
        : last?.contentBlocks?.some((block) => block.type === "sticker")
          ? `发送了表情：${last.contentBlocks.find((block) => block.type === "sticker")?.caption || "表情"}`
          : "尚未开始对话";
      const pinned = state.pinnedCharacters.has(character.character_id);
      // 雪境 ADV (P5)：每行带 data-snow-character，让联系人卡片用各自的角色色（--char）。
      // 仅皮肤开启时写出，关闭态 DOM 与旧界面逐字节一致。
      const rowCharacter = snowAdvActive() ? ` data-snow-character="${escapeHtml(character.character_id)}"` : "";
      return `<div class="character-row" role="listitem"${rowCharacter}><button class="character" aria-current="${character.character_id === state.selected}" data-character="${escapeHtml(character.character_id)}">${avatarMarkup(character, { thumbnail: true, priority: character.character_id === state.selected })}<span><strong>${escapeHtml(character.display_name)}</strong><small>${escapeHtml(summary)}</small></span></button><button class="pin-character" type="button" data-pin-character="${escapeHtml(character.character_id)}" aria-pressed="${pinned}" aria-label="${pinned ? "取消置顶" : "置顶"}${escapeHtml(character.display_name)}">${pinned ? "★" : "☆"}</button></div>`;
    }).join("");
  bindAvatarImages($("character-list"));
  document.querySelectorAll("[data-character]").forEach((button) => { button.onclick = () => selectCharacter(button.dataset.character); });
  document.querySelectorAll("[data-pin-character]").forEach((button) => {
    button.onclick = async () => {
      const characterId = button.dataset.pinCharacter;
      if (state.pinnedCharacters.has(characterId)) state.pinnedCharacters.delete(characterId);
      else state.pinnedCharacters.add(characterId);
      await saveUiPreferences();
      renderCharacters();
      document.querySelector(`[data-pin-character="${CSS.escape(characterId)}"]`)?.focus();
    };
  });
}

const ONBOARDING_STEPS = [
  { title: "先选择想联系的角色", copy: "角色列表支持中文、拼音全拼和首字母搜索。" },
  { title: "使用你自己的模型", copy: "在设置中配置 BYOK 模型会话；API Key 不会写入聊天历史。" },
  { title: "从一句自然的话开始", copy: "输入消息或选择安全的开场建议。建议只会填入输入框，不会自动发送。" },
];
function finishOnboarding() {
  localPreferences.setItem("project-snow-public:onboarding", "complete");
  $("onboarding-guide").hidden = true;
}
function renderOnboarding() {
  if (localPreferences.getItem("project-snow-public:onboarding") === "complete") return;
  // P6a：开机/锁屏层占屏时不显示新手引导，避免盖在开场层之上；消隐/解锁后由
  // snowRunDeferredOpenings() 再次调用本函数按正常顺序显示。
  if (snowOpeningActive()) { $("onboarding-guide").hidden = true; return; }
  const step = Math.min(state.onboardingStep, ONBOARDING_STEPS.length - 1);
  const item = ONBOARDING_STEPS[step];
  $("onboarding-guide").hidden = false;
  $("onboarding-step-label").textContent = `第 ${step + 1} 步，共 ${ONBOARDING_STEPS.length} 步`;
  $("onboarding-title").textContent = item.title;
  $("onboarding-copy").textContent = item.copy;
  $("next-onboarding").textContent = step === ONBOARDING_STEPS.length - 1 ? "开始聊天" : "下一步";
}
function advanceOnboarding() {
  if (state.onboardingStep >= ONBOARDING_STEPS.length - 1) return finishOnboarding();
  state.onboardingStep += 1;
  renderOnboarding();
}
async function loadCharacters() {
  const payload = await api("/characters", { headers: {} });
  state.characters = payload.characters || [];
  await Promise.all(state.characters.map((character) => dbGetThread(character.character_id, 1)));
  await restoreInterruptedChatRequests();
  $("history-character").innerHTML = state.characters.map((character) => `<option value="${escapeHtml(character.character_id)}">${escapeHtml(character.display_name)}</option>`).join("");
  renderCharacters();
  if (!state.selected && state.characters[0]) {
    await selectCharacter(state.characters[0].character_id, { closeContacts: false });
  }
}
function sceneKeyForLocation(location) {
  return sceneVisualKey(location);
}
function cachedSceneForCharacter(characterId) {
  const decoded = decodeStatePackage(state.worldPackage || "");
  const presence = decoded.presence || decoded.world?.presence || {};
  const item = presence[characterId] || {};
  const analystLocation = decoded.analyst_location || decoded.world?.analyst_location || null;
  const location = item.location || null;
  if (!location) return null;
  return {
    analyst_location: analystLocation,
    character_location: location,
    character_activity: item.activity || "正在这里",
    visual_key: sceneKeyForLocation(location),
    co_located: Boolean(analystLocation && analystLocation === location),
    state_scope: item.state_scope || "session_simulation",
  };
}
function fillAvatar(node, character, { thumbnail = true, priority = false } = {}) {
  if (!node || !character) return;
  const temporary = document.createElement("div");
  temporary.innerHTML = avatarMarkup(character, { thumbnail, priority });
  const source = temporary.firstElementChild;
  node.className = `${source.className} ${node.id === "stage-header-avatar" ? "" : ""}`.trim();
  node.style.cssText = source.getAttribute("style") || "";
  node.innerHTML = source.innerHTML;
  delete node.dataset.stagePortraitKey;
  delete node.dataset.expressionCharacterId;
  delete node.dataset.expressionState;
  bindAvatarImages(node.parentElement || node);
}
function expressionStateForMessage(message) {
  return normalizeExpressionState(
    message?.expressionState ?? message?.expression_state,
    message?.communicationChannel || message?.communication_channel || "in_person",
  );
}
function expressionManifestUrlForCharacter(character) {
  const advertised = plain(character?.expression_manifest_url || character?.expressionManifestUrl).trim();
  const candidate = advertised || (character?.character_id === MIA_CHARACTER_ID ? MIA_BUNDLED_EXPRESSION_MANIFEST : "");
  if (!candidate) return "";
  try {
    const parsed = new URL(candidate, window.location.origin);
    if (parsed.origin !== window.location.origin) return "";
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return "";
  }
}
function expressionAssetUrl(value, manifestUrl) {
  const candidate = plain(value).trim();
  if (!candidate) return "";
  try {
    const manifestAbsolute = new URL(manifestUrl, window.location.origin);
    const expressionMarker = manifestAbsolute.pathname.indexOf("/expressions/");
    const packageBase = expressionMarker >= 0
      ? new URL(`${manifestAbsolute.pathname.slice(0, expressionMarker + 1)}`, window.location.origin)
      : manifestAbsolute;
    const parsed = new URL(candidate, candidate.startsWith("expressions/") ? packageBase : manifestAbsolute);
    if (parsed.origin !== window.location.origin) return "";
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return "";
  }
}
function expressionHash(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value : "";
}
function expressionManifestHashes(character, manifestUrl) {
  if (manifestUrl === MIA_BUNDLED_EXPRESSION_MANIFEST && character?.character_id === MIA_CHARACTER_ID) {
    return MIA_BUNDLED_EXPRESSION_MANIFEST_HASHES;
  }
  return [expressionHash(character?.expression_manifest_sha256 || character?.expressionManifestSha256)];
}
async function verifiedExpressionBytes(url, expectedHashes, maximum, signal) {
  const hashes = Array.isArray(expectedHashes) ? expectedHashes : [expectedHashes];
  if (!hashes.length || hashes.some(value => !expressionHash(value))) throw new Error("expression_digest_missing");
  const target = new URL(url, window.location.origin);
  if (target.origin !== window.location.origin || target.username || target.password) throw new Error("expression_asset_invalid");
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const cacheKey = `${target.href}:${hashes.join(":")}`;
  const cached = state.expressionBytesCache.get(cacheKey);
  if (cached) {
    if (cached.byteLength > maximum) throw new Error("expression_asset_too_large");
    state.expressionBytesCache.delete(cacheKey);
    state.expressionBytesCache.set(cacheKey, cached);
    return cached;
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; abort(); }, EXPRESSION_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(target.href, {
      signal: controller.signal, credentials: "omit", redirect: "error", cache: "force-cache",
    });
    if (!response.ok || Number(response.headers.get("content-length")) > maximum || !response.body) throw new Error("expression_asset_unavailable");
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > maximum) throw new Error("expression_asset_too_large");
        chunks.push(value);
      }
    } catch (error) {
      void reader.cancel().catch(() => {});
      throw error;
    } finally { reader.releaseLock(); }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), value => value.toString(16).padStart(2, "0")).join("");
    if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (!hashes.includes(digest)) throw new Error("expression_asset_hash_mismatch");
    state.expressionBytesCache.set(cacheKey, bytes);
    let retained = [...state.expressionBytesCache.values()].reduce((size, value) => size + value.byteLength, 0);
    while (state.expressionBytesCache.size > 32 || retained > EXPRESSION_IMAGE_MAX_BYTES) {
      const oldestKey = state.expressionBytesCache.keys().next().value;
      retained -= state.expressionBytesCache.get(oldestKey).byteLength;
      state.expressionBytesCache.delete(oldestKey);
    }
    return bytes;
  } catch (error) {
    if (timedOut && !signal?.aborted) throw new Error("expression_asset_timeout");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
function expressionImageDataUrl(bytes, mediaType, signal) {
  // Production CSP permits data: images. Encode only already verified bytes;
  // do not fetch the image again or retain another encoded-image cache.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    let settled = false;
    let timer = null;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reader.onload = null;
      reader.onerror = null;
      reader.onabort = null;
      if (reader.readyState === FileReader.LOADING) reader.abort();
      callback(value);
    };
    const abort = () => finish(reject, new DOMException("Aborted", "AbortError"));
    reader.onload = () => {
      const src = reader.result;
      if (typeof src !== "string" || !src.startsWith(`data:${mediaType};base64,`)) {
        finish(reject, new Error("expression_image_encoding_failed"));
      } else finish(resolve, src);
    };
    reader.onerror = () => finish(reject, new Error("expression_image_encoding_failed"));
    reader.onabort = abort;
    if (signal?.aborted) return abort();
    signal?.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => finish(reject, new Error("expression_image_encoding_timeout")), EXPRESSION_DECODE_TIMEOUT_MS);
    try { reader.readAsDataURL(new Blob([bytes], { type: mediaType })); }
    catch (error) { finish(reject, error); }
  });
}
async function verifiedExpressionImage(assetPath, sha256, signal) {
  const bytes = await verifiedExpressionBytes(assetPath, sha256, EXPRESSION_IMAGE_MAX_BYTES, signal);
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const png = bytes[0] === 137 && bytes[1] === 80 && bytes[2] === 78 && bytes[3] === 71;
  const webp = new TextDecoder().decode(bytes.subarray(0, 4)) === "RIFF" && new TextDecoder().decode(bytes.subarray(8, 12)) === "WEBP";
  if (!png && !webp) throw new Error("expression_image_invalid");
  const src = await expressionImageDataUrl(bytes, png ? "image/png" : "image/webp", signal);
  const image = await preloadImage(src, signal);
  return { src, image, dispose: () => image.removeAttribute("src") };
}
function normalizePresentationLayers(presentation, manifestUrl) {
  const canvas = presentation.canvas;
  const validSize = (size) => size && Number.isInteger(size.width) && Number.isInteger(size.height)
    && size.width > 0 && size.height > 0 && size.width <= 4096 && size.height <= 4096;
  if (!validSize(canvas) || !Array.isArray(presentation.layers)
    || presentation.layers.length < 1 || presentation.layers.length > 8) return null;
  let pixels = 0;
  const layers = [];
  for (const layer of presentation.layers) {
    const size = layer?.dimensions;
    const position = layer?.position;
    const assetPath = expressionAssetUrl(layer?.asset_path, manifestUrl);
    const sha256 = expressionHash(layer?.asset_sha256);
    if (!assetPath || !sha256 || !validSize(size) || layer.composite !== "source-over"
      || !position || !Number.isInteger(position.x) || !Number.isInteger(position.y)
      || position.x < 0 || position.y < 0
      || position.x + size.width > canvas.width || position.y + size.height > canvas.height) return null;
    pixels += size.width * size.height;
    if (pixels > canvas.width * canvas.height * 4) return null;
    layers.push({ assetPath, sha256, width: size.width, height: size.height, x: position.x, y: position.y });
  }
  return { canvas: { width: canvas.width, height: canvas.height }, layers };
}
function normalizeExpressionManifest(payload, character, manifestUrl) {
  if (!payload || typeof payload !== "object" || plain(payload.character_id) !== plain(character?.character_id)) throw new Error("expression_manifest_invalid");
  const rawExpressions = payload.expressions && typeof payload.expressions === "object" ? payload.expressions : {};
  const rawPresentations = payload.presentations && typeof payload.presentations === "object" ? payload.presentations : {};
  if (Number(payload.expression_state_count || 0) !== EXPRESSION_STATES.size) throw new Error("expression_manifest_invalid");
  const expressions = {};
  const performances = {};
  const presentations = {};
  const rawLayout = payload.stage_layout && typeof payload.stage_layout === "object"
    ? payload.stage_layout
    : payload.layout && typeof payload.layout === "object"
      ? payload.layout
      : {};
  const normalizeLayout = (value = rawLayout) => {
    const source = value && typeof value === "object" ? value : rawLayout;
    const requestedScale = Number(source.scale ?? source.stage_scale ?? rawLayout.scale ?? rawLayout.stage_scale ?? 1);
    const requestedFocusX = Number(source.focus_x ?? source.horizontal_focus ?? rawLayout.focus_x ?? rawLayout.horizontal_focus ?? 50);
    return {
      scale: Number.isFinite(requestedScale) ? Math.min(1.4, Math.max(.7, requestedScale)) : 1,
      focusX: Number.isFinite(requestedFocusX) ? Math.min(100, Math.max(0, requestedFocusX)) : 50,
    };
  };
  const performanceContract = payload.performance_contract;
  const rawPerformances = performanceContract?.schema_version === "project-snow-character-performance-1"
    && performanceContract.character_scoped === true && performanceContract.base_expression_states_unchanged === true
    && payload.performances && typeof payload.performances === "object" ? payload.performances : {};
  const performanceEntries = Object.entries(rawPerformances);
  const validPerformances = performanceEntries.length <= 6
    && payload.performance_count === performanceEntries.length ? performanceEntries.filter(([cue, item]) =>
      normalizePerformanceId(cue, "in_person") === cue && item?.presentation_id === `performance.${cue}`
      && EXPRESSION_STATES.has(item?.fallback_expression)) : [];
  const rows = [...[...EXPRESSION_STATES].map((key) => [key, rawExpressions[key], false]),
    ...validPerformances.map(([key, item]) => [key, item, true])];
  for (const [expressionState, item, isPerformance] of rows) {
    const legacyStageAssetPath = expressionAssetUrl(item?.stage_asset_path, manifestUrl);
    const faceAssetPath = expressionAssetUrl(item?.face_asset_path, manifestUrl);
    const legacyStageSha256 = expressionHash(item?.stage_asset_sha256);
    if (!legacyStageAssetPath || !faceAssetPath || !legacyStageSha256) throw new Error("expression_manifest_invalid");
    const presentationId = plain(item?.presentation_id).trim() || `legacy.expression.${expressionState}`;
    const declaredPresentation = rawPresentations[presentationId] && typeof rawPresentations[presentationId] === "object"
      ? rawPresentations[presentationId]
      : {};
    const declaredRenderStrategy = plain(declaredPresentation.render_strategy || item?.render_strategy || "single_sprite").trim();
    const nativeLayers = declaredRenderStrategy === "layered_sprite"
      ? normalizePresentationLayers(declaredPresentation, manifestUrl) : null;
    // Incomplete or unsupported presentations use the approved flattened asset.
    const supported = ACTIVE_STAGE_RENDER_STRATEGIES.has(declaredRenderStrategy)
      && (declaredRenderStrategy !== "layered_sprite" || Boolean(nativeLayers));
    const renderStrategy = supported
      ? declaredRenderStrategy
      : "single_sprite";
    const presentationStageAssetPath = expressionAssetUrl(declaredPresentation.stage_asset_path, manifestUrl);
    const stageAssetPath = supported
      ? presentationStageAssetPath || legacyStageAssetPath
      : legacyStageAssetPath;
    const stageAssetSha256 = supported && presentationStageAssetPath
      ? expressionHash(declaredPresentation.stage_asset_sha256) : legacyStageSha256;
    if (!stageAssetSha256) throw new Error("expression_manifest_invalid");
    const presentation = {
      id: presentationId,
      renderStrategy,
      declaredRenderStrategy,
      stageAssetPath,
      stageAssetSha256,
      ...(nativeLayers && supported ? nativeLayers : {}),
      layout: normalizeLayout(declaredPresentation.stage_layout || item?.stage_layout || rawLayout),
      usesLegacyFallback: !supported,
    };
    presentations[presentationId] = presentation;
    const entry = {
      stageAssetPath,
      stageAssetSha256,
      faceAssetPath,
      presentationId,
      renderStrategy,
      presentation,
    };
    if (isPerformance) performances[expressionState] = { ...entry, fallbackExpression: item.fallback_expression };
    else expressions[expressionState] = entry;
  }
  return {
    characterId: plain(character.character_id),
    manifestUrl,
    expressions,
    performances,
    presentations,
    layout: normalizeLayout(rawLayout),
  };
}
async function loadExpressionManifest(character, signal) {
  const releaseCharacter = stageReleaseCharacterForExpressions(character);
  if (releaseCharacter) {
    const expressions = {};
    for (const [key, asset] of Object.entries(releaseCharacter.states)) {
      expressions[key] = { presentation: {
        id: `release.expression.${key}`,
        stageAssetPath: asset.url,
        stageAssetSha256: asset.sha256,
        verifiedAsset: asset,
        renderStrategy: "single_sprite",
        declaredRenderStrategy: "single_sprite",
        layout: { scale: 1, focusX: 50 },
      } };
    }
    return { expressions, performances: {}, fromStageRelease: true };
  }
  const manifestUrl = expressionManifestUrlForCharacter(character);
  try { return await fetchExpressionManifest(character, manifestUrl, signal); }
  catch (error) {
    if (error?.name === "AbortError" || signal?.aborted || character?.character_id !== MIA_CHARACTER_ID
      || manifestUrl === MIA_BUNDLED_EXPRESSION_MANIFEST) throw error;
    const bundled = await fetchExpressionManifest(character, MIA_BUNDLED_EXPRESSION_MANIFEST, signal);
    return { expressions: { neutral: bundled.expressions.neutral }, performances: {} };
  }
}
function stageReleaseCharacterForExpressions(character) {
  // A character-scoped release is authoritative when the server advertises it.
  if (plain(character?.expression_manifest_url || character?.expressionManifestUrl).trim()) return null;
  return state.stageRelease?.characters.find(item => item.character_id === character?.character_id) || null;
}
async function fetchExpressionManifest(character, manifestUrl, signal) {
  if (!manifestUrl) throw new Error("expression_manifest_unavailable");
  const expectedHashes = expressionManifestHashes(character, manifestUrl);
  const cacheKey = `${plain(character.character_id)}:${manifestUrl}:${expectedHashes.join(":")}`;
  const cached = state.expressionManifestCache.get(cacheKey);
  if (cached) return cached;
  const bytes = await verifiedExpressionBytes(manifestUrl, expectedHashes, 2 * 1024 * 1024, signal);
  const manifest = normalizeExpressionManifest(JSON.parse(new TextDecoder().decode(bytes)), character, manifestUrl);
  state.expressionManifestCache.set(cacheKey, manifest);
  return manifest;
}
function preloadImage(src, signal) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    let settled = false;
    let timer = null;
    image.decoding = "async";
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      image.onload = null;
      image.onerror = null;
      callback(value);
    };
    const abort = () => {
      image.src = "";
      finish(reject, new DOMException("Aborted", "AbortError"));
    };
    const decoded = async () => {
      try {
        if (image.decode) await image.decode();
        finish(resolve, image);
      } catch {
        finish(reject, new Error("image_decode_failed"));
      }
    };
    image.onload = decoded;
    image.onerror = () => finish(reject, new Error("image_load_failed"));
    if (signal?.aborted) return abort();
    signal?.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => {
      finish(reject, new Error("image_decode_timeout"));
      image.src = "";
    }, EXPRESSION_DECODE_TIMEOUT_MS);
    image.src = src;
    if (image.complete && image.naturalWidth > 0) void decoded();
  });
}
const STAGE_PRESENTATION_RENDERERS = Object.freeze({
  single_sprite: Object.freeze({
    async preload(presentation, signal) {
      const prepared = await verifiedExpressionImage(presentation.stageAssetPath, presentation.stageAssetSha256, signal);
      return { ...prepared, renderStrategy: "single_sprite" };
    },
    commit(node, _presentation, prepared) {
      if (!prepared?.src) throw new Error("expression_surface_unavailable");
      node.src = prepared.src;
    },
  }),
  layered_sprite: Object.freeze({
    async preload(presentation, signal) {
      const layers = [];
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal?.aborted) abort();
      signal?.addEventListener("abort", abort, { once: true });
      const loading = presentation.layers.map(async (layer, index) => {
        layers[index] = await verifiedExpressionImage(layer.assetPath, layer.sha256, controller.signal);
      });
      try {
        // Download and decode independent layers together, retaining manifest order
        // for composition. Nothing is displayed until every source is verified.
        await Promise.all(loading);
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        const canvas = document.createElement("canvas");
        canvas.width = presentation.canvas.width;
        canvas.height = presentation.canvas.height;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("layer_composition_unavailable");
        context.imageSmoothingEnabled = false;
        for (const [index, layer] of presentation.layers.entries()) {
          const image = layers[index].image;
          if (image.naturalWidth !== layer.width || image.naturalHeight !== layer.height) {
            throw new Error("layer_dimensions_mismatch");
          }
          // Native pixels only: no per-layer scaling, warping, or partial DOM commit.
          context.drawImage(image, layer.x, layer.y);
        }
        const src = canvas.toDataURL("image/png");
        const image = await preloadImage(src, signal);
        return { src, image, renderStrategy: "layered_sprite", dispose: () => image.removeAttribute("src") };
      } finally {
        abort();
        await Promise.allSettled(loading);
        signal?.removeEventListener("abort", abort);
        for (const layer of layers) layer?.dispose();
      }
    },
    commit(node, _presentation, prepared) {
      node.src = prepared.src;
    },
  }),
});
function stagePresentationRenderer(presentation) {
  return STAGE_PRESENTATION_RENDERERS[presentation?.renderStrategy]
    || STAGE_PRESENTATION_RENDERERS.single_sprite;
}
async function prepareStagePresentation(presentation, signal) {
  if (!presentation?.stageAssetPath) throw new Error("expression_manifest_invalid");
  const renderer = stagePresentationRenderer(presentation);
  try {
    const prepared = await renderer.preload(presentation, signal);
    return { renderer, prepared };
  } catch (error) {
    if (error?.name === "AbortError" || signal?.aborted || presentation.renderStrategy !== "layered_sprite") throw error;
    const fallback = STAGE_PRESENTATION_RENDERERS.single_sprite;
    const prepared = await fallback.preload(presentation, signal);
    return { renderer: fallback, prepared };
  }
}
function trimStagePresentationCache() {
  const cache = state.stagePresentationCache;
  let bytes = [...cache.values()].reduce((total, entry) => total + (entry.bytes || 0), 0);
  for (const [key, entry] of cache) {
    if (cache.size <= STAGE_PRESENTATION_CACHE_LIMIT && bytes <= EXPRESSION_IMAGE_MAX_BYTES) break;
    if (!entry.value || entry.waiters) continue;
    cache.delete(key);
    bytes -= entry.bytes;
    entry.value.prepared.dispose?.();
  }
}
function clearStagePresentationCache() {
  for (const entry of state.stagePresentationCache.values()) {
    entry.controller.abort();
    entry.value?.prepared.dispose?.();
  }
  state.stagePresentationCache.clear();
}
function preloadStagePresentation(presentation, signal) {
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  // Include every pixel/composition input, so a changed digest or layer position
  // can never borrow a prepared surface from another presentation.
  const key = JSON.stringify([presentation?.renderStrategy, presentation?.stageAssetPath,
    presentation?.stageAssetSha256, presentation?.canvas, presentation?.layers]);
  const cache = state.stagePresentationCache;
  let entry = cache.get(key);
  if (!entry) {
    entry = { controller: new AbortController(), waiters: 0, value: null, bytes: 0, promise: null };
    cache.set(key, entry);
    entry.promise = prepareStagePresentation(presentation, entry.controller.signal).then((value) => {
      if (entry.controller.signal.aborted) {
        value.prepared.dispose?.();
        throw new DOMException("Aborted", "AbortError");
      }
      entry.value = value;
      const { src, image } = value.prepared;
      entry.bytes = src.length * 2 + (image?.naturalWidth || 0) * (image?.naturalHeight || 0) * 4;
      // A transient layered failure must be retried later, not cached forever as
      // a flattened fallback. The caller still owns that successful fallback.
      if (value.prepared.renderStrategy !== presentation.renderStrategy) cache.delete(key);
      return value;
    }).catch((error) => {
      if (cache.get(key) === entry) cache.delete(key);
      throw error;
    });
  } else {
    cache.delete(key);
    cache.set(key, entry);
  }
  entry.waiters += 1;
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      entry.waiters -= 1;
      if (!entry.value && !entry.waiters) {
        entry.controller.abort();
        if (cache.get(key) === entry) cache.delete(key);
      }
      callback(value);
      trimStagePresentationCache();
      if (entry.value && !entry.waiters && cache.get(key) !== entry) entry.value.prepared.dispose?.();
    };
    const abort = () => finish(reject, new DOMException("Aborted", "AbortError"));
    signal?.addEventListener("abort", abort, { once: true });
    entry.promise.then((value) => {
      // The cache owns decoded images; disposing one visible consumer must not
      // clear a surface being reused by a prewarm or another stage update.
      finish(resolve, { renderer: value.renderer, prepared: { ...value.prepared, dispose: () => {} } });
    }, (error) => finish(reject, error));
  });
}
async function warmCharacterStage(character, expressionState = "neutral", performanceId = "", signal) {
  if (!character || (!expressionManifestUrlForCharacter(character) && !stageReleaseCharacterForExpressions(character))) return false;
  try {
    const manifest = await loadExpressionManifest(character, signal);
    const cue = manifest.performances[normalizePerformanceId(performanceId, "in_person")];
    const presentation = cue?.presentation
      || manifest.expressions[normalizeExpressionState(expressionState, "in_person")]?.presentation
      || manifest.expressions.neutral?.presentation;
    await preloadStagePresentation(presentation, signal);
    return true;
  } catch { return false; /* Foreground rendering keeps its verified fallback path. */ }
}
function hideStageCharacterArt(node) {
  if (!node) return;
  cancelStageArtTransition(node);
  node.hidden = true;
  node.removeAttribute("src");
  state.stageArtSurface?.dispose?.();
  state.stageArtSurface = null;
  node.style.removeProperty("--stage-character-scale");
  node.style.removeProperty("--stage-character-focus-x");
  delete node.dataset.stageArtKey;
  delete node.dataset.stageArtFailedKey;
  delete node.dataset.stageArtFailedAt;
  delete node.dataset.expressionState;
  delete node.dataset.expressionCharacterId;
  delete node.dataset.presentationId;
  delete node.dataset.performanceId;
  delete node.dataset.renderStrategy;
  delete node.dataset.declaredRenderStrategy;
}
function cancelStageArtTransition(node = $("stage-character-art")) {
  const transition = state.stageArtTransition;
  state.stageArtTransition = null;
  for (const animation of transition?.animations || []) animation.cancel();
  transition?.outgoing?.remove();
  transition?.dispose?.();
  if (node) {
    node.style.removeProperty("opacity");
    delete node.dataset.stageArtTransition;
  }
}
function crossfadeStageCharacterArt(node, duration = 280) {
  cancelStageArtTransition(node);
  if (
    !node?.parentNode
    || node.hidden
    || !node.getAttribute("src")
    || typeof node.animate !== "function"
    || reducedMotion()
    || document.hidden
  ) return null;
  const outgoing = node.cloneNode(false);
  outgoing.removeAttribute("id");
  outgoing.classList.add("stage-character-art-outgoing");
  outgoing.hidden = false;
  delete outgoing.dataset.stageArtTransition;
  delete outgoing.dataset.stageMotion;
  delete outgoing.dataset.stageMotionKey;
  delete outgoing.dataset.stageMotionPlayCount;
  node.parentNode.insertBefore(outgoing, node);
  node.style.opacity = "1";
  node.dataset.stageArtTransition = "incoming";
  // 雪境 ADV (P4)：换表情不出现透明空档。外层（旧图）全程保持 opacity 1，
  // 内层（新图，z-index 更高、叠在上面）从 0 淡入到 1，淡入完成后再移除旧图。
  // 这样任意时刻可见画面都是不透明的，绝不会两层同时半透明。关闭开关时沿用旧的交叉淡出。
  const snowAdv = snowAdvActive();
  const options = {
    duration,
    easing: snowAdv ? "cubic-bezier(0.22, 0.61, 0.36, 1)" : "cubic-bezier(.22,.72,.24,1)",
    fill: "both",
  };
  if (snowAdv) outgoing.style.opacity = "1";
  const transition = {
    outgoing,
    dispose: state.stageArtSurface?.dispose,
    animations: [
      snowAdv ? outgoing.animate([{ opacity: 1 }, { opacity: 1 }], options)
              : outgoing.animate([{ opacity: 1 }, { opacity: 0 }], options),
      node.animate([{ opacity: 0 }, { opacity: 1 }], options),
    ],
  };
  state.stageArtTransition = transition;
  state.stageArtSurface = null;
  void Promise.allSettled(transition.animations.map((animation) => animation.finished)).then(() => {
    if (state.stageArtTransition !== transition) return;
    state.stageArtTransition = null;
    for (const animation of transition.animations) animation.cancel();
    outgoing.remove();
    transition.dispose?.();
    node.style.removeProperty("opacity");
    delete node.dataset.stageArtTransition;
  });
  return transition;
}
// 雪境 ADV (P4)：立绘入场。只在开关开启时生效，只动 transform/opacity，绝不阻塞逻辑
// （纯视觉，调用方不 await）。auto=上浮+淡入(--ease-settle, ~420ms)；light=仅淡入；
// off/减少动态/页面隐藏/无 WAAPI=不动画。transform 不与 P3 的 translate 中轴定位冲突。
function playStageEnter(node) {
  if (!snowAdvActive() || !node || node.hidden || typeof node.animate !== "function") return false;
  const tier = stageMotionTier();
  if (tier === "off" || document.hidden) return false;
  state.stageEnterAnimation?.cancel();
  const frames = tier === "light"
    ? [{ opacity: 0 }, { opacity: 1 }]
    : [{ opacity: 0, transform: "translateY(12px)" }, { opacity: 1, transform: "translateY(0)" }];
  const animation = node.animate(frames, {
    duration: 420,
    easing: "cubic-bezier(0.34, 1.32, 0.5, 1)", // --ease-settle
    fill: "both",
  });
  state.stageEnterAnimation = animation;
  node.dataset.stageEnter = tier;
  animation.finished.catch(() => {}).finally(() => {
    if (state.stageEnterAnimation === animation) state.stageEnterAnimation = null;
    delete node.dataset.stageEnter;
    // fill:both leaves the final frame latched; clear it so breathing/motion own transform again.
    try { animation.cancel(); } catch {}
  });
  return true;
}
function cancelStageArt({ hide = false } = {}) {  state.stageArtRequestSequence += 1;
  state.stageArtController?.abort();
  state.stageArtController = null;
  state.stageArtPending = null;
  cancelStageArtTransition();
  if (hide) hideStageCharacterArt($("stage-character-art"));
}
async function updateStageCharacterArt(node, character, expressionState, performanceId = "") {
  if (!node) return false;
  const characterId = plain(character?.character_id);
  const manifestUrl = expressionManifestUrlForCharacter(character);
  const releaseCharacter = stageReleaseCharacterForExpressions(character);
  const requestedState = normalizeExpressionState(expressionState, "in_person");
  const requestedPerformance = normalizePerformanceId(performanceId, "in_person");
  const sourceKey = releaseCharacter ? `stage-release:${state.stageRelease.version}`
    : manifestUrl ? `${manifestUrl}:${expressionManifestHashes(character, manifestUrl).join(":")}` : "";
  const stageArtKey = `${characterId}:${requestedState}:${requestedPerformance}:${sourceKey}`;
  // Returning to the already displayed surface still supersedes an in-flight decode.
  if (state.stageArtPending && state.stageArtPending.key !== stageArtKey) cancelStageArt();
  if (node.dataset.stageArtKey === stageArtKey && node.getAttribute("src") && !node.hidden) return true;
  if (!characterId || !sourceKey) {
    cancelStageArt({ hide: true });
    return false;
  }
  const failedAt = Number(node.dataset.stageArtFailedAt || 0);
  if (node.dataset.stageArtFailedKey === stageArtKey && Date.now() - failedAt < 5000) return false;
  if (state.stageArtPending?.key === stageArtKey) return state.stageArtPending.promise;
  const requestSequence = ++state.stageArtRequestSequence;
  state.stageArtController?.abort();
  const controller = new AbortController();
  state.stageArtController = controller;
  let timedOut = false;
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, EXPRESSION_UPDATE_TIMEOUT_MS);
  const pending = { key: stageArtKey, controller, promise: null };
  const operation = (async () => {
    let manifest;
    try {
      manifest = await loadExpressionManifest(character, controller.signal);
    } catch (error) {
      if (error?.name === "AbortError" || requestSequence !== state.stageArtRequestSequence) return false;
      if (currentCharacter()?.character_id === characterId) {
        hideStageCharacterArt(node);
        node.dataset.stageArtFailedKey = stageArtKey;
        node.dataset.stageArtFailedAt = String(Date.now());
      }
      return false;
    }
    const performance = manifest.performances[requestedPerformance];
    const baseStates = [...new Set([...(performance ? [performance.fallbackExpression] : []), requestedState, "neutral"])];
    const candidates = [
      ...(performance ? [{ state: requestedState, presentation: performance.presentation, performanceId: requestedPerformance }] : []),
      ...baseStates.map((candidate) => ({ state: candidate, presentation: manifest.expressions[candidate]?.presentation, performanceId: "" })),
    ];
    let loadedState = "";
    let loadedPresentation = null;
    let loadedRenderer = null;
    let loadedSurface = null;
    let loadedPerformance = "";
    for (const candidate of candidates) {
      try {
        const presentation = candidate.presentation;
        if (!presentation) continue;
        const { renderer, prepared } = await preloadStagePresentation(presentation, controller.signal);
        loadedState = candidate.state;
        loadedPerformance = candidate.performanceId;
        loadedPresentation = presentation;
        loadedRenderer = renderer;
        loadedSurface = prepared;
        break;
      } catch (error) {
        if (error?.name === "AbortError") return false;
        // The neutral derivative is the final fallback below.
      }
    }
    if (!loadedState && manifest.fromStageRelease && characterId === MIA_CHARACTER_ID && !controller.signal.aborted) {
      // The bundled approved neutral remains available if the optional release fails verification.
      try {
        const bundled = await fetchExpressionManifest(character, MIA_BUNDLED_EXPRESSION_MANIFEST, controller.signal);
        const presentation = bundled.expressions.neutral.presentation;
        const { renderer, prepared } = await preloadStagePresentation(presentation, controller.signal);
        loadedState = "neutral";
        loadedPresentation = presentation;
        loadedRenderer = renderer;
        loadedSurface = prepared;
      } catch (error) {
        if (error?.name === "AbortError") return false;
      }
    }
    if (requestSequence !== state.stageArtRequestSequence || currentCharacter()?.character_id !== characterId) {
      loadedSurface?.dispose?.();
      return false;
    }
    if (!loadedState || !loadedPresentation || !loadedRenderer) {
      hideStageCharacterArt(node);
      node.dataset.stageArtFailedKey = stageArtKey;
      node.dataset.stageArtFailedAt = String(Date.now());
      return false;
    }
    const previousCharacterId = plain(node.dataset.expressionCharacterId);
    const shouldCrossfade = previousCharacterId
      && node.getAttribute("src") !== loadedSurface.src
      && !node.hidden
      && Boolean(node.getAttribute("src"));
    const outgoingTransition = shouldCrossfade
      ? crossfadeStageCharacterArt(node, previousCharacterId === characterId ? 160 : 280) : null;
    // 雪境 ADV (P4)：立绘首次为某场景/到场出现时（上一帧无图或已隐藏，且不是换表情交叉淡入），
    // 以 --ease-settle 做 ~420ms 的柔和上浮(≈12px→0)+淡入(0→1)。light 仅淡入，off 不做。
    const firstAppearance = !outgoingTransition && (node.hidden || !node.getAttribute("src"));
    const previousSurface = state.stageArtSurface;
    loadedRenderer.commit(node, loadedPresentation, loadedSurface);
    state.stageArtSurface = loadedSurface;
    previousSurface?.dispose?.();
    node.dataset.stageArtKey = stageArtKey;
    node.dataset.expressionState = loadedState;
    node.dataset.expressionCharacterId = characterId;
    node.dataset.presentationId = loadedPresentation.id;
    node.dataset.performanceId = loadedPerformance;
    node.dataset.renderStrategy = loadedSurface.renderStrategy;
    node.dataset.declaredRenderStrategy = loadedPresentation.declaredRenderStrategy;
    node.style.setProperty("--stage-character-scale", String(loadedPresentation.layout.scale));
    node.style.setProperty("--stage-character-focus-x", `${loadedPresentation.layout.focusX}%`);
    delete node.dataset.stageArtFailedKey;
    delete node.dataset.stageArtFailedAt;
    node.hidden = false;
    if (!outgoingTransition) cancelStageArtTransition(node);
    if (firstAppearance) playStageEnter(node);
    return true;
  })();
  pending.promise = operation;
  state.stageArtPending = pending;
  try {
    return await operation;
  } finally {
    clearTimeout(deadline);
    if (timedOut && requestSequence === state.stageArtRequestSequence) {
      hideStageCharacterArt(node);
      node.dataset.stageArtFailedKey = stageArtKey;
      node.dataset.stageArtFailedAt = String(Date.now());
    }
    if (state.stageArtPending === pending) state.stageArtPending = null;
    if (state.stageArtController === controller) state.stageArtController = null;
  }
}

function cancelStageMotion(characterId = "") {
  if (characterId && state.stageMotionCharacterId && state.stageMotionCharacterId !== characterId) return false;
  state.stageMotionRequestSequence += 1;
  const animation = state.stageMotionAnimation;
  state.stageMotionAnimation = null;
  state.stageMotionCharacterId = "";
  animation?.cancel();
  const node = $("stage-character-art");
  if (node) delete node.dataset.stageMotion;
  return Boolean(animation);
}
function stageMotionFrames(stageMotion) {
  // 雪境 ADV (P4)：带缓冲、小振幅、会 settle 的曲线，替代旧的 ±10px 生硬抖动。
  // 动作名与触发完全不变；只在开关开启时走这一支。只动 transform。
  if (snowAdvActive()) {
    if (stageMotion === "lean_in") return {
      duration: 1700, easing: "cubic-bezier(0.22, 0.61, 0.36, 1)",
      frames: [
        { transform: "translateY(0) scale(1)", offset: 0 },
        { transform: "translateY(-1.1%) scale(1.035)", offset: 0.32 },
        { transform: "translateY(-1.1%) scale(1.035)", offset: 0.68 },
        { transform: "translateY(0) scale(1)", offset: 1 },
      ],
    };
    if (stageMotion === "tremble") return {
      duration: 760, easing: "cubic-bezier(0.22, 0.61, 0.36, 1)",
      frames: [
        { transform: "translateX(0)", offset: 0 },
        { transform: "translateX(-5px)", offset: 0.1 },
        { transform: "translateX(4.4px)", offset: 0.22 },
        { transform: "translateX(-3.4px)", offset: 0.34 },
        { transform: "translateX(2.5px)", offset: 0.46 },
        { transform: "translateX(-1.6px)", offset: 0.58 },
        { transform: "translateX(0.9px)", offset: 0.72 },
        { transform: "translateX(-0.4px)", offset: 0.86 },
        { transform: "translateX(0)", offset: 1 },
      ],
    };
    if (stageMotion === "recoil") return {
      duration: 1000, easing: "cubic-bezier(0.34, 1.32, 0.5, 1)",
      frames: [
        { transform: "translate(0, 0) scale(1)", offset: 0 },
        { transform: "translate(-0.8%, 1.2%) scale(0.982)", offset: 0.26 },
        { transform: "translate(0, 0) scale(1)", offset: 1 },
      ],
    };
    if (stageMotion === "startle") return {
      duration: 760, easing: "cubic-bezier(0.34, 1.32, 0.5, 1)",
      frames: [
        { transform: "translateY(0)", offset: 0 },
        { transform: "translateY(-2.2%)", offset: 0.16 },
        { transform: "translateY(0.35%)", offset: 0.38 },
        { transform: "translateY(-0.5%)", offset: 0.6 },
        { transform: "translateY(0)", offset: 1 },
      ],
    };
    return null;
  }
  if (stageMotion === "lean_in") return {
    duration: 560,
    frames: [
      { transform: "translateY(0) scale(1)", offset: 0 },
      { transform: "translateY(10px) scale(1.06)", offset: 0.5 },
      { transform: "translateY(0) scale(1)", offset: 1 },
    ],
  };
  if (stageMotion === "tremble") return {
    duration: 480,
    frames: [
      { transform: "translateX(0)" },
      { transform: "translateX(-10px)" }, { transform: "translateX(10px)" },
      { transform: "translateX(-10px)" }, { transform: "translateX(10px)" },
      { transform: "translateX(-10px)" }, { transform: "translateX(10px)" },
      { transform: "translateX(-10px)" }, { transform: "translateX(10px)" },
      { transform: "translateX(0)" },
    ],
  };
  if (stageMotion === "recoil") return {
    duration: 420,
    frames: [
      { transform: "translateY(0) scale(1)", offset: 0 },
      { transform: "translateY(-5px) scale(.95)", offset: 0.48 },
      { transform: "translateY(0) scale(1)", offset: 1 },
    ],
  };
  if (stageMotion === "startle") return {
    duration: 460,
    frames: [
      { transform: "translateY(0) scale(1)", offset: 0 },
      { transform: "translateY(-14px) scale(1.04)", offset: 0.42 },
      { transform: "translateY(0) scale(1)", offset: 1 },
    ],
  };
  return null;
}
function playStageMotion(node, message, presentation, requestSequence = state.stageMotionRequestSequence) {
  const stageMotion = normalizeStageMotion(message?.stageMotion, message?.communicationChannel);
  if (!node || !presentation || presentation.cancelled || stageMotion === "none") return false;
  if (requestSequence !== state.stageMotionRequestSequence) return false;
  if (message?.id !== presentation.messageId || message?.id !== latestInPersonMessage("assistant")?.id) return false;
  if (message?.characterId !== state.selected || node.hidden || !node.getAttribute("src")) return false;
  const releaseCharacter = stageReleaseCharacterForExpressions(currentCharacter());
  if (releaseCharacter && !releaseCharacter.motions.includes(stageMotion)) return false;
  const playKey = `${plain(message.id)}:${stageMotion}`;
  if (state.playedStageMotionKeys.has(playKey)) return false;
  state.playedStageMotionKeys.add(playKey);
  while (state.playedStageMotionKeys.size > 500) {
    state.playedStageMotionKeys.delete(state.playedStageMotionKeys.values().next().value);
  }
  node.dataset.stageMotionKey = playKey;
  node.dataset.stageMotion = reducedMotion() ? "suppressed" : stageMotion;
  if (document.hidden || reducedMotion() || typeof node.animate !== "function") return false;
  // 雪境 ADV (P4)：light 档位只保留淡入淡出，不做任何位移/缩放动作；标记为 suppressed。
  if (snowAdvActive() && stageMotionTier() === "light") { node.dataset.stageMotion = "suppressed"; return false; }
  const definition = stageMotionFrames(stageMotion);
  if (!definition) return false;
  state.stageMotionAnimation?.cancel();
  const animation = node.animate(definition.frames, {
    duration: definition.duration,
    easing: definition.easing || "cubic-bezier(.22,.78,.24,1)",
    iterations: 1,
  });
  state.stageMotionAnimation = animation;
  state.stageMotionCharacterId = message.characterId;
  node.dataset.stageMotionPlayCount = String(Number(node.dataset.stageMotionPlayCount || 0) + 1);
  animation.finished.catch(() => {}).finally(() => {
    if (state.stageMotionAnimation !== animation) return;
    state.stageMotionAnimation = null;
    state.stageMotionCharacterId = "";
    delete node.dataset.stageMotion;
  });
  return true;
}
async function resolvePresenceAttempt(characterId, signal, stateRecoveryAttempt = 0) {
  if (dialogueV2Enabled()) {
    const thread = await dbGetThread(characterId);
    await ensureContinuityDecision(thread);
    const result = await performSceneAction("resolve", characterId, signal);
    if (characterId === state.selected && state.writerOwned) await setChannel(thread.channel, false, thread);
    return result;
  }
  try {
    const result = await api("/presence/resolve", { method: "POST", signal, body: JSON.stringify({ request_id: id(), character_id: characterId, state_package: state.worldPackage || "" }) });
    await saveWorldPackage(result.state_package);
    state.sceneByCharacter.set(characterId, result.scene_state || {});
    if (characterId === state.selected) {
      state.scene = result.scene_state;
      renderScene();
    }
    return result;
  } catch (error) {
    if (stateRecoveryAttempt < 1 && statePackageRecoveryError(error) && !signal?.aborted) {
      await clearPersistedWorldPackage();
      return resolvePresenceAttempt(characterId, signal, stateRecoveryAttempt + 1);
    }
    throw error;
  }
}

async function restoreInterruptedChatRequests() {
  for (const thread of state.threads.values()) {
    let changed = false;
    for (const message of thread.messages || []) {
      if (message.role !== "user") continue;
      if (message.requestId && message.requestSnapshot && typeof message.requestSnapshot === "object") {
        state.retrySnapshots.set(message.id, {
          requestId: message.requestId,
          payload: structuredClone(message.requestSnapshot),
        });
      }
      if (message.status === "pending") {
        message.status = "failed";
        message.errorCode = "stream_disconnected";
        changed = true;
      }
    }
    if (changed) await dbPutThread(thread);
  }
}
async function resolvePresence(characterId = state.selected, signal = undefined) {
  if (!characterId) return null;
  state.presenceResolvePending += 1;
  updateComposerAvailability();
  try {
    return await resolvePresenceAttempt(characterId, signal);
  } finally {
    state.presenceResolvePending = Math.max(0, state.presenceResolvePending - 1);
    updateComposerAvailability();
    void flushDeferredPresenceRefresh();
  }
}
async function flushDeferredPresenceRefresh() {
  const characterId = state.deferredPresenceCharacter;
  if (
    !characterId
    || characterId !== state.selected
    || state.deferredPresenceRunning
    || globalRequestBusy()
  ) return;
  state.deferredPresenceCharacter = "";
  state.deferredPresenceRunning = true;
  setRequestStatus(characterId, "正在重新读取当前角色的场景……");
  try {
    await resolvePresence(characterId, state.selectionController?.signal);
    if (characterId === state.selected) setRequestStatus(characterId, "场景已重新读取");
  } catch (error) {
    if (error?.name !== "AbortError" && characterId === state.selected) {
      setRequestStatus(characterId, `重新读取失败：${displayError(error)}`, "", "refresh_scene");
    }
  } finally {
    state.deferredPresenceRunning = false;
    updateComposerAvailability();
    if (state.deferredPresenceCharacter && state.deferredPresenceCharacter !== characterId) {
      void flushDeferredPresenceRefresh();
    }
  }
}
async function runSceneTransition(operation, title = "正在前往……", owner = 0) {
  if (featureEnabled("motion_v2")) return await operation;
  const layer = $("scene-transition-layer");
  const started = performance.now();
  layer.dataset.owner = String(owner);
  layer.hidden = false;
  layer.dataset.phase = "leaving";
  $("transition-title").textContent = title;
  $("transition-detail").textContent = "正在建立新的场景";
  if (!reducedMotion()) await delay(180);
  layer.dataset.phase = "arriving";
  try {
    const result = await operation;
    const minimum = reducedMotion() ? 0 : 900;
    const elapsed = performance.now() - started;
    if (elapsed < minimum) await delay(minimum - elapsed);
    // A newer selection may have taken ownership while the operation was
    // awaiting the network. Never let an old transition hide or relabel it.
    if (layer.dataset.owner !== String(owner)) return result;
    layer.dataset.phase = "entering";
    $("transition-detail").textContent = "场景已更新";
    if (!reducedMotion()) await delay(180);
    if (layer.dataset.owner === String(owner)) {
      layer.dataset.phase = "idle";
      layer.hidden = true;
    }
    return result;
  } catch (error) {
    if (layer.dataset.owner === String(owner)) {
      layer.dataset.phase = "idle";
      layer.hidden = true;
    }
    throw error;
  }
}
async function runModeTransition(operation, title = "正在打开通讯器") {
  if (globalRequestBusy()) return null;
  const layer = $("mode-transition-layer");
  state.modeTransitionPending = true;
  updateComposerAvailability();
  const owner = ++state.modeTransitionSequence;
  const controller = new AbortController();
  state.modeTransitionController = controller;
  if (featureEnabled("motion_v2") || sceneActionsV2Enabled()) {
    const characterId = state.selected;
    setRequestStatus(characterId, title);
    try {
      // Begin network work on the click, independently of the visual transition.
      return await operation(controller.signal);
    } finally {
      if (state.modeTransitionSequence === owner) {
        state.modeTransitionPending = false;
        state.modeTransitionController = null;
        setRequestStatus(characterId, "");
        updateComposerAvailability();
      }
    }
  }
  if (!layer) {
    try {
      return await operation(controller.signal);
    } finally {
      if (state.modeTransitionSequence === owner) {
        state.modeTransitionPending = false;
        state.modeTransitionController = null;
      }
    }
  }
  const started = performance.now();
  layer.dataset.owner = String(owner);
  layer.dataset.phase = "leaving";
  layer.hidden = false;
  layer.setAttribute("aria-busy", "true");
  $("mode-transition-title").textContent = title;
  $("mode-transition-detail").textContent = "正在准备通讯记录";
  if (!reducedMotion()) await delay(180);
  try {
    const result = await operation(controller.signal);
    const minimum = reducedMotion() ? 0 : 600;
    const elapsed = performance.now() - started;
    if (elapsed < minimum) await delay(minimum - elapsed);
    if (layer.dataset.owner !== String(owner)) return result;
    $("mode-transition-detail").textContent = "通讯器已打开";
    layer.dataset.phase = "entering";
    layer.setAttribute("aria-busy", "false");
    if (!reducedMotion()) await delay(180);
    if (layer.dataset.owner === String(owner)) layer.hidden = true;
    return result;
  } catch (error) {
    if (layer.dataset.owner === String(owner)) {
      layer.dataset.phase = "entering";
      layer.setAttribute("aria-busy", "false");
      layer.hidden = true;
    }
    throw error;
  } finally {
    if (state.modeTransitionSequence === owner) {
      state.modeTransitionPending = false;
      state.modeTransitionController = null;
      updateComposerAvailability();
    }
  }
}
function cancelModeTransition() {
  surfaceMotion.cancel();
  state.modeTransitionController?.abort();
  state.modeTransitionController = null;
  state.modeTransitionSequence += 1;
  state.modeTransitionPending = false;
  const layer = $("mode-transition-layer");
  if (layer) {
    layer.dataset.owner = String(state.modeTransitionSequence);
    layer.dataset.phase = "idle";
    layer.setAttribute("aria-busy", "false");
    layer.hidden = true;
  }
  updateComposerAvailability();
}
async function selectCharacter(characterId, { closeContacts = true } = {}) {
  if (!characterId || characterId === state.selected && !state.selectionController) return;
  cancelSnowScenePresentation();
  cancelBackgroundSummaries();
  if (state.selected) await saveDraftNow();
  const sequence = ++state.selectionSequence;
  state.selectionController?.abort();
  cancelModeTransition();
  state.selectionController = new AbortController();
  const controller = state.selectionController;
  state.stageWarmController?.abort();
  state.stageWarmController = controller;
  // Start artwork alongside local history and presence resolution, while the
  // user is still in the contact/transition UI rather than after opening the stage.
  void warmCharacterStage(characterById(characterId), "neutral", "", controller.signal);
  const previousId = state.selected;
  const previousThread = currentThread();
  const switchingFromStage = Boolean(previousId && previousId !== characterId && previousThread?.channel === "in_person");
  cancelPresentationQueue(previousId);
  if (previousId && previousId !== characterId) cancelStageArt();
  try {
    const thread = await dbGetThread(characterId);
    if (dialogueV2Enabled()) await loadDialogueRecord(characterId);
    if (sequence !== state.selectionSequence) return;
    const previousReply = [...thread.messages].reverse().find(message => message.role === "assistant" && message.communicationChannel === "in_person");
    if (previousReply && (expressionStateForMessage(previousReply) !== "neutral" || previousReply.performanceId)) {
      void warmCharacterStage(characterById(characterId), expressionStateForMessage(previousReply), previousReply.performanceId, controller.signal);
    }
    if (!(await ensureContinuityDecision(thread)) || sequence !== state.selectionSequence) return;
    thread.lastActiveAt = Date.now();
    await dbPutThread(thread);
    if (sequence !== state.selectionSequence) return;

    let preparedScene = state.sceneByCharacter.get(characterId) || cachedSceneForCharacter(characterId) || null;
    let preparedPackage = "";
    let recoveredState = false;
    if (!state.writerOwned) {
      preparedScene = cachedSceneForCharacter(characterId);
    } else if (globalRequestBusy()) {
      state.deferredPresenceCharacter = characterId;
    } else {
      state.presenceResolvePending += 1;
      updateComposerAvailability();
      try {
        const operation = preparePresenceCandidate(characterId, controller.signal);
        const candidate = switchingFromStage && !dialogueV2Enabled()
          ? await runSceneTransition(operation, "正在前往……", sequence)
          : await operation;
        if (sequence !== state.selectionSequence || controller.signal.aborted) return;
        preparedScene = candidate.result.scene_state || preparedScene;
        preparedPackage = plain(candidate.result.state_package);
        recoveredState = candidate.recoveredState;
      } finally {
        state.presenceResolvePending = Math.max(0, state.presenceResolvePending - 1);
        updateComposerAvailability();
      }
    }

    if (sequence !== state.selectionSequence || controller.signal.aborted) return;
    if (recoveredState) await clearPersistedWorldPackage();
    if (preparedPackage && !(await saveWorldPackage(preparedPackage))) {
      preparedScene = cachedSceneForCharacter(characterId);
    }
    if (sequence !== state.selectionSequence || controller.signal.aborted) return;

    state.selected = characterId;
    state.timelineVisibleLimit = 60;
    state.timelineEndId = "";
    state.transcriptEndId = "";
    state.selectedSticker = null;
    state.actionComposerOpen = false;
    if (state.stickerSection === "character") {
      state.stickers = [];
      state.stickerCursor = null;
      state.stickerHasMore = true;
    }
    window.clearTimeout(state.typewriter.timer);
    state.typewriter = { key: "", timer: 0, fullText: "", displayedText: "" };
    if (preparedScene) state.sceneByCharacter.set(characterId, preparedScene);
    state.scene = preparedScene;
    const character = characterById(characterId);
    // Data only; snow-adv.css reads it for the character colour when enabled.
    $("chat-app").dataset.snowCharacter = characterId;
    renderCharacters();
    $("active-character").innerHTML = `${avatarMarkup(character, { thumbnail: true, priority: true, className: "large" })}<div><h1>${escapeHtml(character.display_name)}</h1><p>文字通讯</p></div>`;
    fillAvatar($("stage-header-avatar"), character, { thumbnail: true, priority: true });
    $("stage-character-name").textContent = character.display_name;
    $("stage-speaker").textContent = character.display_name;

    renderScene();
    if (globalRequestBusy() && !preparedPackage) {
      await setChannel("text", false, thread);
      setRequestStatus(characterId, "上一位角色仍在回复，完成后会重新读取当前场景。");
    } else if (dialogueV2Enabled()) {
      await setChannel(thread.channel === "in_person" && preparedScene?.co_located ? "in_person" : "text", false, thread);
    } else if (switchingFromStage && !preparedScene?.co_located) {
      await setChannel("in_person", false);
      window.setTimeout(() => { if (sequence === state.selectionSequence) openPresenceDialog(); }, 0);
    } else {
      await setChannel(thread.channel || "text", false, thread);
    }
    renderAll();
    updateComposerAvailability();
    if (closeContacts) {
      $("contact-panel").classList.remove("open");
      if (window.matchMedia("(max-width: 820px)").matches) {
        $("contact-panel").setAttribute("aria-hidden", "true");
        $("contact-panel").inert = true;
      }
      syncContactToggleState();
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.selectionSequence) return;
    syncContactToggleState();
    showBanner(displayError(error));
  } finally {
    if (state.selectionSequence === sequence && state.selectionController === controller) {
      state.selectionController = null;
    }
  }
}

function blockHtml(block) {
  if (block.type === "sticker") {
    const fullSrc = block.src || block.thumbnailSrc || block.thumbnail_src || "";
    const staticSrc = block.thumbnailSrc || block.thumbnail_src || fullSrc;
    const displaySrc = block.displaySrc || block.display_src || "";
    const animatedSrc = displaySrc || fullSrc;
    const displayAnimated = Boolean(block.displayAnimated ?? block.display_animated ?? block.animated);
    const animateOnScreen = Boolean(displayAnimated && animatedSrc && animatedSrc !== staticSrc);
    const initialSrc = animateOnScreen ? staticSrc : (displaySrc || fullSrc || staticSrc);
    const media = initialSrc
      ? `<img src="${escapeHtml(initialSrc)}" alt="${escapeHtml(block.caption || "表情")}" loading="lazy" decoding="async"${animateOnScreen ? ` data-animated="true" data-animated-src="${escapeHtml(animatedSrc)}" data-static-src="${escapeHtml(staticSrc)}"` : ""} />`
      : "";
    return `<div class="content-sticker"><span>${media}</span><small>${escapeHtml(block.caption || "表情")}</small></div>`;
  }
  const className = block.type === "action" ? "content-action" : block.type === "speech" ? "content-speech" : "content-message";
  return `<div class="${className}">${escapeHtml(block.text)}</div>`;
}
function formatMessageTime(timestamp, previousTimestamp = 0, estimated = false, clockFormatter = null) {
  const value = new Date(Number(timestamp) || Date.now());
  const previous = previousTimestamp ? new Date(Number(previousTimestamp)) : null;
  if (previous && value.getTime() - previous.getTime() <= 5 * 60 * 1000 && localDayKey(value) === localDayKey(previous)) return "";
  const now = new Date();
  const dayDiff = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() - new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()) / 86400000);
  const clock = clockFormatter && Number.isFinite(value.getTime())
    ? clockFormatter.format(value)
    : value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  let label;
  if (value.getFullYear() !== now.getFullYear()) label = `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")} ${clock}`;
  else if (dayDiff === 0) label = clock;
  else if (dayDiff === 1) label = `昨天 ${clock}`;
  else if (dayDiff === 2) label = `前天 ${clock}`;
  else if (dayDiff > 2 && dayDiff < 7) label = `星期${"日一二三四五六"[value.getDay()]} ${clock}`;
  else label = `${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")} ${clock}`;
  return estimated ? `${label}（估算）` : label;
}
function timelineNearBottom() {
  const timeline = $("timeline");
  return !timeline || timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 72;
}
let timelineScrollSnapshot = null;
function rememberTimelineViewport() {
  const timeline = $("timeline");
  if (!timeline || $("text-surface").hidden) return;
  const top = timeline.getBoundingClientRect().top;
  const anchor = [...timeline.querySelectorAll("[data-message-id]")].find(node => node.getBoundingClientRect().bottom > top);
  timelineScrollSnapshot = { characterId: state.selected, top: timeline.scrollTop, nearBottom: timelineNearBottom(), width: timeline.clientWidth, height: timeline.clientHeight,
    anchorId: anchor?.dataset.messageId || "", anchorOffset: anchor ? anchor.getBoundingClientRect().top - top : null };
}
const timelineResizeObserver = new ResizeObserver(() => {
  const timeline = $("timeline");
  const snapshot = timelineScrollSnapshot;
  if (!snapshot || snapshot.characterId !== state.selected || $("text-surface").hidden) return;
  if (snapshot.nearBottom && !state.timelineEndId) timeline.scrollTop = timeline.scrollHeight;
  else {
    const anchor = [...timeline.querySelectorAll("[data-message-id]")].find(node => node.dataset.messageId === snapshot.anchorId);
    timeline.scrollTop = snapshot.top;
    if (anchor && snapshot.anchorOffset !== null) timeline.scrollTop += anchor.getBoundingClientRect().top - timeline.getBoundingClientRect().top - snapshot.anchorOffset;
  }
  rememberTimelineViewport();
});
function observeTimelineViewport() {
  timelineResizeObserver.disconnect();
  timelineResizeObserver.observe($("timeline"));
  $("timeline").querySelectorAll(".message-body").forEach(node => timelineResizeObserver.observe(node));
  rememberTimelineViewport();
}
function starterPromptsMarkup() {
  return `<div class="starter-prompts" aria-label="开场建议"><button type="button" data-starter-prompt="晚上好，今天过得怎么样？">问候近况</button><button type="button" data-starter-prompt="最近有什么想聊的事吗？">聊聊最近</button><button type="button" data-starter-prompt="要一起去休息区喝杯茶吗？">邀请喝茶</button></div>`;
}
function usageMarkup(message) {
  const usage = message?.usage;
  if (!usage || typeof usage !== "object") return "";
  const total = Number(usage.total_tokens ?? usage.totalTokens ?? 0);
  const calls = Number(usage.provider_calls ?? usage.model_calls ?? usage.call_count ?? 0);
  const model = plain(usage.model || message.model);
  const values = [model, total > 0 ? `${total} tokens` : "", calls > 0 ? `${calls} 次模型调用` : ""].filter(Boolean);
  return values.length ? `<span class="message-usage">${escapeHtml(values.join(" · "))}</span>` : "";
}

async function preparePresenceCandidate(characterId, signal, stateRecoveryAttempt = 0, statePackage = state.worldPackage || "") {
  if (dialogueV2Enabled()) return { result: await performSceneAction("resolve", characterId, signal), recoveredState: false };
  try {
    const result = await api("/presence/resolve", { method: "POST", signal, body: JSON.stringify({ request_id: id(), character_id: characterId, state_package: statePackage }) });
    return { result, recoveredState: stateRecoveryAttempt > 0 };
  } catch (error) {
    if (stateRecoveryAttempt < 1 && statePackageRecoveryError(error) && !signal?.aborted) {
      return preparePresenceCandidate(characterId, signal, stateRecoveryAttempt + 1, "");
    }
    throw error;
  }
}
function signedPendingRendezvous(characterId = state.selected) {
  const decoded = decodeStatePackage(state.worldPackage || "");
  const container = decoded.pending_rendezvous ?? decoded.world?.pending_rendezvous;
  const authoritative = Object.hasOwn(decoded, "pending_rendezvous")
    || Object.hasOwn(decoded.world || {}, "pending_rendezvous");
  if (!container || typeof container !== "object" || Array.isArray(container)) return { authoritative, value: null };
  const value = container[characterId];
  return { authoritative: true, value: value && typeof value === "object" ? value : null };
}
function latestMovementMessage(thread = currentThread()) {
  for (const message of [...(thread?.messages || [])].reverse()) {
    if (message.role !== "assistant" || !message.movementStatus) continue;
    const status = plain(message.movementStatus.status);
    if (["joined", "cancelled", "expired"].includes(status)) return null;
    if (status === "character_waiting") return message;
  }
  return null;
}
async function dismissCurrentRendezvous(thread = currentThread()) {
  const message = latestMovementMessage(thread);
  if (message?.movementStatus?.status !== "character_waiting") return;
  state.rendezvousDismissals.add(message.id);
  if (thread === currentThread()) renderTimeline({ preserveScroll: true });
  await saveUiPreferences();
}
function rendezvousCardMarkup(message, presenting = false) {
  if (sceneActionsV2Enabled()) return "";
  const movement = message?.movementStatus;
  const pending = signedPendingRendezvous(message?.characterId);
  if (
    presenting
    || message?.communicationChannel !== "text"
    || movement?.status !== "character_waiting"
    || latestMovementMessage()?.id !== message.id
    || state.rendezvousDismissals.has(message.id)
  ) return "";
  if (pending.authoritative && !pending.value) return "";
  if (pending.value && plain(pending.value.location_id) && plain(pending.value.location_id) !== plain(movement.location_id)) return "";
  const packageDate = statePackageOrder(state.worldPackage || "").scheduleDate;
  const movementDate = plain(movement.schedule_date);
  if (movementDate && packageDate && movementDate !== packageDate) return "";
  const location = plain(movement.location_name || movement.display_name || movement.character_location || state.sceneByCharacter.get(message.characterId)?.character_location || "约定地点");
  return `<section class="rendezvous-card" aria-label="会合操作"><div><strong>她已先到${escapeHtml(location)}等你</strong><small>你可以现在去找她，也可以继续留在通讯器。</small></div><div class="rendezvous-actions"><button type="button" class="primary-button" data-rendezvous-go="${escapeHtml(message.id)}">去找她</button><button type="button" class="secondary-button" data-rendezvous-stay="${escapeHtml(message.id)}">留在通讯器</button></div></section>`;
}
function bindTimelineActions() {
  document.querySelectorAll("[data-retry-message]").forEach((button) => { button.onclick = () => retryMessage(button.dataset.retryMessage); });
  document.querySelectorAll("[data-feedback-message]").forEach((button) => { button.onclick = () => openFeedback(button.dataset.feedbackMessage); });
  document.querySelectorAll("[data-starter-prompt]").forEach((button) => {
    button.onclick = () => {
      $("message-input").value = button.dataset.starterPrompt;
      updateInputCount();
      scheduleDraftSave();
      $("message-input").focus();
    };
  });
  document.querySelectorAll("[data-rendezvous-go]").forEach((button) => {
    button.onclick = async () => {
      if (globalRequestBusy()) return;
      button.disabled = true;
      await arriveInPerson();
    };
  });
  document.querySelectorAll("[data-rendezvous-stay]").forEach((button) => {
    button.onclick = async () => {
      state.rendezvousDismissals.add(button.dataset.rendezvousStay || "");
      renderTimeline({ preserveScroll: true });
      await saveUiPreferences();
    };
  });
  if ($("load-older-messages")) $("load-older-messages").onclick = () => { void loadOlderMessages(); };
  if ($("load-newer-messages")) $("load-newer-messages").onclick = () => loadNewerMessages();
}
function renderCurrentRendezvous() {
  const root = $("current-rendezvous");
  if (!root) return;
  const parent = currentThread()?.channel === "in_person" ? $("composer") : $("text-surface");
  if (root.parentElement !== parent) {
    if (parent === $("composer")) parent.prepend(root);
    else parent.insertBefore(root, $("timeline"));
  }
  const pending = sceneActionsV2Enabled() ? signedPendingRendezvous().value : null;
  const thread = currentThread();
  const active = sceneActionsV2Enabled() && thread ? decodeSidecar(dialoguePackage(thread)).active_invitation : null;
  const invitation = active && ["accepted", "deferred", "proposed", "clarify"].includes(active.status) ? active : null;
  root.hidden = !pending && !invitation;
  if (!pending && !invitation) { root.replaceChildren(); return; }
  const key = plain(pending?.rendezvous_id || invitation.invitation_id);
  const location = pending?.location_name || (state.config?.movement_catalog || []).find(item => item.location_id === invitation?.location_id)?.display_name || "地点待定";
  const title = pending ? "约定" : ({ accepted: "稍后约定", deferred: "暂缓约定", proposed: "邀请待确认", clarify: "邀请待澄清" }[invitation.status]);
  const collapsed = state.rendezvousDismissals.has(key);
  root.innerHTML = `<div><strong>${title} · ${escapeHtml(location)}</strong>${!pending && !collapsed ? '<small>时间待定 · 确认后再赴约</small>' : ""}${collapsed
    ? '<button type="button" class="secondary-button" data-current-rendezvous="open">查看约定</button>'
    : `${pending ? '<button type="button" class="primary-button" data-current-rendezvous="go">赴约</button>' : ""}<button type="button" class="secondary-button" data-current-rendezvous="stay">留在通讯器</button><button type="button" class="secondary-button" data-current-rendezvous="cancel">取消约定</button>`}</div>`;
  root.querySelectorAll("[data-current-rendezvous]").forEach(button => {
    button.disabled = globalRequestBusy() || !state.writerOwned;
    button.onclick = async () => {
      if (globalRequestBusy() || !state.writerOwned) return;
      const action = button.dataset.currentRendezvous;
      if (action === "open" || action === "stay") {
        if (action === "open") state.rendezvousDismissals.delete(key); else state.rendezvousDismissals.add(key);
        renderCurrentRendezvous();
        root.querySelector(`[data-current-rendezvous="${action === "stay" ? "open" : pending ? "go" : "cancel"}"]`)?.focus({ preventScroll: true });
        await saveUiPreferences(); return;
      }
      if (action === "go") return arriveInPerson();
      try {
        const characterId = state.selected;
        await runModeTransition(signal => performSceneAction("cancel_rendezvous", characterId, signal, { rendezvousId: key }), "正在取消约定……");
        renderCurrentRendezvous();
        if (state.selected === characterId) $("message-input").focus({ preventScroll: true });
      } catch (error) { showBanner(displayError(error)); }
    };
  });
}
function renderTimeline({ forceScroll = false, preserveScroll = false } = {}) {
  renderCurrentRendezvous();
  // Render the saved window when the communicator becomes visible. Rebuilding
  // hidden history during a stage transition adds work and loses scroll geometry.
  if (featureEnabled("motion_v2") && $("text-surface").hidden) return;
  const thread = currentThread();
  const allMessages = surfaceHistory();
  const window = historyWindow(allMessages, state.timelineEndId, state.timelineVisibleLimit);
  const messages = window.messages;
  const timeline = $("timeline");
  timeline.style.overflowAnchor = "none";
  // Automatic anchoring is immediate. CSS smooth scrolling can otherwise
  // leave every render reading an intermediate position from the last update.
  timeline.style.scrollBehavior = "auto";
  const viewport = timelineScrollSnapshot?.characterId === state.selected ? timelineScrollSnapshot : null;
  const wasNearBottom = viewport ? viewport.nearBottom : timelineNearBottom();
  const previousTop = viewport?.top ?? timeline.scrollTop;
  const timelineTop = timeline.getBoundingClientRect().top;
  const anchorNode = [...timeline.querySelectorAll("[data-message-id]")].find(node => node.getBoundingClientRect().bottom > timelineTop);
  const anchorId = viewport?.anchorId || anchorNode?.dataset.messageId;
  const anchorOffset = viewport?.anchorOffset ?? (anchorNode ? anchorNode.getBoundingClientRect().top - timelineTop : null);
  const previousCount = Number(timeline.dataset.messageCount || 0);
  const older = thread?.hasOlderMessages || window.start > 0 ? '<button id="load-older-messages" class="secondary-button load-older-messages" type="button">加载更早的 40 条</button>' : "";
  const newer = window.hasNewer ? '<button id="load-newer-messages" class="secondary-button load-older-messages" type="button">查看较新的 40 条</button>' : "";
  if (!messages.length) {
    const emptyCopy = "从一句问候开始吧。历史只保存在此浏览器。";
    // 雪境 ADV (P5)：空会话用角色色问候卡取代纯灰文字；文案不变。关闭态保持原样。
    const emptyBody = snowAdvActive() && currentCharacter()
      ? `<div class="snow-empty-greeting"><span class="snow-empty-avatar" aria-hidden="true">${escapeHtml((currentCharacter().display_name || "").slice(0, 1))}</span><p>${escapeHtml(emptyCopy)}</p></div>`
      : `<p>${escapeHtml(emptyCopy)}</p>`;
    reconcileMarkup(timeline, `${older}${timelineTypingMarkup()}<div class="empty-conversation">${emptyBody}${starterPromptsMarkup()}</div>`);
    timeline.dataset.messageCount = "0";
    bindTimelineActions();
    observeTimelineViewport();
    return;
  }
  // Resolve the device locale/timezone once per render, keeping live changes
  // while avoiding a formatter allocation for every historical timestamp.
  const clockFormatter = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", hour12: false });
  reconcileMarkup(timeline, older + messages.map((message, index) => {
    const failed = message.status === "failed";
    const presenting = message.role === "assistant" && presentationFor()?.messageId === message.id;
    const tools = failed && message.role === "user"
      ? message.errorCode === "request_result_unavailable"
        ? '<span class="message-recovery-note">恢复窗口已结束，未重新生成。</span>'
        : message.errorCode === "subscription_result_unknown"
        ? '<span class="message-recovery-note">结果尚未确认，请先检查个人连接器，避免重复发送。</span>'
        : `<button type="button" data-retry-message="${escapeHtml(message.id)}"${globalRequestBusy() ? " disabled" : ""}>重试</button>`
      : message.role === "assistant" && !presenting ? `<button type="button" data-feedback-message="${escapeHtml(message.id)}">反馈本条</button>` : "";
    const time = formatMessageTime(message.createdAt, messages[index - 1]?.createdAt || 0, message.createdAtEstimated, clockFormatter);
    const avatar = message.role === "user" ? analystAvatarMarkup({ priority: index === messages.length - 1 }) : avatarMarkup(currentCharacter(), { thumbnail: true, priority: index === messages.length - 1 });
    const blocks = message.role === "assistant" ? visibleBlocksFor(message) : message.contentBlocks;
    return `${time ? `<div class="message-time" data-ui-key="time:${escapeHtml(message.id)}" aria-label="${escapeHtml(time)}">${escapeHtml(time)}</div>` : ""}<article class="message ${message.role} ${message.status}" data-message-id="${escapeHtml(message.id)}"><div class="message-avatar">${avatar}</div><div class="message-body"><span class="meta"><span>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")}</span><span>${message.partial ? "回复未完成 · 已保留收到的内容" : failed ? "生成失败" : "文字通讯"}</span></span><div class="message-content-stack">${blocks.map(blockHtml).join("")}</div>${message.role === "assistant" ? rendezvousCardMarkup(message, presenting) + evidenceSourcesMarkup(message) : ""}<div class="message-tools">${tools}${usageMarkup(message)}</div></div></article>`;
  }).join("") + newer + (window.hasNewer ? "" : timelineTypingMarkup()));
  timeline.dataset.messageCount = String(messages.length);
  bindTimelineActions();
  bindAvatarImages(timeline);
  for (const message of messages) {
    if (state.seenMessageEntries.has(message) || state.paintedMessageIds.has(message.id)) continue;
    markMessageEntrySeen(message);
    // Historical rows and background arrivals must not animate on re-render.
    if (!featureEnabled("motion_v2") || quietPresentation() || message.createdAtEstimated || Date.now() - message.createdAt > 3000 || !wasNearBottom) continue;
    const node = [...timeline.querySelectorAll("[data-message-id]")].find(item => item.dataset.messageId === message.id);
    if (!node) continue;
    if (snowAdvActive()) {
      // 雪境 ADV (P5)：气泡 pop-in 由 CSS 关键帧按动态档位驱动（auto 上浮+淡入、
      // light 仅淡入、off 无），只动 transform/opacity。加类即可，不再用 WAAPI。
      node.classList.add("is-snow-pop");
    } else {
      void transientMotion.play(node, [{ opacity: .25, transform: "translateY(6px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 180, easing: "cubic-bezier(.22,.72,.24,1)" });
    }
  }
  const newMessages = allMessages.length > previousCount;
  timeline.dataset.messageCount = String(allMessages.length);
  if (window.hasNewer) $("new-replies").hidden = false;
  if (!preserveScroll && (forceScroll || wasNearBottom || previousCount === 0)) {
    timeline.scrollTop = timeline.scrollHeight;
    $("new-replies").hidden = !window.hasNewer;
  } else if (!preserveScroll) {
    // Keep the same historical row at the same viewport position when reply
    // bubbles, media or time labels are appended below the reader.
    const anchor = [...timeline.querySelectorAll("[data-message-id]")].find(node => node.dataset.messageId === anchorId);
    timeline.scrollTop = previousTop;
    if (anchor && anchorOffset !== null) timeline.scrollTop += anchor.getBoundingClientRect().top - timeline.getBoundingClientRect().top - anchorOffset;
    if (newMessages && messages[messages.length - 1]?.role === "assistant") $("new-replies").hidden = false;
  }
  sampleVisibleReplyPaints();
  observeTimelineViewport();
  const latestAssistant = [...messages].reverse().find((message) => message.role === "assistant" && message.status === "sent" && !presentationFor()?.messageId);
  if (latestAssistant && timeline.dataset.announcedMessageId !== latestAssistant.id) {
    timeline.dataset.announcedMessageId = latestAssistant.id;
    $("conversation-announcer").textContent = `${currentCharacter()?.display_name || "角色"}的新回复已显示`;
  }
}
function latestInPersonMessage(role = "") {
  const thread = currentThread();
  return [...(thread?.messages || [])].reverse().find((message) => !state.readyReplyIds.has(message.id)
    && (!thread.conversationSegmentId || message.conversationSegmentId === thread.conversationSegmentId)
    && message.communicationChannel === "in_person" && (message.status !== "failed" || message.partial) && (!role || message.role === role)) || null;
}
function typewriterCompleted(key, value) {
  const announcement = value
    ? `${currentCharacter()?.display_name || "角色"}：${value}`
    : "";
  return Boolean(
    key
    && state.typewriter.key === key
    && state.typewriter.fullText === ""
    && state.typewriter.displayedText === value
    && $("stage-speech")?.textContent === value
    && $("stage-announcer")?.dataset.key === key
    && (!value || $("stage-announcer")?.textContent === announcement)
  );
}
function waitForTypewriterCompletion(key, text, signal) {
  const value = plain(text);
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  if (typewriterCompleted(key, value)) return Promise.resolve(true);
  return new Promise((resolve, reject) => {
    const complete = () => {
      if (!typewriterCompleted(key, value)) return;
      cleanup();
      resolve(true);
    };
    const abort = () => {
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    };
    const cleanup = () => {
      document.removeEventListener("project-snow:typewriter-complete", complete);
      signal?.removeEventListener("abort", abort);
    };
    document.addEventListener("project-snow:typewriter-complete", complete);
    signal?.addEventListener("abort", abort, { once: true });
    complete();
  });
}
function finishTypewriter(key, value) {
  if (state.typewriter.key !== key) return false;
  window.clearTimeout(state.typewriter.timer);
  state.typewriter.timer = 0;
  state.typewriter.displayedText = value;
  state.typewriter.fullText = "";
  const speechNode = $("stage-speech");
  speechNode.classList.remove("is-typing");
  speechNode.classList.remove("is-revealing");
  speechNode.setAttribute("aria-busy", "false");
  speechNode.textContent = value;
  if (value) recordReplyPaint(latestInPersonMessage("assistant")?.requestId, speechNode);
  const announcement = value
    ? `${currentCharacter()?.display_name || "角色"}：${value}`
    : "";
  if (
    value
    && (
      $("stage-announcer").dataset.key !== key
      || $("stage-announcer").textContent !== announcement
    )
  ) {
    $("stage-announcer").dataset.key = key;
    $("stage-announcer").textContent = announcement;
  }
  document.dispatchEvent(new Event("project-snow:typewriter-complete"));
  syncFastForward();
  return true;
}
function renderTypewriter(text, key, animate = true) {
  const value = plain(text);
  const speechNode = $("stage-speech");
  if (
    state.typewriter.key === key
    && (state.typewriter.fullText === value || state.typewriter.displayedText === value)
  ) {
    if (state.typewriter.fullText === value && state.typewriter.timer) {
      if (!animate || reducedMotion()) {
        finishTypewriter(key, value);
        return;
      }
      speechNode.classList.remove("is-typing");
      speechNode.classList.add("is-revealing");
      speechNode.setAttribute("aria-busy", "true");
      if (speechNode.textContent !== state.typewriter.displayedText) speechNode.textContent = state.typewriter.displayedText;
      return;
    }
    if (typewriterCompleted(key, value)) return;
    if (state.typewriter.displayedText === value) {
      finishTypewriter(key, value);
      return;
    }
  }
  const previousKey = state.typewriter.key;
  const previousDisplayed = state.typewriter.displayedText;
  window.clearTimeout(state.typewriter.timer);
  state.typewriter.key = key;
  state.typewriter.fullText = value;
  state.typewriter.displayedText = "";
  if (!animate || !value || quietPresentation()) {
    finishTypewriter(key, value);
    return;
  }
  const prefix = previousKey === key && value.startsWith(previousDisplayed)
    ? previousDisplayed
    : "";
  const characters = graphemeSegments(value);
  let index = graphemeSegments(prefix).length;
  let displayed = prefix;
  const plan = typewriterRevealPlan(characters, index);
  let planIndex = 0;
  speechNode.classList.remove("is-typing");
  speechNode.classList.add("is-revealing");
  speechNode.setAttribute("aria-busy", "true");
  speechNode.textContent = prefix;
  const revealNext = () => {
    if (state.typewriter.key !== key || state.typewriter.fullText !== value) return;
    state.typewriter.timer = 0;
    const step = plan.steps[planIndex];
    if (!step) {
      finishTypewriter(key, value);
      return;
    }
    displayed += characters.slice(index, step.end).join("");
    index = step.end;
    planIndex += 1;
    speechNode.textContent = displayed;
    state.typewriter.displayedText = displayed;
    if (displayed) recordReplyPaint(latestInPersonMessage("assistant")?.requestId, speechNode);
    if (index >= characters.length) {
      finishTypewriter(key, value);
      return;
    }
    state.typewriter.timer = window.setTimeout(
      revealNext,
      step.delayAfter,
    );
  };
  state.typewriter.timer = window.setTimeout(revealNext, featureEnabled("chat_latency_v2") ? 0 : plan.initialDelay);
  syncFastForward();
}
function renderStage() {
  const renderSequence = ++state.stageRenderSequence;
  const character = currentCharacter();
  const assistantMessage = latestInPersonMessage("assistant");
  const expressionState = expressionStateForMessage(assistantMessage);
  const artNode = $("stage-character-art");
  const livePresentation = presentationFor();
  const motionPresentation = livePresentation?.messageId === assistantMessage?.id ? livePresentation : null;
  const motionRequestSequence = state.stageMotionRequestSequence;
  let artReady = Promise.resolve(false);
  if ($("chat-app").dataset.channel !== "in_person") {
    cancelStageArt({ hide: true });
    if (featureEnabled("chat_latency_v2")) {
      window.clearTimeout(state.typewriter.timer);
      state.typewriter = { key: "", timer: 0, fullText: "", displayedText: "" };
      return;
    }
  } else {
    artReady = updateStageCharacterArt(artNode, character, expressionState,
      normalizePerformanceId(assistantMessage?.performanceId ?? assistantMessage?.performance_id, assistantMessage?.communicationChannel)
    );
    void artReady.then((ready) => {
      if (ready && motionPresentation) {
        playStageMotion(artNode, assistantMessage, motionPresentation, motionRequestSequence);
      }
      if (ready) applyStageActorAnchor(artNode);
    });
  }
  const pending = typingStateFor();
  const speechKey = assistantMessage ? speechTypewriterKey(assistantMessage) : "";
  const liveReply = pending?.phase === "presenting" && pending.channel === "in_person"
    && assistantMessage?.requestId === pending.requestId;
  if (!featureEnabled("chat_latency_v2") && liveReply && $("chat-app").dataset.channel === "in_person" && state.typewriter.key !== speechKey) {
    // Repeated renders share one deadline. A stalled image can delay the first
    // spoken text only briefly, and late completions cannot repaint a new scene.
    if (state.stageSpeechGate?.key !== speechKey) {
      state.stageSpeechGate = { key: speechKey, deadline: performance.now() + STAGE_SPEECH_SYNC_WAIT_MS };
    }
    const remaining = Math.max(0, state.stageSpeechGate.deadline - performance.now());
    if (remaining > 0) {
      void Promise.race([artReady, delay(remaining)]).then(() => {
        if (renderSequence !== state.stageRenderSequence || currentCharacter()?.character_id !== character?.character_id
          || $("chat-app").dataset.channel !== "in_person") return;
        renderStageSpeech(assistantMessage);
      });
      return;
    }
  }
  renderStageSpeech(assistantMessage);
}
function renderStageSpeech(assistantMessage) {
  const pending = typingStateFor();
  const speechNode = $("stage-speech");
  const hasConfirmedBody = assistantMessage?.requestId === pending?.requestId && assistantMessage?.committedBlocks?.length;
  if (!hasConfirmedBody && pending?.channel === "in_person" && ["connecting", "typing", "arrival", "segment"].includes(pending.phase)) {
    window.clearTimeout(state.typewriter.timer);
    state.typewriter.timer = 0;
    if (pending.phase !== "segment") {
      state.typewriter.key = "";
      state.typewriter.fullText = "";
      state.typewriter.displayedText = "";
    }
    speechNode.classList.remove("is-revealing");
    speechNode.classList.add("is-typing");
    speechNode.setAttribute("aria-busy", "true");
    speechNode.innerHTML = typingIndicatorMarkup(characterById(pending.characterId), { includeAvatar: false });
    $("stage-open-feedback").disabled = true;
    $("stage-open-feedback").dataset.messageId = "";
    syncStageQuickMenu();
    return;
  }
  speechNode.classList.remove("is-typing");
  speechNode.setAttribute("aria-busy", "false");
  const visibleAssistantBlocks = assistantMessage ? visibleBlocksFor(assistantMessage) : [];
  const action = inPersonSurfaceText(visibleAssistantBlocks, "action");
  const completeSpeech = inPersonSurfaceText(visibleAssistantBlocks, "speech");
  const conversationStarted = (currentThread()?.messages || []).some(message => message.communicationChannel === "in_person"
    && message.source !== "presence_arrival" && message.conversationSegmentId === currentThread()?.conversationSegmentId);
  $("stage-narration").textContent = action || (!assistantMessage && !conversationStarted ? plain(state.scene?.character_activity) : "");
  const speech = completeSpeech || (!assistantMessage && !conversationStarted ? "场景已经建立。你可以说些什么，也可以只描述一个动作。" : "");
  const speechKey = assistantMessage
    ? completeSpeech
      ? speechTypewriterKey(assistantMessage)
      : `${plain(assistantMessage.characterId)}:${plain(assistantMessage.requestId)}:${plain(assistantMessage.id)}:placeholder`
    : `scene:${state.selected}:${state.scene?.visual_key || "generic"}`;
  const livePresentation = presentationFor();
  const liveReply = assistantMessage && (livePresentation?.messageId === assistantMessage.id
    || pending?.channel === "in_person" && pending.requestId === assistantMessage.requestId);
  // Confirmed replies take priority over decorative arrival/scene transitions,
  // including recovered replies and legacy arrival reactions. Cancel before
  // typewriting or measuring paint; renderScene may have just started a veil.
  if (liveReply && (completeSpeech || action)
      && (snowPushContext || state.snowVeilAnimation || state.snowVeilTimers.length)) cancelSnowScenePresentation();
  renderTypewriter(speech, speechKey, !featureEnabled("chat_latency_v2") || Boolean(liveReply));
  if (assistantMessage?.partial) {
    $("stage-presence-status").hidden = false;
    $("stage-presence-status").textContent = "回复未完成，已保留收到的内容。";
  }
  if (completeSpeech && quietPresentation()) recordReplyPaint(assistantMessage?.requestId, speechNode);
  $("stage-open-feedback").disabled = !assistantMessage || Boolean(presentationFor()?.messageId === assistantMessage?.id);
  $("stage-open-feedback").dataset.messageId = assistantMessage?.id || "";
  syncStageQuickMenu();
}
function evidenceSourcesMarkup(message) {
  const sources = message?.evidenceSources || [];
  return sources.length ? `<details class="evidence-sources"><summary>查看原作来源</summary><ul>${sources.map(source => `<li>${escapeHtml(source.title || "公开原作资料")}</li>`).join("")}</ul></details>` : "";
}
function renderTranscript() {
  if (!$("transcript-panel").classList.contains("open")) return;
  const messages = surfaceHistory("transcript");
  const window = historyWindow(messages, state.transcriptEndId, state.transcriptVisibleLimit);
  const older = currentThread()?.hasOlderMessages || window.start > 0 ? '<button id="transcript-older" class="secondary-button load-older-messages" type="button">加载更早的 40 条</button>' : "";
  const newer = window.hasNewer ? '<button id="transcript-newer" class="secondary-button load-older-messages" type="button">查看较新的 40 条</button>' : "";
  reconcileMarkup($("transcript-content"), older + (window.messages.length ? window.messages.map((message) => {
    const blocks = message.role === "assistant" && message.displayBlocks?.length ? message.displayBlocks : message.contentBlocks;
    return `<article class="transcript-entry" data-message-id="${escapeHtml(message.id)}"><strong>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")} · ${message.communicationChannel === "text" ? "文字通讯" : "面对面"} · ${escapeHtml(new Date(message.createdAt || Date.now()).toLocaleString())}</strong><p>${blocks.map((block) => block.type === "sticker" ? `〔表情〕${escapeHtml(block.caption || "表情")}` : `${block.type === "action" ? "〔动作〕" : ""}${escapeHtml(block.text)}`).join("\n\n")}</p>${evidenceSourcesMarkup(message)}</article>`;
  }).join("") : "<p>当前角色还没有本地记录。</p>") + newer);
  if ($("transcript-older")) $("transcript-older").onclick = () => { void loadOlderMessages("transcript"); };
  if ($("transcript-newer")) $("transcript-newer").onclick = () => loadNewerMessages("transcript");
}

function renderInfo() {
  const character = currentCharacter();
  const scene = state.scene || {};
  $("info-content").innerHTML = character ? `<article class="info-card"><strong>${escapeHtml(character.display_name)}</strong><p>也可用名称：${escapeHtml((character.aliases || []).join("、"))}</p></article><article class="info-card"><strong>当前场景</strong><p>角色位置：${escapeHtml(scene.character_location || "未建立")}</p><p>当前活动：${escapeHtml(scene.character_activity || "未建立")}</p><p>分析员位置：${escapeHtml(scene.analyst_location || "未定位")}</p><p>${scene.co_located ? "你们目前同处一地。" : "目前还不在同一地点。"}</p></article>` : "<p>请选择角色。</p>";
}
function renderScene() {
  const scene = state.scene || {};
  const visualKey = SCENE_KEYS.has(scene.visual_key) ? scene.visual_key : "generic";
  $("in-person-surface").dataset.scene = visualKey;
  const backdrop = SCENE_ASSET_URLS[visualKey] || `/assets/immersive/scenes/${visualKey}.svg`;
  if ($("scene-backdrop").getAttribute("src") !== backdrop) $("scene-backdrop").src = backdrop;
  syncSnowWorld();
  maybePlaySnowVeil(scene, visualKey);
  $("stage-location").textContent = scene.character_location || "场景尚未建立";
  $("stage-activity").textContent = scene.character_activity || "选择角色后读取当前位置";
  const presenceLabel = scene.character_location ? `去见她 · ${scene.character_location}` : "去见她";
  $("go-in-person-label").textContent = presenceLabel;
  $("go-in-person").setAttribute("aria-label", presenceLabel);
  $("go-in-person").title = presenceLabel;
  renderStage();
  renderInfo();
}
function movementCatalog() {
  const configuredCatalog = state.config?.movement_catalog;
  const values = Array.isArray(configuredCatalog)
    ? configuredCatalog
    : configuredCatalog && typeof configuredCatalog === "object"
      ? Object.entries(configuredCatalog).map(([locationId, value]) => typeof value === "string" ? { location_id: locationId, display_name: value } : { location_id: locationId, ...value })
      : [];
  return values
    .filter((item) => item && (item.display_name || item.name || item.location_name))
    .filter((item) => !["base_canteen", "canteen"].includes(plain(item.location_id)))
    .slice(0, 20);
}
function movementInvitationFor(item, name) {
  const template = plain(item?.invitation_text || item?.invitation_template || item?.invitation || "现在一起去{location_name}吗？");
  return template
    .replaceAll("${location_name}", name)
    .replaceAll("{location_name}", name)
    .replaceAll("{location}", name)
    .trim();
}
function openMovementShortcuts() {
  if (globalRequestBusy()) return;
  const values = movementCatalog();
  const root = $("movement-options");
  const invitation = $("movement-invitation");
  invitation.value = "";
  invitation.readOnly = true;
  $("send-movement-invitation").disabled = true;
  $("movement-dialog").dataset.locationId = "";
  if (!values.length) {
    root.innerHTML = '<p class="modal-context">地点目录正在准备中，你仍可直接输入自然语言邀请。</p>';
  } else {
    root.innerHTML = values.map((item) => {
      const name = plain(item.display_name || item.name || item.location_name);
      return `<button type="button" role="radio" data-movement-id="${escapeHtml(item.location_id || "")}" data-movement-name="${escapeHtml(name)}" aria-checked="false" tabindex="-1"><strong>${escapeHtml(name)}</strong>${item.activity_name ? `<small>${escapeHtml(item.activity_name)}</small>` : ""}</button>`;
    }).join("");
    root.querySelector("[role=radio]")?.setAttribute("tabindex", "0");
    root.querySelectorAll("[data-movement-name]").forEach((button, index) => {
      button.onclick = () => {
        root.querySelectorAll("[data-movement-name]").forEach((item) => {
          item.setAttribute("aria-checked", String(item === button));
          item.tabIndex = item === button ? 0 : -1;
        });
        $("movement-dialog").dataset.locationId = button.dataset.movementId || "";
        invitation.value = movementInvitationFor(values[index], button.dataset.movementName || "");
        $("send-movement-invitation").disabled = false;
        $("send-movement-invitation").focus();
      };
      button.onkeydown = (event) => {
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const radios = [...root.querySelectorAll("[role=radio]")];
        const current = radios.indexOf(button);
        const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
        const target = event.key === "Home" ? 0 : event.key === "End" ? radios.length - 1 : (current + direction + radios.length) % radios.length;
        radios[target].focus();
        radios[target].click();
      };
    });
  }
  $("movement-dialog").showModal();
}
function renderAll() { renderTimeline(); renderStage(); renderTranscript(); renderInfo(); renderRequestStatus(); }

function updateComposerAvailability() {
  const selected = Boolean(state.selected);
  const inPerson = currentThread()?.channel === "in_person";
  const requestPending = globalRequestBusy() || !state.writerOwned;
  $("apply-build-update").disabled = globalRequestBusy();
  $("message-input").disabled = !selected || !state.writerOwned;
  $("send-message").disabled = !selected || requestPending;
  $("go-in-person").disabled = !selected || requestPending;
  $("open-communicator").disabled = !selected || requestPending;
  $("open-movement-shortcuts").disabled = !selected || requestPending;
  $("send-movement-invitation").disabled = !selected || requestPending || !$("movement-invitation").value.trim() || !$("movement-dialog").dataset.locationId;
  $("toggle-sticker").hidden = inPerson;
  $("toggle-action").hidden = !inPerson;
  $("toggle-sticker").disabled = !selected || requestPending || inPerson;
  $("toggle-action").disabled = !selected || requestPending || !inPerson;
  if ($("confirm-presence-transition")) $("confirm-presence-transition").disabled = !selected || requestPending;
  if ($("stay-on-communicator")) $("stay-on-communicator").disabled = !selected || requestPending;
  if ($("delete-character-history")) $("delete-character-history").disabled = requestPending;
  if ($("clear-all-history")) $("clear-all-history").disabled = requestPending;
  document.querySelectorAll("[data-retry-message]").forEach((button) => { button.disabled = requestPending; });
  document.querySelectorAll(".request-recovery").forEach((button) => { button.disabled = requestPending; });
  $("current-rendezvous")?.querySelectorAll("button").forEach(button => { button.disabled = requestPending; });
  $("analyst-action-field").hidden = !inPerson || !state.actionComposerOpen;
  $("toggle-action").setAttribute("aria-expanded", String(inPerson && state.actionComposerOpen));
  $("message-input").placeholder = !selected ? "选择角色后输入消息……" : configured() ? (inPerson ? "说些什么，也可只填写动作……" : "输入文字通讯……") : "可浏览历史；发送前请在设置中配置模型";
  renderSelectedSticker();
}
async function setChannel(channel, persist = true, targetThread = null) {
  const thread = targetThread || currentThread();
  const restoreCollapsedSidebarFocus = channel !== "in_person"
    && $("chat-app").classList.contains("sidebar-collapsed");
  // Selection already saved the previous owner's composer. Its stale DOM
  // must not overwrite the newly selected character's persisted draft.
  if (persist && thread && thread.characterId === state.selected) await saveDraftNow();
  if (thread && thread.channel !== (channel === "in_person" ? "in_person" : "text")) {
    cancelPresentationQueue(thread.characterId);
  }
  if (thread) {
    thread.channel = channel === "in_person" ? "in_person" : "text";
    if (persist) await dbPutThread(thread);
  }
  // A character switch may happen while the persistence write is pending.
  // Only the selected thread owns the visible surface and composer controls.
  if (thread && thread.characterId !== state.selected) return;
  if (channel !== "in_person") cancelSnowScenePresentation();
  const composerChanged = state.composerDraftKey !== draftKey(state.selected, channel);
  if (channel === "in_person" && !$("text-surface").hidden) rememberTimelineViewport();
  const snapshot = prepareSurfaceMotion(channel);
  if (composerChanged) state.actionComposerOpen = false;
  if (channel === "in_person") state.selectedSticker = null;
  else cancelStageArt({ hide: true });
  $("chat-app").dataset.channel = channel;
  $("text-surface").hidden = channel === "in_person";
  $("in-person-surface").hidden = channel !== "in_person";
  $("text-surface").inert = channel === "in_person";
  $("in-person-surface").inert = channel !== "in_person";
  $("chat-app").dataset.channel = channel === "in_person" ? "in_person" : "text";
  // 雪境 ADV P3：开关开启进入面对面时，角色栏改为覆盖式抽屉，默认关闭让舞台占满视口；
  // 桌面端用既有 sidebar-collapsed 机制（#open-stage-contacts 仍用 toggleContacts 开合），
  // 不持久化、不改旧 UI 的侧栏偏好语义。移动端（≤820px）沿用抽屉逻辑，无需处理。
  if (featureEnabled("ui_snow_adv") && channel === "in_person" && !window.matchMedia("(max-width: 820px)").matches
    && !$("chat-app").classList.contains("sidebar-collapsed")) {
    $("chat-app").classList.add("sidebar-collapsed");
    $("chat-app").dataset.snowAutoCollapsed = "1";
    $("contact-panel").setAttribute("aria-hidden", "true");
    $("contact-panel").inert = true;
  } else if (channel !== "in_person" && $("chat-app").dataset.snowAutoCollapsed === "1") {
    // 还原我们为面对面自动折叠的侧栏，避免把文字界面也折叠（不碰用户手动折叠的情况）。
    delete $("chat-app").dataset.snowAutoCollapsed;
    if (!window.matchMedia("(max-width: 820px)").matches) {
      $("chat-app").classList.remove("sidebar-collapsed");
      $("contact-panel").setAttribute("aria-hidden", "false");
      $("contact-panel").inert = false;
    }
  }
  syncContactToggleState();
  syncSnowWorld();
  renderAll();
  animateSurface(channel, snapshot);
  // Arrival can confirm the already-visible channel after an IndexedDB write.
  // Keep edits made during that wait; only a new character/channel restores a
  // different draft into the composer.
  if (composerChanged) restoreDraft();
  updateComposerAvailability();
  if (restoreCollapsedSidebarFocus) window.setTimeout(() => $("open-contacts")?.focus(), 0);
}
function requestHistory(messages, excludedId = "", segmentId = "") {
  return messages.filter((message) => message.id !== excludedId && !message.partial && !["failed", "pending"].includes(message.status) && (!segmentId || message.conversationSegmentId === segmentId)).slice(-24).map((message) => ({
    role: message.role,
    communication_channel: message.communicationChannel,
    content_blocks: wireBlocks(message.contentBlocks),
    created_at: new Date(message.createdAt || Date.now()).toISOString(),
  }));
}
function inputBlocks() {
  const channel = currentThread()?.channel || "text";
  const speech = $("message-input").value.trim();
  const action = $("action-input").value.trim();
  if (channel === "text") {
    const blocks = speech ? [{ type: "message", text: speech }] : [];
    if (state.selectedSticker) blocks.push({ type: "sticker", assetId: state.selectedSticker.asset_id, caption: state.selectedSticker.caption, src: state.selectedSticker.src, displaySrc: state.selectedSticker.display_src, thumbnailSrc: state.selectedSticker.thumbnail_src, animated: state.selectedSticker.animated, displayAnimated: state.selectedSticker.display_animated });
    return blocks;
  }
  return [action ? { type: "action", text: action } : null, speech ? { type: "speech", text: speech } : null].filter(Boolean);
}
function resizeMessageInput() {
  const input = $("message-input");
  if (!input.getClientRects().length) return;
  const style = getComputedStyle(input);
  const minimum = Number.parseFloat(style.minHeight) || 0;
  const maximum = Number.parseFloat(style.maxHeight) || Infinity;
  const borders = Number.parseFloat(style.borderTopWidth) + Number.parseFloat(style.borderBottomWidth);
  // While capped text still overflows, leave the live box alone so the browser
  // can keep scrolling the caret as typing continues. Resetting its height here
  // would undo that scroll. Deletion/widening that fits re-enters measurement.
  if (input.value && input.offsetHeight >= maximum && input.scrollHeight > input.clientHeight) return;
  const scrollTop = input.scrollTop;
  // Measure from the intrinsic single row so deletion and wider layouts shrink
  // again. Only the box changes; drafts, selection and manual scroll stay intact.
  input.style.height = "auto";
  input.style.overflowY = "hidden";
  // A long placeholder can wrap even when the composer has no draft.
  const height = input.value ? input.scrollHeight + borders : input.offsetHeight;
  input.style.height = `${Math.max(minimum, Math.min(maximum, height))}px`;
  input.style.overflowY = height > maximum ? "auto" : "hidden";
  input.scrollTop = scrollTop;
}
let messageInputResizeFrame = 0;
function scheduleMessageInputResize() {
  if (messageInputResizeFrame) return;
  messageInputResizeFrame = window.requestAnimationFrame(() => {
    messageInputResizeFrame = 0;
    resizeMessageInput();
  });
}
function updateInputCount() {
  const count = $("message-input").value.length + ($("action-input").value.length || 0);
  $("input-count").textContent = `${count} / 2000`;
  $("input-count").style.color = count > 2000 ? "#ff9dac" : "";
  const foot = $("input-count").closest(".composer-foot");
  if (foot) foot.hidden = count === 0;
  resizeMessageInput();
}
function renderSelectedSticker() {
  const root = $("selected-sticker");
  const sticker = state.selectedSticker;
  if (!root) return;
  root.hidden = !sticker || currentThread()?.channel === "in_person";
  if (!sticker || root.hidden) return;
  const image = $("selected-sticker-image");
  image.src = sticker.thumbnail_src || sticker.display_src || sticker.src || "";
  image.alt = sticker.caption || "已选择表情";
  $("selected-sticker-caption").textContent = sticker.caption || "发送时会单独作为一条消息";
}
function clearSelectedSticker() {
  state.selectedSticker = null;
  renderSelectedSticker();
  $("toggle-sticker").setAttribute("aria-expanded", "false");
  updateInputCount();
}
function renderStickerPicker() {
  const list = $("sticker-list");
  if (!list) return;
  const values = (state.stickers || []).slice(-60);
  const loading = Boolean(state.stickerLoadPromise) && !values.length;
  $("sticker-empty").hidden = loading || values.length > 0;
  $("load-more-stickers").hidden = !state.stickerHasMore || Boolean(state.stickerLoadPromise);
  document.querySelectorAll("[data-sticker-section]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.stickerSection === state.stickerSection));
    button.tabIndex = button.dataset.stickerSection === state.stickerSection ? 0 : -1;
  });
  if (loading) {
    list.setAttribute("aria-busy", "true");
    list.innerHTML = Array.from({ length: 12 }, () => '<span class="sticker-skeleton" aria-hidden="true"></span>').join("");
    return;
  }
  list.setAttribute("aria-busy", "false");
  list.innerHTML = values.map((sticker) => {
    const favorite = state.favoriteStickerIds.has(sticker.asset_id);
    return `<div class="sticker-choice-wrap"><button type="button" class="sticker-choice" data-sticker-id="${escapeHtml(sticker.asset_id)}" title="${escapeHtml(sticker.caption || "表情")}"><img src="${escapeHtml(sticker.thumbnail_src || sticker.display_src || sticker.src || "")}" alt="${escapeHtml(sticker.caption || "表情")}" loading="lazy" decoding="async" /><span>${escapeHtml(sticker.caption || "表情")}</span></button><button type="button" class="favorite-sticker" data-favorite-sticker="${escapeHtml(sticker.asset_id)}" aria-pressed="${favorite}" aria-label="${favorite ? "取消收藏" : "收藏"}${escapeHtml(sticker.caption || "表情")}">${favorite ? "★" : "☆"}</button></div>`;
  }).join("");
  list.querySelectorAll("[data-sticker-id]").forEach((button) => {
    button.onclick = () => {
      state.selectedSticker = state.stickerCatalog.get(button.dataset.stickerId) || state.stickers.find((item) => item.asset_id === button.dataset.stickerId) || null;
      if (state.selectedSticker) {
        state.recentStickerIds = [state.selectedSticker.asset_id, ...state.recentStickerIds.filter((assetId) => assetId !== state.selectedSticker.asset_id)].slice(0, 24);
        state.favoriteStickers.set(state.selectedSticker.asset_id, state.selectedSticker);
        void saveUiPreferences();
      }
      $("sticker-picker").close();
      $("toggle-sticker").setAttribute("aria-expanded", "false");
      updateComposerAvailability();
      if (state.selectedSticker) {
        toast(`已选择“${state.selectedSticker.caption || "表情"}”，发送时会单独作为一条消息。`);
        $("message-input").focus();
      }
    };
  });
  list.querySelectorAll("[data-favorite-sticker]").forEach((button) => {
    button.onclick = async () => {
      const assetId = button.dataset.favoriteSticker;
      const sticker = state.stickerCatalog.get(assetId) || state.stickers.find((item) => item.asset_id === assetId);
      if (state.favoriteStickerIds.has(assetId)) {
        state.favoriteStickerIds.delete(assetId);
        if (!state.recentStickerIds.includes(assetId)) state.favoriteStickers.delete(assetId);
      } else {
        state.favoriteStickerIds.add(assetId);
        if (sticker) state.favoriteStickers.set(assetId, sticker);
      }
      await saveUiPreferences();
      if (state.stickerSection === "favorites") state.stickers = localStickerSectionValues("favorites").filter(stickerMatchesLoadedFilter);
      renderStickerPicker();
    };
  });
}
async function openStickerPicker() {
  if (globalRequestBusy()) return;
  $("toggle-sticker").setAttribute("aria-expanded", "true");
  if (!$("sticker-picker").open) $("sticker-picker").showModal();
  renderStickerPicker();
  if (!state.stickers.length) await loadStickers({ reset: true });
}
async function selectStickerSection(section) {
  if (!["recent", "character", "generic", "favorites", "all"].includes(section)) return;
  if (state.stickerLoadPromise) await state.stickerLoadPromise;
  state.stickerSection = section;
  state.stickers = [];
  state.stickerCursor = null;
  state.stickerHasMore = true;
  renderStickerPicker();
  await loadStickers({ reset: true });
}
async function consumeChatStream(response, { characterId, requestId, fallbackChannel, committedReply = null, onCommitted = null, dialogueV2 = false }) {
  if (!response.body) throw new Error("stream_disconnected");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const streamedBlocks = new Map();
  let buffer = "";
  let completed = false;
  let contentBlocks = [];
  let usage = null;
  let returnedChannel = fallbackChannel;
  let pendingSceneState = null;
  let pendingStateEvent = null;
  let movementStatus = null;
  let recoveryAction = "none";
  let expressionState = "neutral";
  let stageMotion = "none";
  let performanceId = "";
  let serverTimings = null;
  let statePersistence = Promise.resolve();
  let statePackage = "";
  let sessionContextPackage = "";
  let sessionMemories = [];
  let sceneAction = null;
  let evidenceSources = [];
  const handlePacket = async (packet) => {
    const event = (packet.match(/^event: (.+)$/m) || [])[1];
    const data = (packet.match(/^data: (.+)$/m) || [])[1];
    if (!event || !data) return;
    const payload = JSON.parse(data);
    if (committedReply && event === "progress" && ownsTypingState(characterId, requestId)) {
      const labels = { queued: "正在排队…", preparing: "正在整理这次对话…", retrieving: "正在回想相关的事…", generating: "正在回复…", validating: "正在确认回复…", saving: "正在保存回复…" };
      const timing = replyTiming(requestId);
      if (labels[payload.phase]) {
        if (timing) timing.pendingProgressText = labels[payload.phase];
        if (timing?.feedbackAllowed) setRequestStatus(characterId, labels[payload.phase], requestId);
      }
    }
    if (committedReply && event === "committed_block") {
      if (committedReply.accept(payload)) onCommitted?.();
    }
    if (event === "delta") {
      const blockIndex = Number.isFinite(Number(payload.block_index)) ? Number(payload.block_index) : 0;
      const current = streamedBlocks.get(blockIndex) || { type: payload.block_type || (returnedChannel === "text" ? "message" : "speech"), text: "" };
      if (payload.block_type === "sticker") {
        current.asset_id = plain(payload.asset_id);
        current.assetId = current.asset_id;
        current.caption = plain(payload.caption);
        current.src = plain(payload.src);
        current.displaySrc = plain(payload.display_src);
        current.thumbnailSrc = plain(payload.thumbnail_src);
        current.animated = Boolean(payload.animated);
        current.displayAnimated = Boolean(payload.display_animated ?? payload.animated);
      } else current.text += plain(payload.text);
      if (payload.block_type) current.type = payload.block_type;
      streamedBlocks.set(blockIndex, current);
    }
    if (event === "state") {
      if (dialogueV2) {
        statePackage = plain(payload.state_package);
        pendingSceneState = payload.scene_state || pendingSceneState;
        pendingStateEvent = payload.state_event || pendingStateEvent;
        sessionContextPackage = plain(payload.session_context_package);
        sessionMemories = payload.session_memories || sessionMemories;
        sceneAction = payload.scene_action || sceneAction;
        evidenceSources = payload.evidence_sources || evidenceSources;
        return;
      }
      if (ownsTypingState(characterId, requestId)) {
        // Apply the same signed-package ordering gate in memory first. Local
        // disk failures cannot interrupt reading a server-confirmed done packet.
        const accepted = await saveWorldPackage(payload.state_package, { persist: false });
        if (accepted) {
          pendingSceneState = payload.scene_state || pendingSceneState;
          pendingStateEvent = payload.state_event || pendingStateEvent;
          const token = payload.state_package;
          statePersistence = statePersistence.then(async () => {
            if (state.worldPackage !== token) return;
            const order = statePackageOrder(token);
            await storePut("app_state", { key: "world", statePackage: token, revision: order.revision, scheduleDate: order.scheduleDate });
          }).catch(() => { useMemoryStorage(); });
        }
      }
    }
    if (event === "done") {
      committedReply?.verifyFinal(payload.content_blocks || []);
      completed = true;
      usage = payload.usage || null;
      serverTimings = payload.timings_ms && typeof payload.timings_ms === "object" ? payload.timings_ms : null;
      movementStatus = payload.movement_status && typeof payload.movement_status === "object" ? payload.movement_status : null;
      recoveryAction = plain(payload.recovery_action || "none");
      returnedChannel = payload.communication_channel || returnedChannel;
      expressionState = normalizeExpressionState(payload.expression_state, returnedChannel);
      stageMotion = normalizeStageMotion(payload.stage_motion, returnedChannel);
      performanceId = normalizePerformanceId(payload.performance_id, returnedChannel);
      contentBlocks = normalizeBlocks(payload.content_blocks, returnedChannel);
      if (dialogueV2) {
        statePackage = plain(payload.state_package) || statePackage;
        sessionContextPackage = plain(payload.session_context_package) || sessionContextPackage;
        sessionMemories = payload.session_memories || sessionMemories;
        sceneAction = payload.scene_action || sceneAction;
        evidenceSources = payload.evidence_sources || evidenceSources;
      }
      if (!contentBlocks.length) contentBlocks = normalizeBlocks([...streamedBlocks.values()], returnedChannel);
    }
    if (event === "error") {
      const error = chatError(payload.code || "chat_failed", payload);
      error.partial = Boolean(payload.partial);
      error.terminal = true;
      throw error;
    }
  };
  try {
    while (!completed) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const packets = buffer.split("\n\n");
      buffer = packets.pop() || "";
      for (const packet of packets) {
        await handlePacket(packet);
        if (completed) break;
      }
    }
    if (!completed && buffer.trim()) await handlePacket(buffer);
  } finally {
    // A verified done packet completes the request. Waiting for a socket EOF
    // can otherwise turn a saved reply into a timeout and offer a paid resend.
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (!completed) throw new Error("stream_disconnected");
  if (!contentBlocks.length) contentBlocks = normalizeBlocks([...streamedBlocks.values()], returnedChannel);
  if (!contentBlocks.length) throw new Error("upstream_invalid_response");
  return { contentBlocks, usage, returnedChannel, pendingSceneState, pendingStateEvent, movementStatus, recoveryAction, expressionState, stageMotion, performanceId, serverTimings, statePersistence, statePackage, sessionContextPackage, sessionMemories, sceneAction, evidenceSources };
}

function recoverableChatError(error) {
  if (error?.terminal || error?.partial) return false;
  const code = error instanceof Error ? error.message : plain(error);
  if (error instanceof Error && error.retryable === false) return false;
  return error instanceof TypeError || [
    "stream_disconnected",
    "request_in_progress",
    "provider_network_error",
    "provider_timeout",
    "request_timeout",
    "request_failed",
    "generation_queue_full",
    "generation_queue_timeout",
    "subject_generation_busy",
  ].includes(code);
}

function shouldReuseChatRequest(error) {
  const code = error instanceof Error ? error.message : plain(error);
  // retryable=false prevents another generation; uncertain results still need
  // their original UUID so a later reconciliation cannot charge a new request.
  if (["public_database_unavailable", "subscription_result_unknown", "stream_commit_invalid", "stream_commit_conflict", "stream_commit_sequence", "upstream_invalid_response"].includes(code) || error instanceof SyntaxError) return true;
  if (error instanceof Error && error.retryable === false) return false;
  return error instanceof TypeError || [
    "stream_disconnected",
    "request_in_progress",
    "request_failed",
    "provider_network_error",
    "provider_timeout",
    "request_timeout",
    "request_cancelled",
    "public_database_unavailable",
    "subscription_result_unknown",
    "generation_queue_full",
    "generation_queue_timeout",
    "subject_generation_busy",
  ].includes(code);
}

function compatibleRetrySnapshot(userMessage) {
  const candidate = state.retrySnapshots.get(userMessage.id)
    || (userMessage.requestId && userMessage.requestSnapshot
      ? { requestId: userMessage.requestId, payload: userMessage.requestSnapshot }
      : null);
  if (!candidate?.requestId || !candidate.payload || typeof candidate.payload !== "object") return null;
  const candidateRequestId = plain(candidate.requestId);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(candidateRequestId)) return null;
  const v2 = candidate.payload.endpoint === "/public/v2/chat/stream" && candidate.payload.envelope?.protocol === DIALOGUE_PROTOCOL;
  const chat = v2 ? candidate.payload.envelope.chat : candidate.payload;
  if (!chat || plain(chat.request_id) !== candidateRequestId) return null;
  if (plain(chat.character_id) !== plain(userMessage.characterId)) return null;
  if (plain(chat.message) !== plain(userMessage.content)) return null;
  if (plain(chat.communication_channel) !== plain(userMessage.communicationChannel)) return null;
  if (plain(chat.movement_location_id) !== plain(userMessage.movementLocationId)) return null;
  if (plain(chat.provider) !== state.provider || plain(chat.model) !== state.model) return null;
  if (state.provider === SUBSCRIPTION_PROVIDER && snapshotSubscriptionEffort(chat) !== state.reasoningEffort) return null;
  return { requestId: candidateRequestId, payload: structuredClone(chat), endpoint: v2 ? candidate.payload.endpoint : `${apiRoot}/chat/stream`,
    envelope: v2 ? structuredClone(candidate.payload.envelope) : null };
}

async function runChat(thread, userMessage, { stateRecoveryAttempt = 0 } = {}) {
  const sendStartedAt = performance.now();
  let retrySnapshot = stateRecoveryAttempt ? null : compatibleRetrySnapshot(userMessage);
  let requestId = plain(retrySnapshot?.requestId) || id();
  const characterId = thread.characterId;
  const requestEpoch = state.historyEpoch;
  const requestSegment = retrySnapshot?.envelope?.segment_id || thread.conversationSegmentId;
  const dialogueV2 = retrySnapshot ? Boolean(retrySnapshot.envelope) : dialogueV2Enabled();
  if (dialogueV2 && !retrySnapshot) userMessage.id = `user:${requestId}`;
  if (dialogueV2) await loadDialogueRecord(characterId);
  const buildRequestSnapshot = () => ({
    request_id: requestId,
    provider: state.provider,
    model: state.model,
    ...subscriptionReasoningPayload(),
    character_id: characterId,
    message: userMessage.content,
    communication_channel: userMessage.communicationChannel,
    content_blocks: wireBlocks(userMessage.contentBlocks),
    recent_history: requestHistory(thread.messages, userMessage.id, thread.conversationSegmentId),
    history_summary: thread.continuityDecision === "start_today" ? "" : [thread.summary || "", thread.pendingTopics?.length ? `可能想继续的话题：${thread.pendingTopics.join("；")}` : ""].filter(Boolean).join("\n"),
    state_package: state.worldPackage || "",
    continuity_decision: thread.continuityDecision || "",
    local_day_key: thread.localDayKey || localDayKey(),
    ...(userMessage.movementLocationId ? { movement_location_id: userMessage.movementLocationId } : {}),
  });
  let requestSnapshot = retrySnapshot?.payload || buildRequestSnapshot();
  if (!dialogueV2 && retrySnapshot && jsonBodyBytes({ ...requestSnapshot, credential: state.credential }) > PUBLIC_REQUEST_LIMIT_BYTES) {
    state.retrySnapshots.delete(userMessage.id);
    retrySnapshot = null;
    requestId = id();
    requestSnapshot = buildRequestSnapshot();
  }
  let boundedRequest;
  let envelope = dialogueV2 ? retrySnapshot?.envelope || { protocol: DIALOGUE_PROTOCOL, chat: requestSnapshot,
    session_context_package: dialoguePackage(thread), segment_id: requestSegment,
    memory_candidates: dialogueCandidates(characterId, userMessage.content) } : null;
  try {
    if (dialogueV2 && !retrySnapshot) envelope = { ...envelope, ...fitDialogueContext(envelope) };
    if (dialogueV2 && retrySnapshot) {
      boundedRequest = { ...requestSnapshot, credential: state.credential };
      if (jsonBodyBytes({ ...envelope, chat: boundedRequest }) > PUBLIC_REQUEST_LIMIT_BYTES) throw new Error("request_too_large");
    } else boundedRequest = fitPublicRequestPayload(
      { ...requestSnapshot, credential: state.credential },
      {
        arrays: [{ key: "recent_history", minimum: 0 }],
        texts: ["history_summary"],
        targetBytes: retrySnapshot ? PUBLIC_REQUEST_LIMIT_BYTES : PUBLIC_REQUEST_TARGET_BYTES - (dialogueV2 ? jsonBodyBytes({ ...envelope, chat: {} }) : 0),
      },
    );
  } catch (error) {
    if (!dialogueV2 || !retrySnapshot) state.retrySnapshots.delete(userMessage.id);
    userMessage.requestId = requestId;
    if (!dialogueV2 || !retrySnapshot) userMessage.requestSnapshot = null;
    userMessage.status = "failed";
    userMessage.errorCode = error instanceof Error ? error.message : "request_too_large";
    userMessage.errorStage = "content_validation";
    state.latest.set(characterId, { requestId, errorCode: userMessage.errorCode, errorStage: userMessage.errorStage });
    await dbPutThread(thread);
    if (state.selected === characterId) {
      renderAll();
      setRequestStatus(characterId, displayError(error));
    }
    updateComposerAvailability();
    return;
  }
  requestSnapshot = { ...boundedRequest };
  delete requestSnapshot.credential;
  if (envelope && !retrySnapshot) envelope = { ...envelope, chat: structuredClone(requestSnapshot) };
  const storedSnapshot = envelope ? { endpoint: "/public/v2/chat/stream", envelope: structuredClone(envelope) } : requestSnapshot;
  const requestBody = JSON.stringify(envelope ? { ...envelope, chat: boundedRequest } : boundedRequest);
  const requestEndpoint = retrySnapshot?.endpoint || (dialogueV2 ? "/public/v2/chat/stream" : `${apiRoot}/chat/stream`);
  // Optional, isolated analytics signal: identifiers only; never inspect requestBody.
  try {
    window.snowStatisticsRequest?.(requestId);
  } catch { /* Analytics must not affect chat submission. */ }
  const ownsRequest = () => ownsTypingState(characterId, requestId) && thread.historyEpoch === state.historyEpoch
    && state.historyEpoch === requestEpoch && thread.conversationSegmentId === requestSegment && state.writerOwned;
  const ownsVisibleRequest = () => ownsRequest() && state.selected === characterId;
  cancelPresentationQueue(characterId);
  state.chatRequestByCharacter.get(characterId)?.controller.abort();
  const requestController = new AbortController();
  let requestTimedOut = false;
  const requestTimeout = window.setTimeout(() => {
    requestTimedOut = true;
    requestController.abort();
  }, CHAT_STREAM_TIMEOUT_MS);
  state.chatRequestByCharacter.set(characterId, { requestId, controller: requestController });
  userMessage.requestId = requestId;
  userMessage.conversationSegmentId = requestSegment;
  userMessage.status = "pending";
  userMessage.errorCode = "";
  userMessage.errorStage = "";
  if (state.selected === characterId) renderAll();
  setTypingState({ characterId, requestId, channel: userMessage.communicationChannel, phase: "waiting" });
  setRequestStatus(characterId, "", requestId);
  const latencyV2 = featureEnabled("chat_latency_v2");
  const streamV2 = featureEnabled("verified_block_stream") || featureEnabled("verified_block_replay");
  const metrics = { sentAt: sendStartedAt, bodyReceivedAt: 0, firstPaintAt: 0, naturalWaitMs: 0, presented: false, transport: streamV2 ? "verified_blocks" : "complete_reply", segmentSpent: 0, lastBodyPaintAt: 0 };
  const publicProvider = ["openai", "deepseek", "dashscope", "zhipu", "moonshot"].includes(state.provider) ? state.provider : "other";
  metrics.provider = publicProvider;
  metrics.modelProfile = publicProvider !== "other" && Array.isArray(state.config?.model_profiles?.[publicProvider]) && state.config.model_profiles[publicProvider].includes(state.model) ? state.model : "other";
  metrics.channel = userMessage.communicationChannel;
  metrics.characterId = characterId;
  metrics.recovery = Boolean(retrySnapshot);
  const historyLength = requestSnapshot.recent_history?.length || 0;
  metrics.historyBand = historyLength > 16 ? "long" : historyLength > 8 ? "medium" : "short";
  state.replyTimingByRequest.set(requestId, metrics);
  while (state.replyTimingByRequest.size > 50) state.replyTimingByRequest.delete(state.replyTimingByRequest.keys().next().value);
  const committedReply = streamV2 ? new CommittedReply(requestId, characterId) : null;
  let provisionalReply = thread.messages.find(message => message.role === "assistant" && message.requestId === requestId && message.committedBlocks?.length) || null;
  for (const event of provisionalReply?.committedBlocks || []) committedReply?.accept(event);
  if (provisionalReply && committedReply?.blocks.length) {
    // A manual recovery keeps its already delivered prefix on screen even if
    // replay accepts only duplicate blocks before the terminal reply arrives.
    state.streamingPresentationByCharacter.set(characterId, { requestId, receivedCount: committedReply.blocks.length,
      visibleCount: Math.min(provisionalReply.contentBlocks.length, committedReply.blocks.length) });
  }
  let commitDisplay = Promise.resolve();
  const commitPresentationController = new AbortController();
  const materializeCommitted = (count = committedReply?.blocks.length) => {
    if (!committedReply?.blocks.length) return;
    const record = normalizeMessage({ id: provisionalReply?.id || `reply:${requestId}`, characterId, role: "assistant", contentBlocks: committedReply.blocks.slice(0, count),
      committedBlocks: committedReply.snapshot.slice(0, count), communicationChannel: userMessage.communicationChannel, createdAt: provisionalReply?.createdAt || Date.now(),
      requestId, status: "pending", conversationSegmentId: requestSegment });
    if (provisionalReply) Object.assign(provisionalReply, record);
    else { provisionalReply = record; thread.messages.push(record); thread.messageCount = Number(thread.messageCount || 0) + 1; }
    metrics.messageId = record.id;
    const streaming = state.streamingPresentationByCharacter.get(characterId);
    if (streaming?.requestId === requestId) streaming.visibleCount = count;
    if (ownsVisibleRequest()) { updateTypingPhase(characterId, requestId, "streaming"); renderAll(); }
  };
  const onCommitted = () => {
    if (!metrics.bodyReceivedAt) metrics.bodyReceivedAt = performance.now();
    const count = committedReply.blocks.length;
    const streaming = state.streamingPresentationByCharacter.get(characterId) || { requestId, visibleCount: 0 };
    streaming.receivedCount = count;
    state.streamingPresentationByCharacter.set(characterId, streaming);
    syncFastForward();
    commitDisplay = commitDisplay.then(async () => {
      if (latencyV2) await waitForNaturalReply(requestId, commitPresentationController.signal);
      if (latencyV2 && metrics.lastBodyPaintAt && !metrics.skip) {
        const budget = segmentWait(committedReply.blocks[count - 2]?.text.length || 0, metrics.segmentSpent, quietPresentation());
        const began = performance.now();
        const remaining = Math.max(0, budget - (began - metrics.lastBodyPaintAt));
        while (!metrics.skip && !quietPresentation() && performance.now() - began < remaining) {
          await abortableDelay(Math.min(32, remaining - (performance.now() - began)), commitPresentationController.signal);
        }
        metrics.segmentSpent += remaining;
        metrics.naturalWaitMs += performance.now() - began;
      }
      if (!ownsRequest()) return;
      materializeCommitted(count);
      metrics.lastBodyPaintAt = performance.now();
      await dbPutThread(thread);
    });
    // Transport reads continue while a short presentation pause or disk write finishes.
    void commitDisplay.catch(() => {});
  };
  const timing = { shownAt: 0 };
  const feedbackController = new AbortController();
  const typingGate = (async () => {
    try {
      if (!quietPresentation()) await abortableDelay(requestDelay(requestId, "initial", latencyV2 ? 500 : 1800, latencyV2 ? 750 : 2600), feedbackController.signal);
    } catch (error) {
      if (error?.name === "AbortError") return;
      throw error;
    }
    if (!ownsRequest() || metrics.presented || typingStateFor(characterId)?.networkDone) return;
    metrics.feedbackAllowed = true;
    timing.shownAt = performance.now();
    updateTypingPhase(characterId, requestId, "typing");
    if (metrics.pendingProgressText) setRequestStatus(characterId, metrics.pendingProgressText, requestId);
  })();
  userMessage.requestSnapshot = structuredClone(storedSnapshot);
  state.retrySnapshots.set(userMessage.id, { requestId, payload: structuredClone(storedSnapshot) });
  const userPersistence = dbPutThread(thread);
  let terminalResult = null;
  let confirmedAssistant = null;
  try {
    let result = null;
    let lastError = null;
    const backoffs = requestSnapshot.provider === SUBSCRIPTION_PROVIDER ? [0] : [0, 1000, 2000, 4000];
    for (let attempt = 0; attempt < backoffs.length; attempt += 1) {
      if (attempt > 0) {
        if (!recoverableChatError(lastError) || requestController.signal.aborted) throw lastError;
        // Transport recovery is actionable immediately, even while the normal
        // typing indicator is still inside its natural onset window.
        updateTypingPhase(characterId, requestId, "recovering");
        setRequestStatus(characterId, `连接中断，正在恢复（${attempt}/3）……`, requestId);
        const retryAfterMs = Number(lastError?.retryAfterSeconds || 0) * 1000;
        await abortableDelay(Math.max(backoffs[attempt], retryAfterMs), requestController.signal);
      }
      try {
        const response = await fetch(requestEndpoint, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", ...(streamV2 ? { "X-Snow-Stream-Version": "2" } : {}), ...(dialogueV2 && (retrySnapshot || attempt > 0) ? { "X-Snow-Recovery-Only": "1" } : {}) }, signal: requestController.signal, body: requestBody });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          const detail = payload?.detail && typeof payload.detail === "object" ? payload.detail : {};
          const serverCode = plain(detail.code);
          const fallbackCode = [502, 503, 504].includes(response.status) ? "provider_network_error" : "chat_failed";
          const error = chatError(serverCode || fallbackCode, detail, response.status);
          if (!error.retryAfterSeconds) {
            const headerRetryAfter = Number(response.headers.get("Retry-After"));
            if (Number.isFinite(headerRetryAfter) && headerRetryAfter > 0) {
              error.retryAfterSeconds = Math.min(300, Math.floor(headerRetryAfter));
            }
          }
          throw error;
        }
        result = await consumeChatStream(response, { characterId, requestId, fallbackChannel: userMessage.communicationChannel, committedReply, onCommitted, dialogueV2 });
        break;
      } catch (error) {
        lastError = error;
        if (attempt === backoffs.length - 1 || !recoverableChatError(error)) throw error;
      }
    }
    if (!result) throw lastError || new Error("stream_disconnected");
    terminalResult = result;
    // A confirmed terminal reply is a completed business action. Cosmetic waiting
    // can no longer cancel it or expose a retry with a fresh paid-request ID.
    window.clearTimeout(requestTimeout);
    const activeRequest = state.chatRequestByCharacter.get(characterId);
    if (activeRequest?.requestId === requestId) activeRequest.completed = true;
    await result.statePersistence;
    commitPresentationController.abort();
    metrics.serverTimings = result.serverTimings;
    if (!metrics.bodyReceivedAt) metrics.bodyReceivedAt = performance.now();
    await commitDisplay.catch(() => {});
    if (result.returnedChannel === "in_person" && ownsVisibleRequest()) {
      void warmCharacterStage(characterById(characterId), result.expressionState, result.performanceId, requestController.signal);
    }
    await userPersistence;
    if (!latencyV2) await typingGate;
    if (!latencyV2 && timing.shownAt && !reducedMotion()) {
      const visibleFor = performance.now() - timing.shownAt;
      if (visibleFor < 1200) await abortableDelay(1200 - visibleFor, requestController.signal);
    }
    userMessage.status = "sent";
    const completedReply = normalizeMessage({ id: provisionalReply?.id || (dialogueV2 ? `reply:${requestId}` : id()), characterId, role: "assistant", contentBlocks: result.contentBlocks, committedBlocks: committedReply?.snapshot || [], communicationChannel: dialogueV2 ? userMessage.communicationChannel : result.returnedChannel, expressionState: result.expressionState, stageMotion: result.stageMotion, performanceId: result.performanceId, createdAt: provisionalReply?.createdAt || Date.now(), requestId, conversationSegmentId: requestSegment, usage: { ...(result.usage || {}), model: result.usage?.model || state.model }, movementStatus: result.movementStatus });
    const assistantMessage = provisionalReply || completedReply;
    completedReply.evidenceSources = normalizeMessage({ evidenceSources: result.evidenceSources }).evidenceSources;
    confirmedAssistant = assistantMessage;
    metrics.messageId = assistantMessage.id;
    const receivedPresentationCount = state.streamingPresentationByCharacter.get(characterId)?.visibleCount || 0;
    if (latencyV2 && !receivedPresentationCount) state.readyReplyIds.add(assistantMessage.id);
    if (provisionalReply) Object.assign(provisionalReply, completedReply);
    else { thread.messages.push(assistantMessage); thread.messageCount = Number(thread.messageCount || 0) + 1; }
    thread.turnCount += 1;
    if (!dialogueV2 || requestSegment === thread.conversationSegmentId) thread.continuityDecision = "";
    thread.lastActiveAt = Date.now();
    if (ownsRequest() || !typingStateFor(characterId)) state.latest.set(characterId, { requestId, errorCode: "", errorStage: "", usage: result.usage });
    state.retrySnapshots.delete(userMessage.id);
    userMessage.requestSnapshot = null;
    if (dialogueV2) {
      result.sceneAccepted = await commitDialogueResult(thread, result, ownsRequest);
      if (!result.sceneAccepted) await dbPutThread(thread);
    }
    else await dbPutThread(thread);
    if (dialogueV2 && result.sceneAccepted && result.returnedChannel !== thread.channel && ownsRequest()) await setChannel(result.returnedChannel, true, thread);
    if (latencyV2) {
      const pending = typingStateFor(characterId);
      if (pending?.requestId === requestId) pending.networkDone = true;
      void deliverCompletedReply(characterId, requestId, assistantMessage, receivedPresentationCount);
    } else {
      updateTypingPhase(characterId, requestId, "presenting");
      await presentAssistantTurn(characterId, requestId, assistantMessage);
    }
    if (ownsRequest() && result.pendingSceneState && (!dialogueV2 || result.sceneAccepted)) {
      state.sceneByCharacter.set(characterId, result.pendingSceneState);
      if (ownsVisibleRequest()) {
        state.scene = result.pendingSceneState;
        renderScene();
        if (result.pendingStateEvent?.event_type === "joint_movement") setRequestStatus(characterId, `已一起前往${result.pendingSceneState.character_location || "新的地点"}`, requestId);
      }
    }
    if (ownsRequest()) {
      if (result.movementStatus?.status === "state_unchanged") {
        setRequestStatus(characterId, "对白已完成，但场景未更新。", requestId, result.recoveryAction === "refresh_scene" ? "refresh_scene" : "");
      } else if (result.pendingStateEvent?.event_type !== "joint_movement") {
        setRequestStatus(characterId, "已完成", requestId);
      }
      if (!latencyV2) clearTypingState(characterId, requestId);
    }
    scheduleAutoSummary(thread, result.usage);
    completeReplyTiming(metrics, "success");
    renderCharacters();
  } catch (caught) {
    commitPresentationController.abort();
    await commitDisplay.catch(() => {});
    await userPersistence.catch(() => {});
    if (terminalResult) {
      // UI or storage faults after a verified terminal reply cannot downgrade
      // the completed business action or create a fresh paid-request retry.
      userMessage.status = "sent";
      userMessage.errorCode = "";
      userMessage.errorStage = "";
      userMessage.requestSnapshot = null;
      state.retrySnapshots.delete(userMessage.id);
      if (!confirmedAssistant) {
        confirmedAssistant = normalizeMessage({ id: provisionalReply?.id || (dialogueV2 ? `reply:${requestId}` : id()), characterId, role: "assistant", contentBlocks: terminalResult.contentBlocks,
          committedBlocks: committedReply?.snapshot || [], communicationChannel: dialogueV2 ? userMessage.communicationChannel : terminalResult.returnedChannel, requestId,
          expressionState: terminalResult.expressionState, stageMotion: terminalResult.stageMotion, performanceId: terminalResult.performanceId, movementStatus: terminalResult.movementStatus,
          conversationSegmentId: requestSegment, createdAt: provisionalReply?.createdAt || Date.now(), usage: terminalResult.usage });
        if (provisionalReply) Object.assign(provisionalReply, confirmedAssistant);
        else { thread.messages.push(confirmedAssistant); thread.messageCount = Number(thread.messageCount || 0) + 1; }
        thread.turnCount += 1;
        if (!dialogueV2 || requestSegment === thread.conversationSegmentId) thread.continuityDecision = "";
        thread.lastActiveAt = Date.now();
      }
      confirmedAssistant.evidenceSources = normalizeMessage({ evidenceSources: terminalResult.evidenceSources }).evidenceSources;
      cancelPresentationQueue(characterId, requestId, { render: false });
      state.readyReplyIds.delete(confirmedAssistant.id);
      state.latest.set(characterId, { requestId, errorCode: "", errorStage: "", usage: terminalResult.usage });
      await dbPutThread(thread).catch(() => {});
      completeReplyTiming(metrics, "success");
      if (ownsRequest()) setRequestStatus(characterId, "回复已完成，已保留收到的内容。", requestId);
      if (state.selected === characterId) showBanner("回复已经收到并保留。请重新打开记录查看。");
      return;
    }
    const error = caught?.name === "AbortError" ? new Error(requestTimedOut ? "provider_timeout" : "request_cancelled") : caught;
    const contextRetired = dialogueV2 && await retireRejectedDialogueContext(error, thread, envelope, ownsRequest);
    if (!dialogueV2 && !latencyV2 && !committedReply?.blocks.length && requestSnapshot.provider !== SUBSCRIPTION_PROVIDER && stateRecoveryAttempt < 1 && statePackageRecoveryError(error) && !requestController.signal.aborted) {
      cancelPresentationQueue(characterId, requestId);
      state.retrySnapshots.delete(userMessage.id);
      await clearPersistedWorldPackage();
      if (characterId === state.selected) renderScene();
      return await runChat(thread, userMessage, { stateRecoveryAttempt: stateRecoveryAttempt + 1 });
    }
    if (!committedReply?.blocks.length && !dialogueV2 && !shouldReuseChatRequest(error)) {
      state.retrySnapshots.delete(userMessage.id);
      userMessage.requestSnapshot = null;
    }
    cancelPresentationQueue(characterId, requestId);
    if (committedReply?.blocks.length) {
      materializeCommitted();
      provisionalReply.partial = true;
      provisionalReply.status = "failed";
      provisionalReply.errorCode = error.message || "stream_disconnected";
    }
    userMessage.status = "failed";
    userMessage.errorCode = error instanceof Error ? error.message : "chat_failed";
    if (requestSnapshot.provider === SUBSCRIPTION_PROVIDER && ["subscription_not_connected", "subscription_disconnected", "credential_invalid"].includes(userMessage.errorCode)) {
      forgetSubscription("disconnected", "subscription_not_connected");
      renderSubscriptionSettings();
    }
    userMessage.errorStage = normalizedErrorStage(error instanceof Error ? error.errorStage : "") || _errorStageForClientCode(userMessage.errorCode);
    if (ownsRequest() || !typingStateFor(characterId)) state.latest.set(characterId, { requestId, errorCode: userMessage.errorCode, errorStage: userMessage.errorStage });
    await dbPutThread(thread);
    if (ownsVisibleRequest()) renderAll();
    if (ownsRequest()) {
      setRequestStatus(characterId, provisionalReply?.partial ? "回复未完成，已保留收到的内容。可用原请求恢复结果。" : displayError(error), requestId, contextRetired ? "refresh_scene" : "");
      clearTypingState(characterId, requestId);
    }
    completeReplyTiming(metrics, provisionalReply?.partial ? "partial" : "failed");
  } finally {
    window.clearTimeout(requestTimeout);
    requestController.abort();
    if (state.chatRequestByCharacter.get(characterId)?.requestId === requestId) state.chatRequestByCharacter.delete(characterId);
    if (state.streamingPresentationByCharacter.get(characterId)?.requestId === requestId) state.streamingPresentationByCharacter.delete(characterId);
    if (state.completedPresentationByCharacter.get(characterId)?.requestId !== requestId) clearTypingState(characterId, requestId);
    if (state.completedPresentationByCharacter.get(characterId)?.requestId !== requestId) feedbackController.abort();
    updateComposerAvailability();
    void flushDeferredPresenceRefresh();
  }
}
async function submitUserBlocks(blocks, { clearComposer = false, onAccepted = null, movementLocationId = "" } = {}) {
  if (!state.selected || !state.writerOwned || globalRequestBusy()) return false;
  cancelBackgroundSummaries();
  const total = renderBlocksText(blocks).length;
  if (!blocks.length) return false;
  if (total > 2000) {
    showBanner("单轮动作与对白合计不能超过 2,000 字。");
    return false;
  }
  if (sceneActionsV2Enabled() && !movementLocationId && blocks.length === 1 && blocks[0].type === "message"
    && isPureRendezvousJoin(renderBlocksText(blocks)) && signedPendingRendezvous().value) {
    const original = { message: $("message-input").value, action: $("action-input").value };
    const success = await arriveSceneAction(renderBlocksText(blocks));
    if (success && clearComposer) {
      if ($("message-input").value === original.message) $("message-input").value = "";
      if ($("action-input").value === original.action) $("action-input").value = "";
      await saveDraftNow(); updateInputCount();
    }
    return success;
  }
  if (!configured()) {
    openSettings("models");
    showError("setup-error", "请先配置当前标签页使用的模型会话。");
    return false;
  }
  const thread = await dbGetThread(state.selected);
  if (!(await ensureContinuityDecision(thread))) return false;
  const submittedDraft = clearComposer ? {
    message: $("message-input").value,
    action: $("action-input").value,
    stickerId: plain(state.selectedSticker?.asset_id),
  } : null;
  const userMessage = normalizeMessage({ id: id(), characterId: thread.characterId, role: "user", contentBlocks: blocks, communicationChannel: thread.channel, createdAt: Date.now(), status: "pending", conversationSegmentId: thread.conversationSegmentId, movementLocationId });
  thread.messages.push(userMessage);
  thread.messageCount = Number(thread.messageCount || 0) + 1;
  finishOnboarding();
  onAccepted?.();
  await runChat(thread, userMessage);
  if (clearComposer && userMessage.status === "sent") {
    if ($("message-input").value === submittedDraft.message) $("message-input").value = "";
    if ($("action-input").value === submittedDraft.action) $("action-input").value = "";
    if (plain(state.selectedSticker?.asset_id) === submittedDraft.stickerId) clearSelectedSticker();
    if (!$("action-input").value) state.actionComposerOpen = false;
    state.drafts.set(draftKey(thread.characterId, thread.channel, "message"), $("message-input").value);
    state.drafts.set(draftKey(thread.characterId, thread.channel, "action"), $("action-input").value);
    void persistDrafts();
    $("toggle-sticker").setAttribute("aria-expanded", "false");
    updateInputCount();
    updateComposerAvailability();
  }
  return userMessage.status === "sent";
}
async function sendMessage(event) {
  event.preventDefault();
  await submitUserBlocks(inputBlocks(), { clearComposer: true });
}
async function sendMovementInvitation(event) {
  event.preventDefault();
  const invitation = $("movement-invitation").value.trim();
  const locationId = $("movement-dialog").dataset.locationId || "";
  if (!invitation || !locationId) return;
  const channel = currentThread()?.channel === "in_person" ? "in_person" : "text";
  await submitUserBlocks(
    [{ type: channel === "in_person" ? "speech" : "message", text: invitation }],
    { onAccepted: () => $("movement-dialog").close(), movementLocationId: locationId },
  );
}
async function retryMessage(messageId) {
  if (globalRequestBusy()) return;
  if (!configured()) return openSettings("models");
  const thread = currentThread();
  const message = thread?.messages.find((item) => item.id === messageId && item.role === "user");
  if (!message || message.status !== "failed") return;
  if (message.errorCode === "request_result_unavailable") return showBanner(displayError(new Error(message.errorCode)));
  const snapshot = state.retrySnapshots.get(message.id)
    || (message.requestId && message.requestSnapshot ? { requestId: message.requestId, payload: message.requestSnapshot } : null);
  const v2 = snapshot?.payload?.endpoint === "/public/v2/chat/stream";
  const chat = v2 ? snapshot.payload.envelope?.chat : snapshot?.payload;
  if (v2 && message.errorCode === "request_result_unavailable") return showBanner(displayError(new Error(message.errorCode)));
  if (snapshot && (
    plain(chat?.provider) !== state.provider
    || plain(chat?.model) !== state.model
    || (state.provider === SUBSCRIPTION_PROVIDER && snapshotSubscriptionEffort(chat) !== state.reasoningEffort)
  )) {
    if (v2) return showBanner("请切回原请求的模型会话后恢复。未确认的请求不会自动重新生成。");
    state.retrySnapshots.delete(message.id);
    message.requestId = "";
    message.requestSnapshot = null;
    await dbPutThread(thread);
  }
  if (v2 && !compatibleRetrySnapshot(message)) return showBanner("原请求快照不完整，不能安全恢复。不会重新生成，请发送新消息。");
  await runChat(thread, message);
}
function successfulChatMessages(thread) {
  return (thread.messages || []).filter((message) => message.conversationSegmentId === thread.conversationSegmentId && message.role === "assistant" && message.status === "sent" && !["presence_arrival", "scene_action"].includes(message.source));
}
function remainingProviderCallBudget(usage) {
  const used = Number(usage?.provider_calls);
  if (!Number.isInteger(used) || used < 0) return 0;
  const configuredLimit = Number(state.config?.max_provider_calls_per_action);
  const limit = Number.isInteger(configuredLimit) && configuredLimit > 0
    ? Math.min(2, configuredLimit)
    : 2;
  return Math.max(0, limit - used);
}
function scheduleAutoSummary(thread, chatUsage = null) {
  if (state.provider === SUBSCRIPTION_PROVIDER || !state.autoSummaryEnabled || !configured()) return;
  // A summary is another provider operation belonging to the current user
  // action. Never start it when validation/rewriting already consumed both
  // calls, or when an older server omitted the auditable call count.
  if (remainingProviderCallBudget(chatUsage) < 1) return;
  const successful = successfulChatMessages(thread);
  if (successful.length < 12 || successful.length < thread.summarizedThroughTurnCount + 12) return;
  const checkpoint = successful[successful.length - 1];
  if (!checkpoint) return;
  if (!thread.summaryRequestId) {
    thread.summaryRequestId = id();
    thread.summaryCheckpointMessageId = checkpoint.id;
  }
  if (state.summaryInFlight.has(thread.characterId)) return;
  void dbPutThread(thread).then(() => runAutoSummary(thread));
}
function cancelBackgroundSummaries() {
  for (const controller of state.summaryControllers.values()) controller.abort();
  for (const thread of state.threads.values()) {
    // A provider may have accepted an interrupted request. A later summary
    // gets a new ID and checkpoint instead of replaying a possibly charged call.
    if (thread.summaryRequestId) {
      thread.summaryRequestId = "";
      void dbPutThread(thread);
    }
  }
  state.summaryControllers.clear();
  state.summaryInFlight.clear();
}
async function runAutoSummary(thread) {
  if (state.provider === SUBSCRIPTION_PROVIDER || !thread.summaryRequestId || !state.autoSummaryEnabled || !configured() || state.summaryInFlight.has(thread.characterId) || globalRequestBusy()) return;
  state.summaryInFlight.add(thread.characterId);
  const controller = new AbortController();
  state.summaryControllers.set(thread.characterId, controller);
  updateComposerAvailability();
  const requestId = thread.summaryRequestId;
  const epoch = state.historyEpoch;
  try {
    const turns = thread.messages.filter((message) => message.conversationSegmentId === thread.conversationSegmentId && message.status === "sent").slice(-24).map((message) => ({
      role: message.role,
      communication_channel: message.communicationChannel,
      content_blocks: wireBlocks(message.contentBlocks),
    }));
    const requestPayload = fitPublicRequestPayload(
      { request_id: requestId, provider: state.provider, credential: state.credential, model: state.model, ...subscriptionReasoningPayload(), character_id: thread.characterId, turns, previous_summary: thread.summary || "" },
      { arrays: [{ key: "turns", minimum: 2 }], texts: ["previous_summary"] },
    );
    const payload = await api("/chat/summarize", { method: "POST", signal: controller.signal, timeoutMs: 20000, body: JSON.stringify(requestPayload) });
    if (controller.signal.aborted || epoch !== state.historyEpoch || thread.summaryRequestId !== requestId) return;
    thread.summary = payload.summary || thread.summary;
    thread.pendingTopics = Array.isArray(payload.pending_topics)
      ? payload.pending_topics.map(plain).filter(Boolean).slice(0, 12)
      : thread.pendingTopics;
    thread.summarizedThroughMessageId = thread.summaryCheckpointMessageId;
    thread.summarizedThroughTurnCount = successfulChatMessages(thread).length;
    thread.summaryRequestId = "";
    thread.summaryUpdatedAt = Date.now();
    await dbPutThread(thread);
    if (thread.characterId === state.selected) {
      $("summary-last-updated").textContent = `最近更新：${new Date(thread.summaryUpdatedAt).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}`;
    }
  } catch (error) {
    if (controller.signal.aborted || epoch !== state.historyEpoch) return;
    // Only a future successful user turn may schedule another summary. Its
    // turns/checkpoint differ, so it must never reuse this paid-request UUID.
    if (thread.characterId === state.selected) setRequestStatus(thread.characterId, "对话已完成；连续性整理稍后重试。");
    thread.summaryRequestId = "";
    await dbPutThread(thread);
    if (error?.message === "credential_invalid") clearCredential();
  } finally {
    if (state.summaryControllers.get(thread.characterId) === controller) {
      state.summaryControllers.delete(thread.characterId);
      state.summaryInFlight.delete(thread.characterId);
    }
    updateComposerAvailability();
    void flushDeferredPresenceRefresh();
  }
}

async function performSceneAction(action, characterId = state.selected, signal = undefined, { rendezvousId = "", userText = "", scopeRecoveryAttempt = 0 } = {}) {
  const thread = await dbGetThread(characterId);
  const epoch = state.historyEpoch;
  const segment = thread.conversationSegmentId;
  const record = await loadDialogueRecord(characterId);
  const previous = record.sceneActionSnapshot;
  const envelope = previous?.envelope || {
    protocol: SCENE_PROTOCOL, request_id: id(), character_id: characterId, action,
    state_package: state.worldPackage || "", continuity_decision: thread.continuityDecision || "",
    local_day_key: thread.localDayKey || localDayKey(), rendezvous_id: rendezvousId,
    ...fitDialogueContext({ segment_id: segment, session_context_package: dialoguePackage(thread),
      memory_candidates: dialogueCandidates(characterId) }),
  };
  const snapshot = previous || { endpoint: "/public/v2/scene/action", envelope: structuredClone(envelope) };
  const owner = envelope.request_id;
  state.sceneActionByCharacter.set(characterId, owner);
  const owns = () => state.writerOwned && state.historyEpoch === epoch && thread.historyEpoch === epoch
    && thread.conversationSegmentId === segment && state.sceneActionByCharacter.get(characterId) === owner;
  const saved = { ...record, sceneActionSnapshot: snapshot };
  state.dialogueByCharacter.set(characterId, saved);
  await storePut("app_state", saved);
  try {
    let result;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (attempt) await abortableDelay(attempt * 600, signal);
      try {
        result = await dialogueHttp.request("/scene/action", { method: "POST", signal,
          headers: previous || attempt ? { "X-Snow-Recovery-Only": "1" } : {}, body: JSON.stringify(envelope) });
        break;
      } catch (error) {
        if (attempt === 2 || error?.name === "AbortError" || !recoverableChatError(error)) throw error;
      }
    }
    if (!owns()) return result;
    if (envelope.segment_id !== segment) {
      const reconciled = { ...state.dialogueByCharacter.get(characterId), sceneActionSnapshot: null };
      state.dialogueByCharacter.set(characterId, reconciled);
      await storePut("app_state", reconciled);
      return performSceneAction(action, characterId, signal, { rendezvousId, userText });
    }
    thread.channel = result.communication_channel === "in_person" && result.scene_state?.co_located ? "in_person" : "text";
    thread.continuityDecision = "";
    const cleared = { ...state.dialogueByCharacter.get(characterId), sceneActionSnapshot: null };
    state.dialogueByCharacter.set(characterId, cleared);
    if (result.scene_action?.status === "joined") {
      const requestId = envelope.request_id;
      if (userText && !thread.messages.some(message => message.id === `user:${requestId}`)) {
        thread.messages.push(normalizeMessage({ id: `user:${requestId}`, characterId, role: "user", content: userText,
          requestId, communicationChannel: "text", status: "sent", conversationSegmentId: segment }));
      }
      if (!thread.messages.some(message => message.id === `reply:${requestId}`)) {
        thread.messages.push(normalizeMessage({ id: `reply:${requestId}`, characterId, role: "assistant", requestId,
          communicationChannel: "in_person", contentBlocks: [{ type: "action", text: `你已来到${result.scene_state?.character_location || "她身边"}。` }],
          source: "scene_action", status: "sent", conversationSegmentId: segment, usage: { provider_calls: 0 } }));
      }
      thread.messageCount = Math.max(thread.messageCount, thread.messages.length);
    }
    if (await commitDialogueResult(thread, result, owns)) {
      state.sceneByCharacter.set(characterId, result.scene_state || {});
      if (characterId === state.selected) { state.scene = result.scene_state; renderScene(); }
    } else throw new Error("session_context_conflict");
    if (previous && envelope.action !== action && action !== "resolve") {
      return performSceneAction(action, characterId, signal, { rendezvousId, userText });
    }
    return result;
  } catch (error) {
    if (owns() && (["request_result_unavailable", "invalid_scene_action", "session_context_invalid", "session_context_conflict", "session_context_subject_mismatch", "state_subject_mismatch", "state_invalid"].includes(error.message)
      || error.message === "request_id_conflict" && error.httpStatus === 409)) {
      const cleaned = { ...state.dialogueByCharacter.get(characterId), sceneActionSnapshot: null };
      state.dialogueByCharacter.set(characterId, cleaned);
      await storePut("app_state", cleaned);
    }
    const retired = await retireRejectedDialogueContext(error, thread, envelope, owns);
    if (retired && action === "resolve" && !previous && scopeRecoveryAttempt < 2 && !signal?.aborted) {
      // Scope rejection is confirmed before generation. Re-establish only the
      // model-free scene read; the immutable chat request is never resubmitted.
      return performSceneAction("resolve", characterId, signal, { scopeRecoveryAttempt: scopeRecoveryAttempt + 1 });
    }
    if (retired && characterId === state.selected) setRequestStatus(characterId, displayError(error), "", "refresh_scene");
    throw error;
  } finally {
    if (state.sceneActionByCharacter.get(characterId) === owner) state.sceneActionByCharacter.delete(characterId);
  }
}
async function arriveSceneAction(userText = "") {
  // P6a：用户亲身进入室内，世界背景从此为当前室内地点，不再跳回外景。
  state.snowBeenInPerson = true;
  const characterId = state.selected;
  const thread = currentThread();
  const previousChannel = thread?.channel || "text";
  state.arrivalPending = true;
  setRequestStatus(characterId, "正在确认会合地点……");
  updateComposerAvailability();
  try {
    await saveDraftNow();
    const hadRendezvous = Boolean(signedPendingRendezvous(characterId).value);
    await ensureContinuityDecision(thread);
    if (thread.continuityDecision) {
      await performSceneAction("resolve", characterId);
      if (hadRendezvous && !signedPendingRendezvous(characterId).value) {
        showBanner("从今天开始后，昨天尚未赴约的约定已结束。你可以重新发起邀请。");
        return false;
      }
    }
    const pending = signedPendingRendezvous(characterId).value;
    const result = await performSceneAction(pending ? "join_rendezvous" : "join_character", characterId, undefined,
      { rendezvousId: plain(pending?.rendezvous_id), userText });
    if (state.selected !== characterId || thread.historyEpoch !== state.historyEpoch) return false;
    await setChannel(result.communication_channel === "in_person" ? "in_person" : "text", true, thread);
    renderScene();
    // 雪境 ADV：舞台旁白「你已来到X。」已是到场通知（aria-live 播报一次），
    // 开关开启时不再重复显示状态行与横幅，只清掉进行中的状态；关闭时保留旧的三处提示。
    if (!suppressArrivalDuplicateNotice()) {
      setRequestStatus(characterId, `已来到${result.scene_state?.character_location || "她身边"}，可以继续聊天。`);
      showBanner(`已来到${result.scene_state?.character_location || "她身边"}。`);
    } else {
      setRequestStatus(characterId, "");
    }
    return true;
  } catch (error) {
    if (state.selected === characterId && thread.historyEpoch === state.historyEpoch) {
      await setChannel(previousChannel, false, thread);
      showBanner(displayError(error));
    }
    return false;
  } finally {
    state.arrivalPending = false;
    updateComposerAvailability();
  }
}
async function transitionPresence(targetChannel, characterId = state.selected, targetThread = null, signal = undefined) {
  cancelBackgroundSummaries();
  const thread = targetThread || state.threads.get(characterId) || currentThread();
  if (sceneActionsV2Enabled()) {
    const result = await performSceneAction(targetChannel === "in_person" ? "join_character" : "open_communicator", characterId, signal);
    await setChannel(result.communication_channel || targetChannel, true, thread);
    return result;
  }
  const result = await api("/presence/transition", { method: "POST", signal, body: JSON.stringify({ request_id: id(), character_id: characterId, target_channel: targetChannel, action: targetChannel === "in_person" ? "join_character" : "open_communicator", state_package: state.worldPackage || "" }) });
  await saveWorldPackage(result.state_package);
  state.sceneByCharacter.set(characterId, result.scene_state || {});
  if (state.selected === characterId) state.scene = result.scene_state;
  await setChannel(targetChannel, true, thread);
  if (state.selected === characterId) renderScene();
  return result;
}
function arrivalReactionUnavailableMessage(code = "") {
  if (code === "credential_invalid") {
    return "位置切换已完成；模型会话已失效，重新配置后可以继续对话。";
  }
  if (["generation_queue_full", "generation_queue_timeout", "rate_limit_exceeded", "provider_rate_limited"].includes(code)) {
    return "位置切换已完成；到场反应暂未生成，你可以直接开始对话。";
  }
  return "位置切换已完成；她暂时没有作出到场回应，你可以直接开始对话。";
}
async function arriveInPerson() {
  cancelBackgroundSummaries();
  if (!state.selected || globalRequestBusy()) return;
  state.snowBeenInPerson = true;
  if (sceneActionsV2Enabled()) return arriveSceneAction();
  state.arrivalPending = true;
  const started = performance.now();
  const characterId = state.selected;
  const arrivalId = id();
  const thread = currentThread();
  const previousChannel = thread?.channel || "text";
  let arrivalMessage = null;
  let transitionApplied = false;
  const ownsArrival = () => ownsTypingState(characterId, arrivalId);
  const ownsVisibleArrival = () => ownsArrival() && state.selected === characterId;
  setTypingState({ characterId, requestId: arrivalId, channel: "in_person", phase: "arrival" });
  if (!featureEnabled("motion_v2")) await setChannel("in_person", false, thread);
  if (ownsVisibleArrival()) {
    $("presence-arrival-loading").hidden = featureEnabled("motion_v2");
    $("stage-presence-status").hidden = false;
    $("stage-presence-status").textContent = "正在靠近……";
  }
  updateComposerAvailability();
  try {
    await saveDraftNow();
    await transitionPresence("in_person", characterId, thread);
    transitionApplied = true;
    if (featureEnabled("motion_v2") && ownsVisibleArrival()) {
      $("presence-arrival-loading").hidden = true;
      $("stage-presence-status").textContent = "已来到她身边，正在等待回应…";
    }
    await dismissCurrentRendezvous(thread);
    if (!configured()) {
      if (ownsVisibleArrival()) showBanner("场景已更新。配置模型后可以开始对话。");
      return;
    }
    const arrivalPayload = fitPublicRequestPayload(
      { arrival_id: arrivalId, provider: state.provider, credential: state.credential, model: state.model, ...subscriptionReasoningPayload(), character_id: characterId, recent_history: requestHistory(thread?.messages || [], "", thread?.conversationSegmentId || ""), history_summary: thread?.continuityDecision === "start_today" ? "" : (thread?.summary || ""), state_package: state.worldPackage || "" },
      { arrays: [{ key: "recent_history", minimum: 0 }], texts: ["history_summary"] },
    );
    const result = await api("/presence/arrival", { method: "POST", body: JSON.stringify(arrivalPayload) });
    if (result.reaction && ownsVisibleArrival()) {
      void warmCharacterStage(characterById(characterId), result.reaction.expression_state, result.reaction.performance_id);
    }
    if (ownsArrival()) await saveWorldPackage(result.state_package);
    if (ownsArrival()) state.sceneByCharacter.set(characterId, result.scene_state || {});
    if (ownsVisibleArrival()) state.scene = result.scene_state;
    if (thread) thread.channel = "in_person";
    if (thread && result.reaction) {
      arrivalMessage = normalizeMessage({ id: result.reaction.message_id || id(), characterId, role: "assistant", contentBlocks: result.reaction.content_blocks, communicationChannel: "in_person", expressionState: result.reaction.expression_state, stageMotion: result.reaction.stage_motion, performanceId: result.reaction.performance_id, createdAt: Date.now(), requestId: result.arrival_id, source: "presence_arrival", conversationSegmentId: thread.conversationSegmentId });
      thread.messages.push(arrivalMessage);
      thread.messageCount = Number(thread.messageCount || 0) + 1;
    }
    if (thread) await dbPutThread(thread);
    // Populate the arrival turn before exposing the stage. Otherwise
    // setChannel() can make the scene visible for one paint while the
    // reaction is still being persisted, producing an empty dialogue box.
    if (ownsVisibleArrival()) {
      updateTypingPhase(characterId, arrivalId, "presenting");
      await setChannel("in_person");
      // 雪境 ADV：到场反应已显示在舞台对白框（旁白 + 对白，aria-live 播报一次），
      // 开关开启且确有舞台反应时抑制重复的「她注意到了你的到来。」横幅；错误 / 未注意
      // （无舞台反应）仍保留横幅，保证至少有一处可见通知。
      const stageHasArrivalReaction = Boolean(arrivalMessage) && suppressArrivalDuplicateNotice();
      if (result.terminal_error) showBanner(arrivalReactionUnavailableMessage(result.terminal_error));
      else if (result.decision === "unnoticed") showBanner("你来到她身边，她暂时没有注意到。位置切换已经完成。");
      else if (!stageHasArrivalReaction) showBanner("她注意到了你的到来。");
      renderAll();
    }
    if (arrivalMessage && ownsArrival()) {
      const presentation = presentAssistantTurn(characterId, arrivalId, arrivalMessage);
      if (featureEnabled("chat_latency_v2")) void presentation.catch(() => {});
      else await presentation;
    }
  } catch (error) {
    cancelPresentationQueue(characterId, arrivalId);
    const code = error instanceof Error ? error.message : plain(error);
    if (code === "credential_invalid") clearCredential();
    if (transitionApplied) {
      if (thread) {
        thread.channel = "in_person";
        await dbPutThread(thread);
      }
      if (ownsVisibleArrival()) {
        await setChannel("in_person", false, thread);
        showBanner(arrivalReactionUnavailableMessage(code));
      }
    } else {
      if (thread && thread.characterId === state.selected) {
        await setChannel(previousChannel, false, thread);
      } else if (thread) {
        thread.channel = previousChannel;
      }
      if (thread) await dbPutThread(thread);
      if (ownsVisibleArrival()) showBanner(displayError(error));
    }
  } finally {
    const minimum = featureEnabled("motion_v2") || reducedMotion() ? 0 : 900;
    const elapsed = performance.now() - started;
    if (elapsed < minimum) await delay(minimum - elapsed);
    state.arrivalPending = false;
    $("presence-arrival-loading").hidden = true;
    $("stage-presence-status").hidden = true;
    clearTypingState(characterId, arrivalId);
    updateComposerAvailability();
    if (characterId === state.selected) renderStage();
    void flushDeferredPresenceRefresh();
  }
}
async function openPresenceDialog() {
  if (!state.selected || globalRequestBusy()) return;
  try { await resolvePresence(); } catch (error) { return showBanner(displayError(error)); }
  $("presence-dialog-location").textContent = state.scene?.character_location || "当前位置尚未建立";
  $("presence-dialog-activity").textContent = state.scene?.character_activity || "场景建立后即可前往。";
  $("presence-dialog").showModal();
}

function openDrawer(name) {
  state.drawerReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  for (const panel of [$("info-panel"), $("transcript-panel")]) {
    const open = panel.id === name;
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
    panel.inert = !open;
  }
  $("chat-app").querySelector(".chat-panel").inert = true;
  $("contact-panel").inert = true;
  $("drawer-scrim").hidden = false;
  renderInfo();
  renderTranscript();
  window.setTimeout(() => $(name)?.querySelector("button, [href], input, select, textarea")?.focus(), 0);
}
function closeDrawers() {
  for (const panel of [$("info-panel"), $("transcript-panel")]) {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    panel.inert = true;
  }
  $("chat-app").querySelector(".chat-panel").inert = false;
  $("contact-panel").inert = window.matchMedia("(max-width: 820px)").matches
    ? !$("contact-panel").classList.contains("open")
    : $("chat-app").classList.contains("sidebar-collapsed");
  $("drawer-scrim").hidden = true;
  state.drawerReturnFocus?.focus?.();
  state.drawerReturnFocus = null;
}
function trapDrawerFocus(event) {
  const panel = [$("info-panel"), $("transcript-panel")].find((item) => item.classList.contains("open"));
  if (!panel) return false;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDrawers();
    return true;
  }
  return focusWithin(event, panel);
}
function contactsExpanded() {
  const panel = $("contact-panel");
  const app = $("chat-app");
  if (!panel || !app) return false;
  return window.matchMedia("(max-width: 820px)").matches
    ? panel.classList.contains("open")
    : !app.classList.contains("sidebar-collapsed");
}
function syncContactToggleState(expanded = contactsExpanded()) {
  const value = String(Boolean(expanded));
  $("open-contacts")?.setAttribute("aria-expanded", value);
  $("open-stage-contacts")?.setAttribute("aria-expanded", value);
  $("open-contacts")?.setAttribute("aria-label", expanded ? "角色列表已打开" : "打开角色列表");
  $("open-stage-contacts")?.setAttribute("aria-label", expanded ? "收起角色通讯栏" : "展开角色通讯栏");
  return Boolean(expanded);
}
function syncResponsiveLayout() {
  const panel = $("contact-panel");
  const expanded = contactsExpanded();
  const drawerOpen = [$("info-panel"), $("transcript-panel")].some((item) => item.classList.contains("open"));
  panel.inert = !expanded || drawerOpen;
  panel.setAttribute("aria-hidden", String(!expanded));
  syncContactToggleState(expanded);
  if (!expanded && panel.contains(document.activeElement)) {
    (currentThread()?.channel === "in_person" ? $("open-stage-contacts") : $("open-contacts"))?.focus();
  }
}
function toggleContacts({ mobileOpen = false } = {}) {
  const panel = $("contact-panel");
  const mobile = window.matchMedia("(max-width: 820px)").matches;
  if (mobile) {
    const open = mobileOpen || !panel.classList.contains("open");
    if (open) state.contactReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
    panel.inert = !open;
    syncContactToggleState(open);
    if (open) window.setTimeout(() => $("close-contacts")?.focus(), 0);
    else {
      state.contactReturnFocus?.focus?.();
      state.contactReturnFocus = null;
    }
    return open;
  }
  const collapsed = $("chat-app").classList.toggle("sidebar-collapsed");
  const expanded = !collapsed;
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  panel.setAttribute("aria-hidden", String(collapsed));
  panel.inert = collapsed;
  syncContactToggleState(expanded);
  trigger?.focus?.();
  return expanded;
}
function openSettings(tab = "models") {
  if (tab !== "models") { modelSetupController?.abort(); stopSubscriptionPolling(); }
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    const selected = button.dataset.settingsTab === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== tab; });
  if (tab === "history") storageBytes().then((bytes) => { $("storage-usage").textContent = `当前浏览器记录约占 ${formatBytes(bytes)}。`; });
  const activeThread = currentThread();
  $("auto-summary-enabled").checked = state.autoSummaryEnabled;
  $("subscription-summary-hint").hidden = state.provider !== SUBSCRIPTION_PROVIDER;
  $("history-retention").value = String(state.historyRetentionDays);
  $("day-continuity-preference").value = state.dayContinuityPreference;
  $("summary-last-updated").textContent = activeThread?.summaryUpdatedAt ? `最近更新：${new Date(activeThread.summaryUpdatedAt).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}` : "尚未生成摘要";
  $("provider-select").value = state.modelSetupMode === "subscription" && subscriptionEnabled()
    ? SUBSCRIPTION_PROVIDER : state.apiProviderChoice || state.provider || $("provider-select").value;
  syncProviderControls($("provider-select").value);
  $("model-id").value = state.model || $("model-id").value;
  refreshCredentialStatus();
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
  renderSubscriptionSettings();
  if (tab === "models") startSubscriptionPolling();
}
function openFeedback(messageId = "") {
  state.feedbackMessageId = messageId;
  $("feedback-include-context").checked = true;
  showError("feedback-error", "");
  prepareFeedbackVerification();
  renderFeedbackContextPreview();
  $("feedback-dialog").showModal();
}
function feedbackContextSelection() {
  const thread = currentThread() || { messages: [] };
  const currentMessages = thread.messages.filter(message => message.communicationChannel === (thread.channel || "text")
    && (!thread.conversationSegmentId || message.conversationSegmentId === thread.conversationSegmentId));
  const selectedTurn = thread.messages.find(message => message.id === state.feedbackMessageId);
  const lastUser = [...currentMessages].reverse().find(message => message.role === "user");
  const target = selectedTurn || (lastUser?.status === "failed" ? lastUser : [...currentMessages].reverse().find(message => message.role === "assistant") || lastUser) || null;
  const targetIndex = target ? thread.messages.indexOf(target) : thread.messages.length;
  const userMessage = target?.role === "user" ? target : target?.source === "presence_arrival" ? {} : [...thread.messages.slice(0, targetIndex)].reverse()
    .find(message => message.role === "user" && message.communicationChannel === target?.communicationChannel
      && message.conversationSegmentId === target?.conversationSegmentId && (!target?.requestId || message.requestId === target.requestId)) || {};
  const chatRequestId = target?.requestId || userMessage.requestId || null;
  const latest = { requestId: chatRequestId, errorCode: target?.errorCode || userMessage.errorCode || "", errorStage: target?.errorStage || userMessage.errorStage || "" };
  const userBlocks = wireBlocks(normalizeBlocks(userMessage.contentBlocks, userMessage.communicationChannel || thread.channel || "text", userMessage.content || ""));
  const assistantBlocks = target?.role === "assistant"
    ? wireBlocks(normalizeBlocks(target.contentBlocks, target.communicationChannel || thread.channel || "text", target.content || "")) : [];
  return { thread, target, userMessage, latest, userBlocks, assistantBlocks, chatRequestId };
}
function renderFeedbackContextPreview() {
  const include = $("feedback-include-context").checked;
  const { target, userMessage, userBlocks, assistantBlocks } = feedbackContextSelection();
  $("feedback-context-preview").hidden = !include;
  if (!include) {
    $("feedback-context").textContent = "不会附带对话、角色、模型或请求诊断，只提交你填写的反馈正文与可选 QQ。";
    return;
  }
  $("feedback-context").textContent = target ? `将附带 ${target.communicationChannel === "text" ? "文字通讯" : "面对面"}中${target.source === "presence_arrival" ? "这次到场回应" : target.role === "user" && target.status === "failed" ? "这次失败请求的消息" : "相关一问一答"}及请求编号。` : "将附带当前一问一答和请求编号；服务端会按编号关联内部诊断。";
  const preview = [
    { role: "你", text: userMessage.content || userBlocks.map((block) => block.text || `〔表情〕${block.caption || "表情"}`).join("\n") },
    { role: "角色", text: assistantBlocks.map((block) => block.text || `〔表情〕${block.caption || "表情"}`).join("\n") },
  ].filter(item => item.text);
  $("feedback-context-preview-text").textContent = preview.length ? preview.map(item => `${item.role}：${item.text}`).join("\n\n") : "当前没有可附带的对话内容。";
}
async function submitFeedback(event) {
  event.preventDefault();
  const submitButton = $("feedback-form").querySelector("button[type=submit]");
  if (submitButton?.disabled) return;
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.dataset.originalText = submitButton.textContent;
    submitButton.textContent = "正在提交…";
  }
  showError("feedback-error", "");
  $("feedback-retry-verification").hidden = true;
  const { thread, target, userMessage, latest, chatRequestId, userBlocks, assistantBlocks } = feedbackContextSelection();
  const includeContext = $("feedback-include-context").checked;
  const assistantSpeech = renderBlocksText(assistantBlocks.filter((block) => block.type !== "action"));
  try {
    const contextual = includeContext ? { chat_request_id: chatRequestId || null, character_id: state.selected || "", provider: state.provider || "", model: state.model || "", user_message: userMessage.content || "", assistant_answer: assistantSpeech, user_content_blocks: userBlocks, assistant_content_blocks: assistantBlocks, error_code: latest.errorCode || userMessage.errorCode || "", error_stage: latest.errorStage || userMessage.errorStage || "" } : {};
    const payload = await api("/feedback", { method: "POST", timeoutMs: 20000, body: JSON.stringify({ request_id: id(), body: $("feedback-body").value, qq: $("feedback-qq").value, turnstile_token: await tokenForFeedback(), include_conversation_context: includeContext, request_stage: target?.communicationChannel || thread.channel || "immersive-web", ui_surface: "immersive-web", ...contextual }) });
    $("feedback-dialog").close();
    $("feedback-form").reset();
    toast(`反馈已提交，编号：${payload.feedback_code}`);
  } catch (error) {
    if (error?.name !== "AbortError") {
      showError("feedback-error", error);
      $("feedback-retry-verification").hidden = false;
      if (!$("feedback-verification-status").textContent.includes("失败") && !$("feedback-verification-status").textContent.includes("过期")) {
        $("feedback-verification-status").textContent = "反馈尚未提交，请重新验证后再试。";
      }
    }
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = submitButton.dataset.originalText || "提交反馈";
    }
  }
}

$("character-search").oninput = renderCharacters;
$("composer").onsubmit = sendMessage;
$("message-input").oninput = () => { updateInputCount(); scheduleDraftSave(); };
$("action-input").oninput = () => { updateInputCount(); scheduleDraftSave(); };
$("message-input").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); } };
$("toggle-action").onclick = () => {
  if (globalRequestBusy() || currentThread()?.channel !== "in_person") return;
  state.actionComposerOpen = !state.actionComposerOpen;
  if (!state.actionComposerOpen) scheduleDraftSave();
  updateComposerAvailability();
  if (state.actionComposerOpen) $("action-input").focus();
  updateInputCount();
};
$("toggle-sticker").onclick = openStickerPicker;
$("clear-sticker").onclick = clearSelectedSticker;
$("open-movement-shortcuts").onclick = openMovementShortcuts;
$("movement-form").onsubmit = sendMovementInvitation;
$("movement-invitation").oninput = () => {
  $("send-movement-invitation").disabled = globalRequestBusy() || !$("movement-invitation").value.trim() || !$("movement-dialog").dataset.locationId;
};
$("stop-waiting").onclick = () => {
  const pending = typingStateFor();
  if (pending?.networkDone || state.chatRequestByCharacter.get(pending?.characterId)?.completed) {
    finishReceivedPresentation();
    return;
  }
  if (pending && presentationFor(pending.characterId)?.requestId === pending.requestId) {
    cancelPresentationQueue(pending.characterId, pending.requestId);
    updateTypingPhase(pending.characterId, pending.requestId, "presenting");
    setRequestStatus(pending.characterId, "已停止分段等待，完整回复已经显示。", pending.requestId);
    return;
  }
  const request = pending ? state.chatRequestByCharacter.get(pending.characterId) : null;
  if (!request || request.requestId !== pending.requestId) return;
  $("stop-waiting").disabled = true;
  setRequestStatus(pending.characterId, "正在停止本地等待……", pending.requestId);
  request.controller.abort();
};
$("fast-forward-reply").onclick = finishReceivedPresentation;
$("motion-preference").onchange = () => {
  localPreferences.setItem("project-snow-public:motion", $("motion-preference").value);
  syncMotionState();
  surfaceMotion.cancel();
  transientMotion.cancel();
  cancelStageMotion();
  cancelStageArtTransition();
  if (reducedMotion()) finishReceivedPresentation();
  refreshAnimatedStickers();
};
$("new-replies").onclick = () => {
  loadNewerMessages("timeline", true);
  $("timeline").scrollTop = $("timeline").scrollHeight;
  $("new-replies").hidden = true;
};
$("timeline").addEventListener("scroll", () => {
  rememberTimelineViewport();
  if (timelineNearBottom() && !state.timelineEndId) $("new-replies").hidden = true;
  sampleVisibleReplyPaints();
}, { passive: true });
$("feedback-include-context").onchange = renderFeedbackContextPreview;
$("feedback-retry-verification").onclick = () => $("feedback-form").requestSubmit();
$("next-onboarding").onclick = advanceOnboarding;
$("skip-onboarding").onclick = finishOnboarding;
$("load-more-stickers").onclick = () => { void loadStickers(); };
document.querySelectorAll("[data-sticker-section]").forEach((button) => {
  button.onclick = () => { void selectStickerSection(button.dataset.stickerSection); };
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-sticker-section]")];
    const current = tabs.indexOf(button);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    void selectStickerSection(tabs[next].dataset.stickerSection);
  };
});
$("sticker-search").oninput = () => {
  window.clearTimeout($("sticker-search").searchTimer);
  $("sticker-search").searchTimer = window.setTimeout(async () => {
    if (state.stickerLoadPromise) await state.stickerLoadPromise;
    state.stickerQuery = $("sticker-search").value.trim();
    await loadStickers({ reset: true });
  }, 180);
};
$("go-in-person").onclick = openPresenceDialog;
$("confirm-presence-transition").onclick = async () => {
  if (globalRequestBusy()) return;
  $("presence-dialog").close();
  await arriveInPerson();
};
$("stay-on-communicator").onclick = async () => {
  if (globalRequestBusy()) return;
  $("presence-dialog").close();
  try { await runModeTransition((signal) => transitionPresence("text", state.selected, null, signal), "正在打开通讯器"); } catch (error) { if (error?.name !== "AbortError") showBanner(displayError(error)); }
};
$("open-communicator").onclick = async () => {
  if (globalRequestBusy()) return;
  try { await runModeTransition((signal) => transitionPresence("text", state.selected, null, signal), "正在打开通讯器"); } catch (error) { if (error?.name !== "AbortError") showBanner(displayError(error)); }
};
$("open-contacts").onclick = () => toggleContacts({ mobileOpen: true });
$("open-stage-contacts").onclick = () => toggleContacts();
$("close-contacts").onclick = () => toggleContacts({ mobileOpen: false });
$("open-info").onclick = () => openDrawer("info-panel");
$("open-announcements").onclick = $("stage-open-announcements").onclick = () => openAnnouncements();
$("retry-announcements").onclick = () => refreshAnnouncements();
document.querySelectorAll("[data-announcement-tab]").forEach((button) => {
  button.onclick = () => selectAnnouncementTab(button.dataset.announcementTab);
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "updates" : event.key === "End" ? "birthdays" : announcements.tab === "updates" ? "birthdays" : "updates";
    selectAnnouncementTab(next, true);
  };
});
$("announcements-dialog").addEventListener("cancel", acknowledgeAnnouncements);
$("announcements-dialog").addEventListener("close", acknowledgeAnnouncements);
document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("close", () => window.setTimeout(maybeShowAnnouncements, 0)));
window.addEventListener("focus", announcementTick);
document.addEventListener("visibilitychange", announcementTick);
window.addEventListener("storage", (event) => {
  if (event.key !== ANNOUNCEMENT_READ_KEY) return;
  readAnnouncementHistory();
  renderAnnouncementBadges();
});
$("open-transcript").onclick = () => openDrawer("transcript-panel");
$("close-info").onclick = $("close-transcript").onclick = $("drawer-scrim").onclick = closeDrawers;
$("toggle-stage-ui").onclick = () => { $("in-person-surface").classList.add("ui-hidden"); $("restore-stage-ui").hidden = false; };
$("restore-stage-ui").onclick = () => { $("in-person-surface").classList.remove("ui-hidden"); $("restore-stage-ui").hidden = true; };
$("open-global-feedback").onclick = $("floating-feedback").onclick = () => openFeedback();
$("feedback-form").onsubmit = submitFeedback;
$("open-settings").onclick = () => openSettings("models");
$("header-config-model").onclick = () => openSettings("models");
$("stage-open-settings").onclick = () => { $("stage-menu").open = false; openSettings("models"); };
$("stage-open-transcript").onclick = () => { $("stage-menu").open = false; openDrawer("transcript-panel"); };
$("stage-toggle-ui").onclick = () => { $("stage-menu").open = false; $("in-person-surface").classList.add("ui-hidden"); $("restore-stage-ui").hidden = false; };
$("stage-open-feedback").onclick = () => { const messageId = $("stage-open-feedback").dataset.messageId || ""; if (!messageId) return; $("stage-menu").open = false; openFeedback(messageId); };
$("discover-models").onclick = discoverModels;
$("save-model").onclick = saveModelSession;
$("model-mode-api").onclick = () => chooseModelMode("api");
$("model-mode-subscription").onclick = () => chooseModelMode("subscription");
$("subscription-model-select").onchange = () => chooseSubscriptionModel($("subscription-model-select").value);
$("subscription-effort-select").onchange = () => {
  const effort = $("subscription-effort-select").value;
  state.subscription.draftEffort = subscriptionCombinationSupported(state.subscription.draftModel, effort) ? effort : "";
  state.subscription.selectionNotice = "";
  renderSubscriptionSettings();
};
$("pair-subscription").onclick = pairSubscription;
$("disconnect-subscription").onclick = disconnectSubscription;
$("copy-subscription-code").onclick = async () => {
  if (!state.subscription.pairingCode) return;
  try { await navigator.clipboard.writeText(state.subscription.pairingCode); toast("配对码已复制，请只输入到你自己的连接器"); }
  catch { toast("无法自动复制，请选中配对码后手动复制"); }
};
$("clear-credential").onclick = () => { clearCredential(); $("api-key").focus(); toast("当前标签页的模型凭证已清除"); };
$("discovered-models").onchange = () => { if (state.modelSetupMode !== "subscription" && $("discovered-models").value) { $("model-id").value = $("discovered-models").value; state.model = $("discovered-models").value; saveCredential(); } };
$("toggle-advanced-model").onclick = () => {
  const panel = $("advanced-model-panel");
  const expanded = panel.hidden;
  panel.hidden = !expanded;
  $("toggle-advanced-model").setAttribute("aria-expanded", String(expanded));
};
$("provider-select").onchange = () => chooseProvider($("provider-select").value);
$("auto-summary-enabled").onchange = async () => {
  state.autoSummaryEnabled = $("auto-summary-enabled").checked;
  if (!state.autoSummaryEnabled) cancelBackgroundSummaries();
  await saveConversationPreferences();
  if (state.autoSummaryEnabled) toast("将在下一轮仍有模型调用余量时整理连续性摘要");
};
$("day-continuity-preference").onchange = async () => {
  state.dayContinuityPreference = $("day-continuity-preference").value === "continue_previous" ? "continue_previous" : "start_today";
  await saveConversationPreferences();
  toast("跨日聊天方式已保存，下次进入新日期时生效");
};
$("history-retention").onchange = async () => {
  state.historyRetentionDays = [30, 90].includes(Number($("history-retention").value)) ? Number($("history-retention").value) : 0;
  await saveUiPreferences();
  try {
    await pruneExpiredMessages();
    toast(state.historyRetentionDays ? `将自动清理超过 ${state.historyRetentionDays} 天的本地消息` : "已关闭自动清理");
  } catch { /* prune displays the actionable failure without pretending success */ }
};
document.querySelectorAll("[data-settings-tab]").forEach((button) => {
  button.onclick = () => openSettings(button.dataset.settingsTab);
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-settings-tab]")];
    const current = tabs.indexOf(button);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    openSettings(tabs[next].dataset.settingsTab);
    tabs[next].focus();
  };
});
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  if (!button.getAttribute("aria-label")) button.setAttribute("aria-label", "关闭对话框");
  button.onclick = () => {
    // Persist before close(): its event is asynchronous and a quick reload
    // must not lose the receipt or show the same announcement again.
    if (button.dataset.closeDialog === "announcements-dialog") acknowledgeAnnouncements();
    $(button.dataset.closeDialog).close();
  };
});
$("feedback-dialog").addEventListener("close", () => prepareFeedbackVerification({ cancelPending: true }));
$("settings-dialog").addEventListener("close", () => { modelSetupController?.abort(); stopSubscriptionPolling(); });
$("settings-dialog").addEventListener("cancel", () => { modelSetupController?.abort(); stopSubscriptionPolling(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) stopSubscriptionPolling(); else startSubscriptionPolling(); });
$("sticker-picker").addEventListener("close", () => $("toggle-sticker").setAttribute("aria-expanded", "false"));
window.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  if (trapDrawerFocus(event)) return;
  if (window.matchMedia("(max-width: 820px)").matches && $("contact-panel").classList.contains("open")) {
    if (event.key === "Escape") { event.preventDefault(); toggleContacts(); return; }
    if (focusWithin(event, $("contact-panel"))) return;
  }
  if (event.key.toLocaleLowerCase() === "h" && currentThread()?.channel === "in_person" && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    const hidden = $("in-person-surface").classList.toggle("ui-hidden");
    $("restore-stage-ui").hidden = !hidden;
  }
});

$("character-list").addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const options = [...$("character-list").querySelectorAll("[data-character]")];
  if (!options.length) return;
  event.preventDefault();
  const current = options.indexOf(document.activeElement);
  const target = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : Math.max(0, Math.min(options.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
  options[target].focus();
});
$("character-search").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown") return;
  const first = $("character-list").querySelector("[data-character]");
  if (first) { event.preventDefault(); first.focus(); }
});
window.matchMedia("(max-width: 820px)").addEventListener("change", syncResponsiveLayout);
let messageInputWidth = 0;
const messageInputResizeObserver = new ResizeObserver(([entry]) => {
  const width = entry.borderBoxSize?.[0]?.inlineSize ?? entry.contentRect.width;
  if (width === messageInputWidth) return;
  messageInputWidth = width;
  // Height-only observations must not loop. Defer width-driven layout work;
  // recording zero also makes showing a previously hidden composer remeasure.
  if (width > 0) scheduleMessageInputResize();
});
messageInputResizeObserver.observe($("message-input"));
document.fonts?.ready.then(scheduleMessageInputResize);
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", event => {
  syncMotionState();
  if (event.matches) { surfaceMotion.cancel(); transientMotion.cancel(); cancelStageMotion(); cancelStageArtTransition(); finishReceivedPresentation(); renderStage(); }
  refreshAnimatedStickers();
});
document.addEventListener("visibilitychange", () => {
  syncMotionState();
  if (document.hidden) { void saveDraftNow(); cancelBackgroundSummaries(); surfaceMotion.cancel(); transientMotion.cancel(); cancelStageMotion(); cancelStageArtTransition(); finishReceivedPresentation(); }
  refreshAnimatedStickers();
  if (!document.hidden) sampleVisibleReplyPaints();
});

const WRITER_LOCK_NAME = "project-snow-public:writer:v1";
async function tryWriterLease() {
  const db = await openDB();
  if (!db) return true;
  return new Promise((resolve, reject) => {
    const tx = db.transaction("app_state", "readwrite");
    const store = tx.objectStore("app_state");
    let owned = false;
    const read = store.get("writer_lease");
    read.onsuccess = () => {
      const lease = read.result;
      if (!lease || lease.owner === state.writerId || lease.expiresAt <= Date.now()) {
        store.put({ key: "writer_lease", owner: state.writerId, expiresAt: Date.now() + 15000 });
        owned = true;
      }
    };
    tx.oncomplete = () => resolve(owned);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}
async function acquireWriter() {
  // Without a persistent database there is no shared history to coordinate.
  if (!(await openDB())) {
    state.writerOwned = true;
    state.writerReady = true;
    $("tab-session-banner").hidden = true;
    updateComposerAvailability();
    return true;
  }
  if (navigator.locks?.request) {
    const owned = await new Promise((resolve) => {
      void navigator.locks.request(WRITER_LOCK_NAME, { ifAvailable: true }, async (lock) => {
        if (!lock) { resolve(false); return; }
        await new Promise((release) => { state.writerRelease = release; resolve(true); });
      }).catch(() => resolve(false));
    });
    state.writerOwned = owned;
  } else {
    state.writerOwned = await tryWriterLease().catch(() => false);
    if (state.writerOwned && !state.writerRenewTimer) state.writerRenewTimer = window.setInterval(async () => {
      if (!(await tryWriterLease().catch(() => false))) await releaseWriter();
    }, 5000);
  }
  state.writerReady = true;
  $("tab-session-banner").hidden = state.writerOwned;
  updateComposerAvailability();
  return state.writerOwned;
}
async function releaseWriter() {
  cancelBackgroundSummaries();
  for (const request of state.chatRequestByCharacter.values()) request.controller.abort();
  await saveDraftNow();
  // Persist pending request IDs before another tab may resume them.
  await Promise.all([...state.threads.values()].map(dbPutThread));
  state.writerOwned = false;
  state.historyEpoch += 1;
  state.writerRelease?.();
  state.writerRelease = null;
  window.clearInterval(state.writerRenewTimer);
  state.writerRenewTimer = 0;
  if (!navigator.locks?.request) {
    const db = await openDB();
    if (db) await new Promise((resolve) => {
      const tx = db.transaction("app_state", "readwrite");
      const store = tx.objectStore("app_state");
      const request = store.get("writer_lease");
      request.onsuccess = () => { if (request.result?.owner === state.writerId) store.delete("writer_lease"); };
      tx.oncomplete = tx.onerror = tx.onabort = resolve;
    });
  }
  $("tab-session-banner").hidden = false;
  updateComposerAvailability();
  state.writerChannel?.postMessage({ type: "released", sender: state.writerId });
}
async function refreshStoredHistory() {
  state.historyEpoch += 1;
  state.threads.clear();
  state.persistedMessages.clear();
  for (const store of Object.values(state.memoryStores)) store.clear();
  state.worldPackage = "";
  state.dialogueByCharacter.clear();
  await migrateBrowserState();
  const selected = state.selected;
  state.selected = "";
  await loadCharacters();
  if (selected && selected !== state.selected) await selectCharacter(selected, { closeContacts: false });
  restoreDraft();
  renderAll();
}
function broadcastHistoryChange() {
  state.writerChannel?.postMessage({ type: "history-changed", sender: state.writerId });
}
async function startTabCoordination() {
  state.writerId = id();
  if (typeof BroadcastChannel === "function") {
    try { state.writerChannel = new BroadcastChannel(WRITER_LOCK_NAME); } catch { /* locks still prevent concurrent writes */ }
  }
  if (state.writerChannel) {
    state.writerChannel.onmessage = async ({ data }) => {
      if (!data || data.sender === state.writerId) return;
      if (data.type === "take-over" && state.writerOwned) await releaseWriter();
      if (data.type === "released" && $("take-over-session").disabled) {
        if (await acquireWriter()) await refreshStoredHistory();
        $("take-over-session").disabled = false;
      }
      if (data.type === "history-changed" && !state.writerOwned) await refreshStoredHistory();
    };
  }
  await acquireWriter();
}
$("take-over-session").onclick = async () => {
  $("take-over-session").disabled = true;
  if (await acquireWriter()) {
    await refreshStoredHistory();
    $("take-over-session").disabled = false;
    return;
  }
  state.writerChannel?.postMessage({ type: "take-over", sender: state.writerId });
  window.setTimeout(async () => {
    if (!$("take-over-session").disabled) return;
    if (await acquireWriter()) await refreshStoredHistory();
    else toast("另一标签页尚未释放会话，请关闭该页后重试。");
    $("take-over-session").disabled = false;
  }, 1500);
};

async function deleteCharacterHistory() {
  if (!state.writerOwned || globalRequestBusy()) return;
  cancelBackgroundSummaries();
  const characterId = $("history-character").value;
  state.historyEpoch += 1;
  for (const thread of state.threads.values()) if (thread.characterId !== characterId) thread.historyEpoch = state.historyEpoch;
  state.threads.delete(characterId);
  await deleteMessagesForCharacter(characterId);
  await storeDelete("threads", characterId);
  await storeDelete("app_state", dialogueRecordKey(characterId));
  state.dialogueByCharacter.delete(characterId);
  state.sceneActionByCharacter.delete(characterId);
  state.sceneByCharacter.delete(characterId);
  for (const key of state.drafts.keys()) if (key.startsWith(`${characterId}:`)) state.drafts.delete(key);
  await persistDrafts();
  if (characterId === state.selected) { await dbGetThread(characterId); restoreDraft(); renderAll(); }
  broadcastHistoryChange();
  openSettings("history");
  toast("该角色本地历史已删除");
}
async function clearAllHistory() {
  if (!state.writerOwned || globalRequestBusy()) return;
  if (!window.confirm("确定清空全部 小吉终端 本地历史与世界状态吗？")) return;
  cancelBackgroundSummaries();
  state.historyEpoch += 1;
  const db = await openDB();
  if (db) await clearHistoryDatabase(db, !navigator.locks?.request
    ? { key: "writer_lease", owner: state.writerId, expiresAt: Date.now() + 15000 } : undefined);
  for (const store of Object.values(state.memoryStores)) store.clear();
  state.threads.clear(); state.persistedMessages.clear(); state.worldPackage = "";
  state.dialogueByCharacter.clear(); state.sceneActionByCharacter.clear(); state.sceneByCharacter.clear();
  state.drafts.clear(); state.pinnedCharacters.clear(); state.favoriteStickerIds.clear();
  state.favoriteStickers.clear(); state.recentStickerIds = []; state.rendezvousDismissals.clear();
  if (state.selected) { await dbGetThread(state.selected); restoreDraft(); await resolvePresence(); }
  broadcastHistoryChange();
  renderAll(); openSettings("history"); toast("全部本地历史已清空");
}
$("delete-character-history").onclick = () => { void deleteCharacterHistory().catch(() => showBanner("删除未能保存，请导出备份后重试。")); };
$("clear-all-history").onclick = () => { void clearAllHistory().catch(() => showBanner("清空未能保存，请导出备份后重试。")); };

let pendingHistoryImport = null;
async function exportHistory() {
  await saveDraftNow();
  const backup = portableCopy({
    schema: "project-snow-history-1", databaseVersion: 4, exportedAt: new Date().toISOString(),
    threads: await storeAll("threads"), messages: (await storeAll("messages")).map(message => ({ ...message,
      ...(message.requestSnapshot?.envelope && message.status !== "sent" ? { errorCode: "request_result_unavailable", status: "failed" } : {}),
      requestSnapshot: message.requestSnapshot?.envelope ? null : message.requestSnapshot })),
    appState: (await storeAll("app_state")).filter((entry) => ["drafts", "preferences", "ui_preferences"].includes(entry.key)),
  });
  const url = URL.createObjectURL(new Blob([JSON.stringify(backup)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `project-snow-history-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.append(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function importHistory(backup) {
  if (!state.writerOwned || globalRequestBusy()) throw new Error("history_busy");
  cancelBackgroundSummaries();
  await saveDraftNow();
  const db = await openDB();
  if (!db) throw new Error("history_storage_required");
  await mergeHistoryDatabase(db, { ...backup, messages: backup.messages.map(message => ({ ...message,
    ...((message.requestSnapshot?.envelope || plain(message.id).startsWith("user:")) && message.status !== "sent" ? { errorCode: "request_result_unavailable", status: "failed" } : {}),
    requestSnapshot: message.requestSnapshot?.envelope ? null : message.requestSnapshot })) }, normalizeMessage);
  await refreshStoredHistory();
  broadcastHistoryChange();
}
$("export-history").onclick = () => { void exportHistory().catch(() => showBanner("无法导出备份，请重试。")); };
$("import-history").onclick = () => $("history-import-file").click();
$("history-import-file").onchange = async () => {
  const file = $("history-import-file").files?.[0];
  $("history-import-file").value = "";
  if (!file) return;
  try {
    if (file.size > 50 * 1024 * 1024) throw new Error("backup_too_large");
    pendingHistoryImport = validateHistoryBackup(JSON.parse(await file.text()));
    $("history-import-preview").textContent = `备份包含 ${pendingHistoryImport.threads.length} 位角色、${pendingHistoryImport.messages.length} 条消息。仅补充尚不存在的消息，不覆盖当前记录。`;
    $("history-import-error").textContent = "";
    $("history-import-dialog").showModal();
  } catch { showBanner("无法导入：请选择有效的本地历史备份（最大 50 MB）。"); }
};
$("confirm-history-import").onclick = async () => {
  if (!pendingHistoryImport) return;
  $("confirm-history-import").disabled = true;
  try {
    await importHistory(pendingHistoryImport);
    pendingHistoryImport = null;
    $("history-import-dialog").close();
    toast("备份已合并，原有消息和草稿已保留。");
  } catch { $("history-import-error").textContent = "未能合并备份。请确认当前标签页拥有会话，且本地存储可用。"; }
  finally { $("confirm-history-import").disabled = false; }
};

let initialBuild = null;
let pendingBuild = null;
async function checkBuildUpdate() {
  if (document.hidden) return;
  try {
    const info = await api("/build-info", { timeoutMs: 8000, cache: "no-store" });
    if (!info.app_version) return;
    if (!initialBuild) { initialBuild = info; return; }
    const changed = info.app_version !== initialBuild.app_version || (info.revision && initialBuild.revision && info.revision !== initialBuild.revision);
    if (!changed || buildIdentity(info) === tabPreferences.getItem("project-snow-public:dismissed-build")) return;
    pendingBuild = info;
    $("build-update-copy").textContent = info.api_schema && info.api_schema !== "public-v1" || info.state_schema && info.state_schema !== "public-state-2"
      ? "服务已有新版本，请先保存草稿并更新页面。" : "新版本已就绪，更新前会保存草稿。";
    $("build-update-banner").hidden = false;
    $("apply-build-update").disabled = globalRequestBusy();
  } catch { /* Optional deployment metadata never blocks the conversation. */ }
}
$("dismiss-build-update").onclick = () => {
  if (pendingBuild) tabPreferences.setItem("project-snow-public:dismissed-build", buildIdentity(pendingBuild));
  $("build-update-banner").hidden = true;
};
$("apply-build-update").onclick = async () => {
  if (globalRequestBusy()) { toast("当前回复完成后即可更新。"); return; }
  cancelBackgroundSummaries();
  await saveDraftNow();
  await Promise.all([...state.threads.values()].map(dbPutThread));
  window.location.reload();
};
window.addEventListener("focus", () => { void checkBuildUpdate(); });
window.addEventListener("pagehide", () => {
  stopSubscriptionPolling();
  state.writerRelease?.();
  state.writerChannel?.close();
  state.stageWarmController?.abort();
  cancelStageArt();
  clearStagePresentationCache();
});

async function boot() {
  await startTabCoordination();
  await openDB();
  await migrateBrowserState();
  await pruneExpiredMessages();
  await loadConfig();
  // P6a：开关只在 config 解析后才已知，故开机/锁屏层只能此时才出现。
  // 桌面 → 终端开机（真实加载步驱动，就绪即消隐，不加延迟）；手机 → 终端锁屏。
  // 开场层永不阻断启动：任何构造异常都降级为「无开场层」。
  let snowBoot = false;
  let snowLock = false;
  try { snowBoot = maybeStartSnowBoot(); } catch { snowBoot = false; }
  try { snowLock = maybeStartSnowLock(); } catch { snowLock = false; }
  if (snowBoot) snowBootStep("读取本地记录");
  // 体验须知与新手引导：桌面开机消隐后、手机解锁后按正常顺序出现（绝不盖在开场层之上）；
  // 其余情况（flag 关 / 跳过开机锁屏）保持原有顺序——config 之后立即弹须知。
  if (!snowBoot && !snowLock) await showExperienceNoticeIfNeeded();
  await loadCharacters();
  if (snowBoot) { snowBootStep("就绪"); dismissSnowBoot(() => { void snowRunDeferredOpenings(); }); }
  void loadStageRelease(state.config?.stage_release, state.characters.map(item => item.character_id)).then(release => {
    state.stageRelease = release;
    if (release && currentThread()?.channel === "in_person") renderStage();
  }).catch(() => { state.stageRelease = null; });
  $("connection-status").textContent = "服务已连接";
  const contactsOpen = contactsExpanded();
  syncContactToggleState(contactsOpen);
  $("contact-panel").setAttribute("aria-hidden", String(!contactsOpen));
  $("contact-panel").inert = !contactsOpen;
  $("info-panel").inert = true;
  $("transcript-panel").inert = true;
  $("auto-summary-enabled").checked = state.autoSummaryEnabled;
  updateComposerAvailability();
  restoreDraft();
  // P6a：桌面开关开启时，通讯器背后挂载世界背景（人在外→generic 外景）。
  syncSnowWorld();
  renderOnboarding();
  if (!state.storageAvailable) showBanner("浏览器未开放本地存储，本次聊天不会保存；仍可继续使用。 ");
  startAnnouncements();
  void checkBuildUpdate();
  window.setInterval(() => { void checkBuildUpdate(); }, 300000);
}
boot().catch((error) => {
  // 启动失败（含 /public/v1/config 取回失败）也必须立刻解除首帧中性保持，绝不卡白。
  snowClearFirstFlashHold();
  $("snow-boot")?.remove();
  const lock = $("snow-lock");
  if (lock) { $("chat-app").inert = Boolean(lock.snowPreviousInert); lock.remove(); }
  $("connection-status").textContent = "连接失败";
  showBanner(displayError(error));
});
