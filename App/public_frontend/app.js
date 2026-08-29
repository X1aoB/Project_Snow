const apiRoot = "/public/v1";
const dbName = "project-snow-public";
const dbVersion = 4;
const SCENE_KEYS = new Set(["generic", "quarters", "lounge", "training", "archive", "canteen", "observation", "medical", "corridor"]);
const state = {
  config: null,
  credential: "",
  credentialExpiresAt: 0,
  provider: "",
  model: "",
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
  feedbackMessageId: "",
  arrivalPending: false,
  autoSummaryEnabled: true,
  selectionSequence: 0,
  selectionController: null,
  typewriter: { key: "", timer: 0, fullText: "", displayedText: "" },
  summaryInFlight: new Set(),
  continuityPrompt: null,
  storageAvailable: true,
  memoryStores: { threads: new Map(), messages: new Map(), app_state: new Map() },
  drafts: new Map(),
  draftTimer: 0,
  pinnedCharacters: new Set(),
  timelineVisibleLimit: 60,
  drawerReturnFocus: null,
  contactReturnFocus: null,
  onboardingStep: 0,
  historyRetentionDays: 0,
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
  turnstile_required: "人机验证未完成，请稍后重试。",
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

const STATE_PACKAGE_RECOVERY_CODES = new Set(["state_subject_mismatch", "state_invalid"]);

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
function displayError(error) {
  const code = error instanceof Error ? error.message : plain(error);
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
    value: Object.freeze({ escapeHtml, fitPublicRequestPayload, deriveDisplayBlocks, statePackageOrder }),
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
function reducedMotion() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
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
  const scale = Math.min(1, TYPEWRITER_MAX_DURATION_MS / Math.max(1, rawDuration));
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
function localDayKey(timestamp = Date.now()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function newConversationSegment() { return id(); }

async function api(path, options = {}) {
  const { timeoutMs = 0, signal: callerSignal, ...fetchOptions } = options;
  const headers = { ...(fetchOptions.headers || {}) };
  if (fetchOptions.method && fetchOptions.method !== "GET") headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  let relayAbort = null;
  if (callerSignal) {
    relayAbort = () => controller.abort();
    if (callerSignal.aborted) controller.abort();
    else callerSignal.addEventListener("abort", relayAbort, { once: true });
  }
  const timeout = timeoutMs > 0 ? window.setTimeout(() => controller.abort(), timeoutMs) : 0;
  try {
    const response = await fetch(`${apiRoot}${path}`, {
      credentials: "same-origin",
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.detail?.code || "request_failed");
    return payload;
  } catch (error) {
    if (error?.name === "AbortError" && timeoutMs > 0 && !callerSignal?.aborted) {
      throw new Error("request_timeout");
    }
    throw error;
  } finally {
    if (timeout) window.clearTimeout(timeout);
    if (callerSignal && relayAbort) callerSignal.removeEventListener("abort", relayAbort);
  }
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

async function loadMessagePage(characterId, { before = Number.MAX_SAFE_INTEGER, limit = 60 } = {}) {
  const db = await openDB();
  if (!db) {
    return [...state.memoryStores.messages.values()]
      .filter((message) => message.characterId === characterId && Number(message.createdAt || 0) < before)
      .sort((a, b) => Number(b.createdAt || 0) - Number(a.createdAt || 0))
      .slice(0, limit)
      .reverse()
      .map(normalizeMessage);
  }
  try {
    return await new Promise((resolve, reject) => {
    const values = [];
    const index = db.transaction("messages", "readonly").objectStore("messages").index("by_character_created");
    const upper = before >= Number.MAX_SAFE_INTEGER ? Number.MAX_SAFE_INTEGER : Math.max(0, before);
    const range = IDBKeyRange.bound([characterId, 0], [characterId, upper], false, before < Number.MAX_SAFE_INTEGER);
    const request = index.openCursor(range, "prev");
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor || values.length >= limit) {
        resolve(values.reverse().map(normalizeMessage));
        return;
      }
      values.push(cursor.value);
      cursor.continue();
    };
    request.onerror = () => reject(request.error);
    });
  } catch {
    useMemoryStorage();
    return [...state.memoryStores.messages.values()]
      .filter((message) => message.characterId === characterId && Number(message.createdAt || 0) < before)
      .sort((a, b) => Number(b.createdAt || 0) - Number(a.createdAt || 0))
      .slice(0, limit)
      .reverse()
      .map(normalizeMessage);
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
  const displayBlocks = role === "assistant"
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
    createdAt: hasCreatedAt ? parsedCreatedAt : Date.now(),
    createdAtEstimated: Boolean(message.createdAtEstimated || message.created_at_estimated || !hasCreatedAt),
    status: ["sent", "pending", "failed"].includes(message.status) ? message.status : "sent",
    requestId: plain(message.requestId || message.request_id),
    movementLocationId: plain(message.movementLocationId || message.movement_location_id),
    requestSnapshot: message.requestSnapshot && typeof message.requestSnapshot === "object"
      ? structuredClone(message.requestSnapshot)
      : null,
    errorCode: message.errorCode || "",
    source: message.source || "chat",
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
  return {
    characterId,
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
async function dbPutThread(thread) {
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
  for (const message of thread.messages) {
    state.memoryStores.messages.set(message.id, structuredClone(message));
  }
  const db = await openDB();
  if (!db) {
    await storePut("threads", metadata);
    await Promise.all(thread.messages.map((message) => storePut("messages", message)));
  } else {
    try {
      await new Promise((resolve, reject) => {
        const tx = db.transaction(["threads", "messages"], "readwrite");
        tx.objectStore("threads").put(metadata);
        for (const message of thread.messages) tx.objectStore("messages").put(message);
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error("indexeddb_write_aborted"));
      });
    } catch {
      useMemoryStorage();
    }
  }
  thread.messageCount = metadata.messageCount;
}

async function loadOlderMessages() {
  const thread = currentThread();
  if (!thread?.hasOlderMessages || !thread.messages.length) return;
  const timeline = $("timeline");
  const previousHeight = timeline.scrollHeight;
  const previousTop = timeline.scrollTop;
  const oldest = Math.min(...thread.messages.map((message) => Number(message.createdAt || 0)));
  const older = await loadMessagePage(thread.characterId, { before: oldest, limit: 40 });
  const known = new Set(thread.messages.map((message) => message.id));
  const additions = older.filter((message) => !known.has(message.id));
  thread.messages = [...additions, ...thread.messages];
  thread.hasOlderMessages = additions.length === 40 && thread.messages.length < Number(thread.messageCount || 0);
  renderTimeline({ preserveScroll: true });
  timeline.scrollTop = previousTop + (timeline.scrollHeight - previousHeight);
}
function decodeStatePackage(token) {
  try {
    const encoded = token.split(".", 1)[0].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded + "=".repeat((4 - encoded.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return {}; }
}
function statePackageOrder(token) {
  const decoded = typeof token === "string" ? decodeStatePackage(token) : (token || {});
  const scheduleDate = plain(decoded.schedule_date || decoded.world?.schedule_date || decoded.date).match(/^\d{4}-\d{2}-\d{2}$/)?.[0] || "";
  return { scheduleDate, revision: Number(decoded.revision || decoded.world?.revision || 0) };
}
async function saveWorldPackage(token) {
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
    await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
    return true;
  }
  if (state.worldPackage && incoming.scheduleDate !== current.scheduleDate) {
    // A dated 0.9.2 package supersedes an undated legacy package. An undated
    // late response must never replace a dated package from the current day.
    if (!incoming.scheduleDate) return false;
    state.worldPackage = token;
    await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
    return true;
  }
  if (state.worldPackage && incoming.revision < current.revision) return false;
  if (state.worldPackage && incoming.revision === current.revision) {
    return token === state.worldPackage;
  }
  state.worldPackage = token;
  await storePut("app_state", { key: "world", statePackage: token, revision: incoming.revision, scheduleDate: incoming.scheduleDate });
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
  const payload = { threads: await storeAll("threads"), messages: await storeAll("messages"), appState: await storeAll("app_state") };
  return new Blob([JSON.stringify(payload)]).size;
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
  if (!state.historyRetentionDays) return;
  const cutoff = Date.now() - state.historyRetentionDays * 86400000;
  const expired = (await storeAll("messages")).filter((message) => Number(message.createdAt || 0) < cutoff);
  if (!expired.length) return;
  await Promise.all(expired.map((message) => storeDelete("messages", message.id)));
  const counts = new Map();
  for (const message of await storeAll("messages")) counts.set(message.characterId, Number(counts.get(message.characterId) || 0) + 1);
  for (const record of await storeAll("threads")) {
    await storePut("threads", { ...record, messageCount: Number(counts.get(record.characterId) || 0) });
  }
}

function draftKey(characterId = state.selected, channel = currentThread()?.channel || "text", field = "message") {
  return `${plain(characterId)}:${channel === "in_person" ? "in_person" : "text"}:${field}`;
}
async function saveDraftNow() {
  window.clearTimeout(state.draftTimer);
  if (!state.selected) return;
  const channel = currentThread()?.channel || "text";
  state.drafts.set(draftKey(state.selected, channel, "message"), $("message-input").value);
  state.drafts.set(draftKey(state.selected, channel, "action"), $("action-input").value);
  await storePut("app_state", { key: "drafts", values: Object.fromEntries(state.drafts) });
}
function scheduleDraftSave() {
  window.clearTimeout(state.draftTimer);
  state.draftTimer = window.setTimeout(() => { void saveDraftNow(); }, 220);
}
function restoreDraft() {
  if (!state.selected) return;
  const channel = currentThread()?.channel || "text";
  $("message-input").value = state.drafts.get(draftKey(state.selected, channel, "message")) || "";
  $("action-input").value = state.drafts.get(draftKey(state.selected, channel, "action")) || "";
  updateInputCount();
}

function sessionKey() { return "project-snow-public:byok"; }
function saveCredential() {
  if (!state.credential || !state.credentialExpiresAt) return;
  sessionStorage.setItem(sessionKey(), JSON.stringify({ credential: state.credential, provider: state.provider, model: state.model, expiresAt: state.credentialExpiresAt }));
}
function clearCredential() {
  sessionStorage.removeItem(sessionKey());
  state.credential = "";
  state.credentialExpiresAt = 0;
  state.model = "";
  refreshCredentialStatus();
  updateComposerAvailability();
}
function configured() {
  if (state.credentialExpiresAt && state.credentialExpiresAt <= Date.now()) clearCredential();
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
    $("credential-status").textContent = `加密凭证有效至 ${expires}；获取模型列表和切换角色无需重新输入 Key。`;
  }
}

async function waitForTurnstile() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (window.turnstile) return window.turnstile;
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
  return null;
}
async function tokenFor(action) {
  const sitekey = state.config?.turnstile_site_key;
  if (!sitekey) return "development-bypass";
  const turnstile = await waitForTurnstile();
  if (!turnstile) throw new Error("turnstile_unavailable");
  return new Promise((resolve, reject) => {
    const container = document.createElement("div");
    document.body.append(container);
    let widget = "";
    const cleanup = () => { if (widget !== "") turnstile.remove(widget); container.remove(); };
    try {
      widget = turnstile.render(container, {
        sitekey, action, execution: "execute", appearance: "interaction-only",
        callback: (token) => { cleanup(); resolve(token); },
        "error-callback": () => { cleanup(); reject(new Error("turnstile_required")); },
        "expired-callback": () => { cleanup(); reject(new Error("turnstile_required")); },
      });
      turnstile.execute(widget);
    } catch { cleanup(); reject(new Error("turnstile_unavailable")); }
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
function experienceNoticeAccepted() { return localStorage.getItem(experienceNoticeKey()) === "accepted"; }
async function showExperienceNoticeIfNeeded() {
  if (experienceNoticeAccepted()) return;
  const dialog = $("experience-notice-dialog");
  await new Promise((resolve) => {
    let settled = false;
    const finish = async (accepted) => {
      if (settled) return;
      settled = true;
      if (accepted) {
        localStorage.setItem(experienceNoticeKey(), "accepted");
        state.autoSummaryEnabled = $("notice-auto-summary").checked;
        await storePut("app_state", { key: "preferences", autoSummaryEnabled: state.autoSummaryEnabled });
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
}
function renderMobileProviderOptions() {
  const root = $("provider-options-mobile");
  if (!root) return;
  root.innerHTML = (state.config?.providers || []).map((provider) => `<button type="button" role="radio" data-provider-option="${escapeHtml(provider.provider_id)}" aria-checked="false" tabindex="-1">${escapeHtml(provider.display_name)}</button>`).join("");
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
  $("version-badge").textContent = state.config.app_version || "0.9.3";
  $("github-link").href = state.config.source_links.project_snow;
  $("website-github-link").href = state.config.source_links.mywebsite;
  $("releases-link").href = state.config.source_links.releases;
  $("provider-select").innerHTML = state.config.providers.length
    ? state.config.providers.map((provider) => `<option value="${escapeHtml(provider.provider_id)}">${escapeHtml(provider.display_name)}</option>`).join("")
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
  $("provider-empty").hidden = Boolean(state.config.providers.length);
  $("discover-models").disabled = !state.config.providers.length;
  $("save-model").disabled = !state.config.providers.length;
  if (!state.config.providers.length) showError("setup-error", "当前暂未开放可用的模型厂商。");
  attachTurnstile();
  const stored = sessionStorage.getItem(sessionKey());
  if (stored) {
    try {
      const saved = JSON.parse(stored);
      const available = state.config.providers.some((item) => item.provider_id === saved.provider);
      if (saved.expiresAt > Date.now() && available) {
        state.credential = saved.credential;
        state.credentialExpiresAt = saved.expiresAt;
        state.provider = saved.provider;
        state.model = saved.model || "";
        $("provider-select").value = state.provider;
        syncProviderControls(state.provider);
        $("model-id").value = state.model;
      } else clearCredential();
    } catch { clearCredential(); }
  }
  refreshCredentialStatus();
  syncProviderControls($("provider-select").value);
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
async function issueCredential() {
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
  const payload = await api("/byok/session", {
    method: "POST",
    body: JSON.stringify({
      provider,
      api_key: apiKey,
      turnstile_token: await tokenFor("byok-session"),
      accepted_transit_notice: true,
      accepted_cost_notice: true,
      accepted_local_history_notice: true,
    }),
  });
  state.credential = payload.credential;
  state.provider = provider;
  state.credentialExpiresAt = Date.parse(payload.expires_at);
  $("api-key").value = "";
  saveCredential();
  refreshCredentialStatus();
}
async function discoverModels() {
  showError("setup-error", "");
  try {
    if (!$("provider-select").value) throw new Error("provider_not_enabled");
    if (!state.credential || state.provider !== $("provider-select").value || state.credentialExpiresAt <= Date.now()) await issueCredential();
    const payload = await api("/byok/models", { method: "POST", body: JSON.stringify({ provider: state.provider, credential: state.credential, request_id: id() }) });
    saveCredential();
    const select = $("discovered-models");
    select.innerHTML = `<option value="">选择已发现模型</option>${payload.models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")}`;
    select.hidden = false;
  } catch (error) { showError("setup-error", error); }
}
async function saveModelSession() {
  showError("setup-error", "");
  try {
    const model = $("discovered-models").value.trim() || $("model-id").value.trim();
    if (!model) throw new Error("请填写或选择模型 ID。");
    if (!$("provider-select").value) throw new Error("provider_not_enabled");
    if (!state.credential || state.provider !== $("provider-select").value || state.credentialExpiresAt <= Date.now()) await issueCredential();
    state.provider = $("provider-select").value;
    state.model = model;
    saveCredential();
    refreshCredentialStatus();
    updateComposerAvailability();
    $("settings-dialog").close();
    toast("模型会话已保存到当前标签页");
  } catch (error) { showError("setup-error", error); }
}

function currentCharacter() { return state.characters.find((item) => item.character_id === state.selected) || null; }
function currentThread() { return state.threads.get(state.selected) || null; }
async function ensureContinuityDecision(thread) {
  if (!thread) return true;
  const today = localDayKey();
  if (!thread.localDayKey) {
    thread.localDayKey = today;
    thread.lastActiveAt = Date.now();
    await dbPutThread(thread);
    return true;
  }
  if (thread.localDayKey === today) {
    thread.lastActiveAt = Date.now();
    await dbPutThread(thread);
    return true;
  }
  const dialog = $("continuity-dialog");
  if (!dialog.dataset.bound) {
    dialog.dataset.bound = "1";
    dialog.addEventListener("close", () => {
      if (state.continuityPrompt) {
        const pending = state.continuityPrompt;
        state.continuityPrompt = null;
        pending.resolve(false);
      }
    });
  }
  return new Promise((resolve) => {
    state.continuityPrompt = { thread, today, resolve };
    const finish = async (decision) => {
      if (!state.continuityPrompt) return;
      thread.continuityDecision = decision;
      thread.localDayKey = today;
      // Continuing yesterday keeps the prior segment available for the
      // bounded recent-history window. Starting today deliberately receives a
      // fresh segment so no previous transcript can leak into the request.
      if (decision === "start_today") thread.conversationSegmentId = newConversationSegment();
      thread.lastActiveAt = Date.now();
      if (decision === "start_today") thread.pendingTopics = [];
      await dbPutThread(thread);
      const pending = state.continuityPrompt;
      state.continuityPrompt = null;
      if (dialog.open) dialog.close();
      pending.resolve(true);
    };
    $("continue-yesterday").onclick = () => finish("continue_previous");
    $("start-today").onclick = () => finish("start_today");
    dialog.showModal();
  });
}
function avatarMarkup(character, { thumbnail = true, priority = false, className = "" } = {}) {
  const avatar = character?.avatar || null;
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
    || state.typingByCharacter.size
    || state.arrivalPending
    || state.modeTransitionPending
    || state.presenceResolvePending
    || state.summaryInFlight.size,
  );
}
function presentationFor(characterId = state.selected) {
  return characterId ? state.presentationByCharacter.get(characterId) || null : null;
}
function cancelPresentationQueue(characterId = state.selected, requestId = "") {
  const queue = presentationFor(characterId);
  if (!queue || (requestId && queue.requestId !== requestId)) return false;
  queue.cancelled = true;
  queue.controller?.abort();
  if (queue.timer) window.clearTimeout(queue.timer);
  state.presentationByCharacter.delete(characterId);
  if (characterId === state.selected) {
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
async function presentAssistantTurn(characterId, requestId, message) {
  if (!ownsTypingState(characterId, requestId)) return false;
  const blocks = presentationBlocks(message);
  const inPerson = message.communicationChannel === "in_person";
  cancelPresentationQueue(characterId);
  if (!blocks.length || !ownsTypingState(characterId, requestId)) return false;
  const queue = {
    characterId,
    requestId,
    messageId: message.id,
    blocks,
    visibleCount: inPerson ? blocks.length : 1,
    timer: 0,
    cancelled: false,
    controller: new AbortController(),
  };
  const revealCounts = blocks.length <= 4
    ? blocks.map((_block, index) => index + 1)
    : [1, 2, 3, blocks.length];
  state.presentationByCharacter.set(characterId, queue);
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
      for (let step = 1; step < revealCounts.length; step += 1) {
        if (queue.cancelled || !ownsTypingState(characterId, requestId)) return false;
        const previousCount = revealCounts[step - 1];
        const visibleCount = revealCounts[step];
        const revealed = blocks.slice(previousCount, visibleCount);
        const hasSticker = revealed.some((item) => item.type === "sticker");
        const previousTextLength = [...plain(blocks[Math.max(0, previousCount - 1)]?.text)].length;
        const totalWait = hasSticker
          ? requestDelay(requestId, `sticker:${step}`, 2200, 3000)
          : Math.max(2800, Math.min(4200, 2200 + previousTextLength * 35));
        const readingWait = Math.min(totalWait, requestDelay(requestId, `reading:${step}`, 800, 1400));
        updateTypingPhase(characterId, requestId, "reading");
        if (!reducedMotion()) await abortableDelay(readingWait, queue.controller.signal);
        if (queue.cancelled || !ownsTypingState(characterId, requestId)) return false;
        updateTypingPhase(characterId, requestId, "segment");
        const preload = hasSticker
          ? Promise.all(revealed.filter((item) => item.type === "sticker").map((item) => preloadStickerBlock(item, 1200, queue.controller.signal)))
          : Promise.resolve();
        if (!reducedMotion()) await Promise.all([abortableDelay(totalWait - readingWait, queue.controller.signal), preload]);
        else await preload;
        if (queue.cancelled || !ownsTypingState(characterId, requestId)) return false;
        queue.visibleCount = visibleCount;
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
  const pending = typingStateFor();
  target.className = "request-status";
  target.setAttribute("aria-busy", pending ? "true" : "false");
  target.textContent = state.requestStatusByCharacter.get(state.selected) || "";
  if (state.requestRecoveryByCharacter.get(state.selected) === "refresh_scene") {
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
        state.requestRecoveryByCharacter.delete(state.selected);
        setRequestStatus(state.selected, "场景已重新读取");
      } catch (error) {
        setRequestStatus(state.selected, `重新读取失败：${displayError(error)}`, "", "refresh_scene");
      }
    };
    target.append(" ", refresh);
  }
  const stop = $("stop-waiting");
  if (stop) stop.hidden = !pending || ["arrival", "presenting"].includes(pending.phase);
}
function setRequestStatus(characterId, message, requestId = "", recoveryAction = "") {
  if (!characterId) return;
  if (requestId && !ownsTypingState(characterId, requestId)) return;
  if (message) state.requestStatusByCharacter.set(characterId, plain(message));
  else state.requestStatusByCharacter.delete(characterId);
  if (recoveryAction) state.requestRecoveryByCharacter.set(characterId, recoveryAction);
  else state.requestRecoveryByCharacter.delete(characterId);
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
  root.querySelectorAll(".portrait img, .stage-portrait img").forEach((image) => {
    if (image.dataset.bound) return;
    image.dataset.bound = "1";
    image.addEventListener("error", () => {
      image.closest(".portrait, .stage-portrait")?.classList.remove("has-image");
      image.remove();
    }, { once: true });
    image.addEventListener("load", () => image.closest(".portrait, .stage-portrait")?.classList.add("has-image"), { once: true });
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
        if (entry.isIntersecting && activeStickerGifs.size < 3) {
          if (image.dataset.animatedSrc) image.src = image.dataset.animatedSrc;
          activeStickerGifs.add(image);
        } else if (!entry.isIntersecting) {
          activeStickerGifs.delete(image);
          if (image.dataset.staticSrc) image.src = image.dataset.staticSrc;
        }
      }
      // An already-visible GIF may have been waiting behind the three-active
      // cap. Fill any slot freed by an item leaving the viewport without
      // enabling a fourth animation at the same time.
      if (activeStickerGifs.size < 3) {
        for (const image of document.querySelectorAll('.content-sticker img[data-animated="true"][data-visible="true"]')) {
          if (activeStickerGifs.size >= 3) break;
          if (!activeStickerGifs.has(image)) {
            if (image.dataset.animatedSrc) image.src = image.dataset.animatedSrc;
            activeStickerGifs.add(image);
          }
        }
      }
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
      return `<div class="character-row" role="listitem"><button class="character" aria-current="${character.character_id === state.selected}" data-character="${escapeHtml(character.character_id)}">${avatarMarkup(character, { thumbnail: true, priority: character.character_id === state.selected })}<span><strong>${escapeHtml(character.display_name)}</strong><small>${escapeHtml(summary)}</small></span></button><button class="pin-character" type="button" data-pin-character="${escapeHtml(character.character_id)}" aria-pressed="${pinned}" aria-label="${pinned ? "取消置顶" : "置顶"}${escapeHtml(character.display_name)}">${pinned ? "★" : "☆"}</button></div>`;
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
  localStorage.setItem("project-snow-public:onboarding", "complete");
  $("onboarding-guide").hidden = true;
}
function renderOnboarding() {
  if (localStorage.getItem("project-snow-public:onboarding") === "complete") return;
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
  const value = plain(location);
  if (/商业街|商场|购物中心|公园/.test(value)) return "observation";
  if (/档案|资料|图书/.test(value)) return "archive";
  if (/食堂|餐厅|厨房/.test(value)) return "canteen";
  if (/走廊|通道/.test(value)) return "corridor";
  if (/休息|客厅|休憩/.test(value)) return "lounge";
  if (/医务|医疗/.test(value)) return "medical";
  if (/观景|观测|天台/.test(value)) return "observation";
  if (/宿舍|房间|寝室/.test(value)) return "quarters";
  if (/训练|演习/.test(value)) return "training";
  return "generic";
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
  bindAvatarImages(node.parentElement || node);
}
async function resolvePresenceAttempt(characterId, signal, stateRecoveryAttempt = 0) {
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
  if (state.selected) await saveDraftNow();
  const sequence = ++state.selectionSequence;
  state.selectionController?.abort();
  cancelModeTransition();
  state.selectionController = new AbortController();
  const controller = state.selectionController;
  const previousId = state.selected;
  const previousThread = currentThread();
  const switchingFromStage = Boolean(previousId && previousId !== characterId && previousThread?.channel === "in_person");
  cancelPresentationQueue(previousId);
  try {
    const thread = await dbGetThread(characterId);
    if (sequence !== state.selectionSequence) return;
    if (!(await ensureContinuityDecision(thread)) || sequence !== state.selectionSequence) return;
    thread.lastActiveAt = Date.now();
    await dbPutThread(thread);
    if (sequence !== state.selectionSequence) return;

    let preparedScene = state.sceneByCharacter.get(characterId) || cachedSceneForCharacter(characterId) || null;
    let preparedPackage = "";
    let recoveredState = false;
    if (globalRequestBusy()) {
      state.deferredPresenceCharacter = characterId;
    } else {
      state.presenceResolvePending += 1;
      updateComposerAvailability();
      try {
        const operation = preparePresenceCandidate(characterId, controller.signal);
        const candidate = switchingFromStage
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
    renderCharacters();
    $("active-character").innerHTML = `${avatarMarkup(character, { thumbnail: false, priority: true, className: "large" })}<div><h1>${escapeHtml(character.display_name)}</h1><p>文字通讯</p></div>`;
    fillAvatar($("stage-header-avatar"), character, { thumbnail: true, priority: true });
    fillAvatar($("stage-portrait-avatar"), character, { thumbnail: false, priority: true });
    $("stage-character-name").textContent = character.display_name;
    $("stage-speaker").textContent = character.display_name;

    renderScene();
    if (globalRequestBusy() && !preparedPackage) {
      await setChannel("text", false, thread);
      setRequestStatus(characterId, "上一位角色仍在回复，完成后会重新读取当前场景。");
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
function formatMessageTime(timestamp, previousTimestamp = 0, estimated = false) {
  const value = new Date(Number(timestamp) || Date.now());
  const previous = previousTimestamp ? new Date(Number(previousTimestamp)) : null;
  if (previous && value.getTime() - previous.getTime() <= 5 * 60 * 1000 && localDayKey(value) === localDayKey(previous)) return "";
  const now = new Date();
  const dayDiff = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() - new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()) / 86400000);
  const clock = value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
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
  $("load-older-messages")?.addEventListener("click", () => { void loadOlderMessages(); });
}
function renderTimeline({ forceScroll = false, preserveScroll = false } = {}) {
  const thread = currentThread();
  const messages = (thread?.messages || []).filter((message) => message.communicationChannel === "text");
  const timeline = $("timeline");
  const wasNearBottom = timelineNearBottom();
  const previousDistance = timeline.scrollHeight - timeline.scrollTop;
  const previousCount = Number(timeline.dataset.messageCount || 0);
  const older = thread?.hasOlderMessages ? '<button id="load-older-messages" class="secondary-button load-older-messages" type="button">加载更早的 40 条</button>' : "";
  if (!messages.length) {
    timeline.innerHTML = `${older}${timelineTypingMarkup()}<div class="empty-conversation"><p>从一句问候开始吧。历史只保存在此浏览器。</p>${starterPromptsMarkup()}</div>`;
    timeline.dataset.messageCount = "0";
    bindTimelineActions();
    return;
  }
  timeline.innerHTML = older + messages.map((message, index) => {
    const failed = message.status === "failed";
    const presenting = message.role === "assistant" && presentationFor()?.messageId === message.id;
    const tools = failed
      ? `<button type="button" data-retry-message="${escapeHtml(message.id)}"${globalRequestBusy() ? " disabled" : ""}>重试</button>`
      : message.role === "assistant" && !presenting ? `<button type="button" data-feedback-message="${escapeHtml(message.id)}">反馈本条</button>` : "";
    const time = formatMessageTime(message.createdAt, messages[index - 1]?.createdAt || 0, message.createdAtEstimated);
    const avatar = message.role === "user" ? analystAvatarMarkup({ priority: index === messages.length - 1 }) : avatarMarkup(currentCharacter(), { thumbnail: true, priority: index === messages.length - 1 });
    const blocks = message.role === "assistant" ? visibleBlocksFor(message) : message.contentBlocks;
    return `${time ? `<div class="message-time" aria-label="${escapeHtml(time)}">${escapeHtml(time)}</div>` : ""}<article class="message ${message.role} ${message.status}" data-message-id="${escapeHtml(message.id)}"><div class="message-avatar">${avatar}</div><div class="message-body"><span class="meta"><span>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")}</span><span>${failed ? "生成失败" : "文字通讯"}</span></span><div class="message-content-stack">${blocks.map(blockHtml).join("")}</div>${message.role === "assistant" ? rendezvousCardMarkup(message, presenting) : ""}<div class="message-tools">${tools}${usageMarkup(message)}</div></div></article>`;
  }).join("") + timelineTypingMarkup();
  timeline.dataset.messageCount = String(messages.length);
  bindTimelineActions();
  bindAvatarImages(timeline);
  const newMessages = messages.length > previousCount;
  if (!preserveScroll && (forceScroll || wasNearBottom || previousCount === 0)) {
    timeline.scrollTop = timeline.scrollHeight;
    $("new-replies").hidden = true;
  } else if (!preserveScroll) {
    timeline.scrollTop = Math.max(0, timeline.scrollHeight - previousDistance);
    if (newMessages && messages[messages.length - 1]?.role === "assistant") $("new-replies").hidden = false;
  }
  const latestAssistant = [...messages].reverse().find((message) => message.role === "assistant" && message.status === "sent" && !presentationFor()?.messageId);
  if (latestAssistant && timeline.dataset.announcedMessageId !== latestAssistant.id) {
    timeline.dataset.announcedMessageId = latestAssistant.id;
    $("conversation-announcer").textContent = `${currentCharacter()?.display_name || "角色"}的新回复已显示`;
  }
}
function latestInPersonMessage(role = "") {
  return [...(currentThread()?.messages || [])].reverse().find((message) => message.communicationChannel === "in_person" && message.status !== "failed" && (!role || message.role === role)) || null;
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
  return true;
}
function renderTypewriter(text, key) {
  const value = plain(text);
  const speechNode = $("stage-speech");
  if (
    state.typewriter.key === key
    && (state.typewriter.fullText === value || state.typewriter.displayedText === value)
  ) {
    if (state.typewriter.fullText === value && state.typewriter.timer) {
      if (reducedMotion()) {
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
  if (!value || reducedMotion()) {
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
    if (index >= characters.length) {
      finishTypewriter(key, value);
      return;
    }
    state.typewriter.timer = window.setTimeout(
      revealNext,
      step.delayAfter,
    );
  };
  state.typewriter.timer = window.setTimeout(revealNext, plan.initialDelay);
}
function renderStage() {
  const pending = typingStateFor();
  const speechNode = $("stage-speech");
  if (pending?.channel === "in_person" && ["connecting", "typing", "arrival", "segment"].includes(pending.phase)) {
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
    return;
  }
  speechNode.classList.remove("is-typing");
  speechNode.setAttribute("aria-busy", "false");
  const assistantMessage = latestInPersonMessage("assistant");
  const visibleAssistantBlocks = assistantMessage ? visibleBlocksFor(assistantMessage) : [];
  const action = inPersonSurfaceText(visibleAssistantBlocks, "action");
  const completeSpeech = inPersonSurfaceText(visibleAssistantBlocks, "speech");
  $("stage-narration").textContent = action || plain(state.scene?.character_activity);
  const speech = completeSpeech || "场景已经建立。你可以说些什么，也可以只描述一个动作。";
  const speechKey = assistantMessage
    ? completeSpeech
      ? speechTypewriterKey(assistantMessage)
      : `${plain(assistantMessage.characterId)}:${plain(assistantMessage.requestId)}:${plain(assistantMessage.id)}:placeholder`
    : `scene:${state.selected}:${state.scene?.visual_key || "generic"}`;
  renderTypewriter(speech, speechKey);
  $("stage-open-feedback").disabled = !assistantMessage || Boolean(presentationFor()?.messageId === assistantMessage?.id);
  $("stage-open-feedback").dataset.messageId = assistantMessage?.id || "";
}
function renderTranscript() {
  const messages = currentThread()?.messages || [];
  $("transcript-content").innerHTML = messages.length ? messages.map((message) => {
    const blocks = message.role === "assistant" && message.displayBlocks?.length ? message.displayBlocks : message.contentBlocks;
    return `<article class="transcript-entry"><strong>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")} · ${message.communicationChannel === "text" ? "文字通讯" : "面对面"} · ${escapeHtml(new Date(message.createdAt || Date.now()).toLocaleString())}</strong><p>${blocks.map((block) => block.type === "sticker" ? `〔表情〕${escapeHtml(block.caption || "表情")}` : `${block.type === "action" ? "〔动作〕" : ""}${escapeHtml(block.text)}`).join("\n\n")}</p></article>`;
  }).join("") : "<p>当前角色还没有本地记录。</p>";
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
  $("scene-backdrop").src = `/assets/immersive/scenes/${visualKey}.svg`;
  const preload = new Image();
  preload.src = $("scene-backdrop").src;
  $("stage-location").textContent = scene.character_location || "场景尚未建立";
  $("stage-activity").textContent = scene.character_activity || "选择角色后读取当前位置";
  const presenceLabel = scene.character_location ? `去见她 · ${scene.character_location}` : "去见她";
  $("go-in-person-label").textContent = presenceLabel;
  $("go-in-person").setAttribute("aria-label", presenceLabel);
  $("go-in-person").title = presenceLabel;
  const character = currentCharacter();
  if (character?.avatar?.src) {
    const image = new Image();
    image.src = character.avatar.src;
  }
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
  const requestPending = globalRequestBusy();
  $("message-input").disabled = !selected;
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
  $("analyst-action-field").hidden = !inPerson || !state.actionComposerOpen;
  $("toggle-action").setAttribute("aria-expanded", String(inPerson && state.actionComposerOpen));
  $("message-input").placeholder = !selected ? "选择角色后输入消息……" : configured() ? (inPerson ? "说些什么，也可只填写动作……" : "输入文字通讯……") : "可浏览历史；发送前请在设置中配置模型";
  renderSelectedSticker();
}
async function setChannel(channel, persist = true, targetThread = null) {
  const thread = targetThread || currentThread();
  const restoreCollapsedSidebarFocus = channel !== "in_person"
    && $("chat-app").classList.contains("sidebar-collapsed");
  if (thread && thread.characterId === state.selected) await saveDraftNow();
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
  state.actionComposerOpen = false;
  if (channel === "in_person") state.selectedSticker = null;
  $("chat-app").dataset.channel = channel;
  $("text-surface").hidden = channel === "in_person";
  $("in-person-surface").hidden = channel !== "in_person";
  $("chat-app").dataset.channel = channel === "in_person" ? "in_person" : "text";
  syncContactToggleState();
  renderAll();
  restoreDraft();
  updateComposerAvailability();
  if (restoreCollapsedSidebarFocus) window.setTimeout(() => $("open-contacts")?.focus(), 0);
}
function requestHistory(messages, excludedId = "", segmentId = "") {
  return messages.filter((message) => message.id !== excludedId && !["failed", "pending"].includes(message.status) && (!segmentId || message.conversationSegmentId === segmentId)).slice(-24).map((message) => ({
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
function updateInputCount() {
  const count = $("message-input").value.length + ($("action-input").value.length || 0);
  $("input-count").textContent = `${count} / 2000`;
  $("input-count").style.color = count > 2000 ? "#ff9dac" : "";
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
async function consumeChatStream(response, { characterId, requestId, fallbackChannel }) {
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
  const handlePacket = async (packet) => {
    const event = (packet.match(/^event: (.+)$/m) || [])[1];
    const data = (packet.match(/^data: (.+)$/m) || [])[1];
    if (!event || !data) return;
    const payload = JSON.parse(data);
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
      if (ownsTypingState(characterId, requestId)) {
        const accepted = await saveWorldPackage(payload.state_package);
        if (accepted) {
          pendingSceneState = payload.scene_state || pendingSceneState;
          pendingStateEvent = payload.state_event || pendingStateEvent;
        }
      }
    }
    if (event === "done") {
      completed = true;
      usage = payload.usage || null;
      movementStatus = payload.movement_status && typeof payload.movement_status === "object" ? payload.movement_status : null;
      recoveryAction = plain(payload.recovery_action || "none");
      returnedChannel = payload.communication_channel || returnedChannel;
      contentBlocks = normalizeBlocks(payload.content_blocks, returnedChannel, renderBlocksText([...streamedBlocks.values()]));
    }
    if (event === "error") throw new Error(payload.code || "chat_failed");
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const packets = buffer.split("\n\n");
    buffer = packets.pop() || "";
    for (const packet of packets) await handlePacket(packet);
  }
  if (buffer.trim()) await handlePacket(buffer);
  if (!completed) throw new Error("stream_disconnected");
  if (!contentBlocks.length) contentBlocks = normalizeBlocks([], returnedChannel, renderBlocksText([...streamedBlocks.values()]));
  if (!contentBlocks.length) throw new Error("upstream_invalid_response");
  return { contentBlocks, usage, returnedChannel, pendingSceneState, pendingStateEvent, movementStatus, recoveryAction };
}

function recoverableChatError(error) {
  const code = error instanceof Error ? error.message : plain(error);
  return error instanceof TypeError || ["stream_disconnected", "request_in_progress", "provider_network_error", "request_failed"].includes(code);
}

function shouldReuseChatRequest(error) {
  const code = error instanceof Error ? error.message : plain(error);
  return error instanceof TypeError || [
    "stream_disconnected",
    "request_in_progress",
    "provider_network_error",
    "provider_timeout",
    "request_cancelled",
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
  if (plain(candidate.payload.request_id) !== candidateRequestId) return null;
  if (plain(candidate.payload.character_id) !== plain(userMessage.characterId)) return null;
  if (plain(candidate.payload.message) !== plain(userMessage.content)) return null;
  if (plain(candidate.payload.communication_channel) !== plain(userMessage.communicationChannel)) return null;
  if (plain(candidate.payload.movement_location_id) !== plain(userMessage.movementLocationId)) return null;
  if (plain(candidate.payload.provider) !== state.provider || plain(candidate.payload.model) !== state.model) return null;
  return { requestId: candidateRequestId, payload: structuredClone(candidate.payload) };
}

async function runChat(thread, userMessage, { stateRecoveryAttempt = 0 } = {}) {
  let retrySnapshot = stateRecoveryAttempt ? null : compatibleRetrySnapshot(userMessage);
  let requestId = plain(retrySnapshot?.requestId) || id();
  const characterId = thread.characterId;
  const buildRequestSnapshot = () => ({
    request_id: requestId,
    provider: state.provider,
    model: state.model,
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
  if (retrySnapshot && jsonBodyBytes({ ...requestSnapshot, credential: state.credential }) > PUBLIC_REQUEST_LIMIT_BYTES) {
    state.retrySnapshots.delete(userMessage.id);
    retrySnapshot = null;
    requestId = id();
    requestSnapshot = buildRequestSnapshot();
  }
  let boundedRequest;
  try {
    boundedRequest = fitPublicRequestPayload(
      { ...requestSnapshot, credential: state.credential },
      {
        arrays: [{ key: "recent_history", minimum: 0 }],
        texts: ["history_summary"],
        targetBytes: retrySnapshot ? PUBLIC_REQUEST_LIMIT_BYTES : PUBLIC_REQUEST_TARGET_BYTES,
      },
    );
  } catch (error) {
    state.retrySnapshots.delete(userMessage.id);
    userMessage.requestId = requestId;
    userMessage.requestSnapshot = null;
    userMessage.status = "failed";
    userMessage.errorCode = error instanceof Error ? error.message : "request_too_large";
    state.latest.set(characterId, { requestId, errorCode: userMessage.errorCode });
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
  const requestBody = JSON.stringify(boundedRequest);
  const ownsRequest = () => ownsTypingState(characterId, requestId);
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
  userMessage.conversationSegmentId = thread.conversationSegmentId;
  userMessage.status = "pending";
  userMessage.errorCode = "";
  if (state.selected === characterId) renderAll();
  setTypingState({ characterId, requestId, channel: userMessage.communicationChannel, phase: "waiting" });
  setRequestStatus(characterId, "", requestId);
  const timing = { shownAt: 0 };
  const typingGate = (async () => {
    try {
      if (!reducedMotion()) await abortableDelay(requestDelay(requestId, "initial", 1800, 2600), requestController.signal);
    } catch (error) {
      if (error?.name === "AbortError") return;
      throw error;
    }
    if (!ownsRequest()) return;
    timing.shownAt = performance.now();
    updateTypingPhase(characterId, requestId, "typing");
  })();
  userMessage.requestSnapshot = structuredClone(requestSnapshot);
  state.retrySnapshots.set(userMessage.id, { requestId, payload: structuredClone(requestSnapshot) });
  const userPersistence = dbPutThread(thread);
  try {
    let result = null;
    let lastError = null;
    const backoffs = [0, 1000, 2000, 4000];
    for (let attempt = 0; attempt < backoffs.length; attempt += 1) {
      if (attempt > 0) {
        if (!recoverableChatError(lastError) || requestController.signal.aborted) throw lastError;
        setRequestStatus(characterId, `连接中断，正在恢复（${attempt}/3）……`, requestId);
        await abortableDelay(backoffs[attempt], requestController.signal);
      }
      try {
        const response = await fetch(`${apiRoot}/chat/stream`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, signal: requestController.signal, body: requestBody });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          const serverCode = plain(payload?.detail?.code);
          const error = new Error(serverCode || ([502, 503, 504].includes(response.status) ? "provider_network_error" : "chat_failed"));
          throw error;
        }
        result = await consumeChatStream(response, { characterId, requestId, fallbackChannel: userMessage.communicationChannel });
        break;
      } catch (error) {
        lastError = error;
        if (attempt === backoffs.length - 1 || !recoverableChatError(error)) throw error;
      }
    }
    if (!result) throw lastError || new Error("stream_disconnected");
    state.retrySnapshots.delete(userMessage.id);
    userMessage.requestSnapshot = null;
    await userPersistence;
    await typingGate;
    if (timing.shownAt && !reducedMotion()) {
      const visibleFor = performance.now() - timing.shownAt;
      if (visibleFor < 1200) await abortableDelay(1200 - visibleFor, requestController.signal);
    }
    userMessage.status = "sent";
    const assistantMessage = normalizeMessage({ id: id(), characterId, role: "assistant", contentBlocks: result.contentBlocks, communicationChannel: result.returnedChannel, createdAt: Date.now(), requestId, conversationSegmentId: thread.conversationSegmentId, usage: { ...(result.usage || {}), model: result.usage?.model || state.model }, movementStatus: result.movementStatus });
    thread.messages.push(assistantMessage);
    thread.messageCount = Number(thread.messageCount || 0) + 1;
    thread.turnCount += 1;
    thread.continuityDecision = "";
    thread.lastActiveAt = Date.now();
    if (ownsRequest() || !typingStateFor(characterId)) state.latest.set(characterId, { requestId, errorCode: "", usage: result.usage });
    await dbPutThread(thread);
    updateTypingPhase(characterId, requestId, "presenting");
    await presentAssistantTurn(characterId, requestId, assistantMessage);
    if (ownsRequest() && result.pendingSceneState) {
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
      clearTypingState(characterId, requestId);
    }
    scheduleAutoSummary(thread, result.usage);
    renderCharacters();
  } catch (caught) {
    await userPersistence.catch(() => {});
    const error = caught?.name === "AbortError" ? new Error(requestTimedOut ? "provider_timeout" : "request_cancelled") : caught;
    if (stateRecoveryAttempt < 1 && statePackageRecoveryError(error) && !requestController.signal.aborted) {
      cancelPresentationQueue(characterId, requestId);
      state.retrySnapshots.delete(userMessage.id);
      await clearPersistedWorldPackage();
      if (characterId === state.selected) renderScene();
      return await runChat(thread, userMessage, { stateRecoveryAttempt: stateRecoveryAttempt + 1 });
    }
    if (!shouldReuseChatRequest(error)) {
      state.retrySnapshots.delete(userMessage.id);
      userMessage.requestSnapshot = null;
    }
    cancelPresentationQueue(characterId, requestId);
    userMessage.status = "failed";
    userMessage.errorCode = error instanceof Error ? error.message : "chat_failed";
    if (ownsRequest() || !typingStateFor(characterId)) state.latest.set(characterId, { requestId, errorCode: userMessage.errorCode });
    await dbPutThread(thread);
    if (ownsVisibleRequest()) renderAll();
    if (ownsRequest()) {
      setRequestStatus(characterId, displayError(error), requestId);
      clearTypingState(characterId, requestId);
    }
  } finally {
    window.clearTimeout(requestTimeout);
    requestController.abort();
    if (state.chatRequestByCharacter.get(characterId)?.requestId === requestId) state.chatRequestByCharacter.delete(characterId);
    clearTypingState(characterId, requestId);
    updateComposerAvailability();
    void flushDeferredPresenceRefresh();
  }
}
async function submitUserBlocks(blocks, { clearComposer = false, onAccepted = null, movementLocationId = "" } = {}) {
  if (!state.selected || globalRequestBusy()) return false;
  const total = renderBlocksText(blocks).length;
  if (!blocks.length) return false;
  if (total > 2000) {
    showBanner("单轮动作与对白合计不能超过 2,000 字。");
    return false;
  }
  if (!configured()) {
    openSettings("models");
    showError("setup-error", "请先配置当前标签页使用的模型会话。");
    return false;
  }
  const thread = await dbGetThread(state.selected);
  if (!(await ensureContinuityDecision(thread))) return false;
  const userMessage = normalizeMessage({ id: id(), characterId: thread.characterId, role: "user", contentBlocks: blocks, communicationChannel: thread.channel, createdAt: Date.now(), status: "pending", conversationSegmentId: thread.conversationSegmentId, movementLocationId });
  thread.messages.push(userMessage);
  thread.messageCount = Number(thread.messageCount || 0) + 1;
  onAccepted?.();
  if (clearComposer) {
    $("message-input").value = "";
    $("action-input").value = "";
    clearSelectedSticker();
    state.actionComposerOpen = false;
    state.drafts.set(draftKey(thread.characterId, thread.channel, "message"), "");
    state.drafts.set(draftKey(thread.characterId, thread.channel, "action"), "");
    void storePut("app_state", { key: "drafts", values: Object.fromEntries(state.drafts) });
    $("toggle-sticker").setAttribute("aria-expanded", "false");
    updateInputCount();
  }
  await runChat(thread, userMessage);
  return true;
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
  const snapshot = state.retrySnapshots.get(message.id)
    || (message.requestId && message.requestSnapshot ? { requestId: message.requestId, payload: message.requestSnapshot } : null);
  if (snapshot && (
    plain(snapshot.payload?.provider) !== state.provider
    || plain(snapshot.payload?.model) !== state.model
  )) {
    state.retrySnapshots.delete(message.id);
    message.requestId = "";
    message.requestSnapshot = null;
    await dbPutThread(thread);
  }
  await runChat(thread, message);
}
function successfulChatMessages(thread) {
  return (thread.messages || []).filter((message) => message.role === "assistant" && message.status === "sent" && message.source !== "presence_arrival");
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
  if (!state.autoSummaryEnabled || !configured()) return;
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
async function runAutoSummary(thread) {
  if (!thread.summaryRequestId || !state.autoSummaryEnabled || !configured() || state.summaryInFlight.has(thread.characterId) || globalRequestBusy()) return;
  state.summaryInFlight.add(thread.characterId);
  updateComposerAvailability();
  const requestId = thread.summaryRequestId;
  try {
    const turns = thread.messages.filter((message) => message.status === "sent").slice(-24).map((message) => ({
      role: message.role,
      communication_channel: message.communicationChannel,
      content_blocks: wireBlocks(message.contentBlocks),
    }));
    const requestPayload = fitPublicRequestPayload(
      { request_id: requestId, provider: state.provider, credential: state.credential, model: state.model, character_id: thread.characterId, turns, previous_summary: thread.summary || "" },
      { arrays: [{ key: "turns", minimum: 2 }], texts: ["previous_summary"] },
    );
    const payload = await api("/chat/summarize", { method: "POST", body: JSON.stringify(requestPayload) });
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
    // Summary failure never blocks or rewrites the conversation.  Keep the
    // request UUID so the next suitable turn retries idempotently.
    if (thread.characterId === state.selected) setRequestStatus(thread.characterId, "对话已完成；连续性整理稍后重试。");
    if (error?.message === "credential_invalid") clearCredential();
  } finally {
    state.summaryInFlight.delete(thread.characterId);
    updateComposerAvailability();
    void flushDeferredPresenceRefresh();
  }
}

async function transitionPresence(targetChannel, characterId = state.selected, targetThread = null, signal = undefined) {
  const thread = targetThread || state.threads.get(characterId) || currentThread();
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
  if (!state.selected || globalRequestBusy()) return;
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
  await setChannel("in_person", false, thread);
  if (ownsVisibleArrival()) {
    $("presence-arrival-loading").hidden = false;
    $("stage-presence-status").hidden = false;
    $("stage-presence-status").textContent = "正在靠近……";
  }
  updateComposerAvailability();
  try {
    await transitionPresence("in_person", characterId, thread);
    transitionApplied = true;
    await dismissCurrentRendezvous(thread);
    if (!configured()) {
      if (ownsVisibleArrival()) showBanner("场景已更新。配置模型后可以开始对话。");
      return;
    }
    const arrivalPayload = fitPublicRequestPayload(
      { arrival_id: arrivalId, provider: state.provider, credential: state.credential, model: state.model, character_id: characterId, recent_history: requestHistory(thread?.messages || [], "", thread?.conversationSegmentId || ""), history_summary: thread?.continuityDecision === "start_today" ? "" : (thread?.summary || ""), state_package: state.worldPackage || "" },
      { arrays: [{ key: "recent_history", minimum: 0 }], texts: ["history_summary"] },
    );
    const result = await api("/presence/arrival", { method: "POST", body: JSON.stringify(arrivalPayload) });
    if (ownsArrival()) await saveWorldPackage(result.state_package);
    if (ownsArrival()) state.sceneByCharacter.set(characterId, result.scene_state || {});
    if (ownsVisibleArrival()) state.scene = result.scene_state;
    if (thread) thread.channel = "in_person";
    if (thread && result.reaction) {
      arrivalMessage = normalizeMessage({ id: result.reaction.message_id || id(), characterId, role: "assistant", contentBlocks: result.reaction.content_blocks, communicationChannel: "in_person", createdAt: Date.now(), requestId: result.arrival_id, source: "presence_arrival", conversationSegmentId: thread.conversationSegmentId });
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
      if (result.terminal_error) showBanner(arrivalReactionUnavailableMessage(result.terminal_error));
      else if (result.decision === "unnoticed") showBanner("你来到她身边，她暂时没有注意到。位置切换已经完成。");
      else showBanner("她注意到了你的到来。");
      renderAll();
    }
    if (arrivalMessage && ownsArrival()) {
      await presentAssistantTurn(characterId, arrivalId, arrivalMessage);
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
    const minimum = reducedMotion() ? 0 : 900;
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
  if (event.key !== "Tab") return false;
  const controls = [...panel.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter((item) => !item.hidden);
  if (!controls.length) return false;
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  return false;
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
  document.querySelectorAll("[data-settings-tab]").forEach((button) => button.classList.toggle("active", button.dataset.settingsTab === tab));
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== tab; });
  if (tab === "history") storageBytes().then((bytes) => { $("storage-usage").textContent = `当前浏览器记录约占 ${formatBytes(bytes)}。`; });
  const activeThread = currentThread();
  $("auto-summary-enabled").checked = state.autoSummaryEnabled;
  $("history-retention").value = String(state.historyRetentionDays);
  $("summary-last-updated").textContent = activeThread?.summaryUpdatedAt ? `最近更新：${new Date(activeThread.summaryUpdatedAt).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}` : "尚未生成摘要";
  $("provider-select").value = state.provider || $("provider-select").value;
  syncProviderControls($("provider-select").value);
  $("model-id").value = state.model || $("model-id").value;
  refreshCredentialStatus();
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
}
function openFeedback(messageId = "") {
  state.feedbackMessageId = messageId;
  $("feedback-include-context").checked = true;
  renderFeedbackContextPreview();
  $("feedback-dialog").showModal();
}
function feedbackContextSelection() {
  const thread = currentThread() || { messages: [] };
  const target = thread.messages.find((message) => message.id === state.feedbackMessageId) || [...thread.messages].reverse().find((message) => message.role === "assistant") || null;
  const targetIndex = target ? thread.messages.indexOf(target) : thread.messages.length;
  const userMessage = [...thread.messages.slice(0, targetIndex)].reverse().find((message) => message.role === "user") || [...thread.messages].reverse().find((message) => message.role === "user") || {};
  const latest = state.latest.get(state.selected) || {};
  const userBlocks = wireBlocks(normalizeBlocks(userMessage.contentBlocks, userMessage.communicationChannel || thread.channel || "text", userMessage.content || ""));
  const assistantBlocks = wireBlocks(normalizeBlocks(target?.contentBlocks, target?.communicationChannel || thread.channel || "text", target?.content || ""));
  return { thread, target, userMessage, latest, userBlocks, assistantBlocks, chatRequestId: target?.requestId || latest.requestId || userMessage.requestId || null };
}
function renderFeedbackContextPreview() {
  const include = $("feedback-include-context").checked;
  const { target, userMessage, userBlocks, assistantBlocks } = feedbackContextSelection();
  $("feedback-context-preview").hidden = !include;
  if (!include) {
    $("feedback-context").textContent = "不会附带对话、角色、模型或请求诊断，只提交你填写的反馈正文与可选 QQ。";
    return;
  }
  $("feedback-context").textContent = target ? `将附带 ${target.communicationChannel === "text" ? "文字通讯" : "面对面"}中的相关一问一答及请求编号。` : "将附带当前一问一答和请求编号；服务端会按编号关联内部诊断。";
  const preview = [
    userMessage.content || userBlocks.map((block) => block.text || `〔表情〕${block.caption || "表情"}`).join("\n"),
    assistantBlocks.map((block) => block.text || `〔表情〕${block.caption || "表情"}`).join("\n"),
  ].filter(Boolean);
  $("feedback-context-preview-text").textContent = preview.length ? preview.map((text, index) => `${index ? "角色" : "你"}：${text}`).join("\n\n") : "当前没有可附带的对话内容。";
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
  const { thread, target, userMessage, latest, chatRequestId, userBlocks, assistantBlocks } = feedbackContextSelection();
  const includeContext = $("feedback-include-context").checked;
  const assistantSpeech = renderBlocksText(assistantBlocks.filter((block) => block.type !== "action"));
  try {
    const contextual = includeContext ? { chat_request_id: chatRequestId || null, character_id: state.selected || "", provider: state.provider || "", model: state.model || "", user_message: userMessage.content || "", assistant_answer: assistantSpeech, user_content_blocks: userBlocks, assistant_content_blocks: assistantBlocks, error_code: latest.errorCode || userMessage.errorCode || "" } : {};
    const payload = await api("/feedback", { method: "POST", timeoutMs: 20000, body: JSON.stringify({ request_id: id(), body: $("feedback-body").value, qq: $("feedback-qq").value, turnstile_token: await tokenFor("feedback"), include_conversation_context: includeContext, request_stage: target?.communicationChannel || thread.channel || "immersive-web", ui_surface: "immersive-web", ...contextual }) });
    $("feedback-dialog").close();
    $("feedback-form").reset();
    toast(`反馈已提交，编号：${payload.feedback_code}`);
  } catch (error) {
    showError("feedback-error", error);
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
  if (!state.actionComposerOpen) $("action-input").value = "";
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
$("new-replies").onclick = () => {
  $("timeline").scrollTop = $("timeline").scrollHeight;
  $("new-replies").hidden = true;
};
$("timeline").addEventListener("scroll", () => {
  if (timelineNearBottom()) $("new-replies").hidden = true;
}, { passive: true });
$("feedback-include-context").onchange = renderFeedbackContextPreview;
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
$("clear-credential").onclick = () => { clearCredential(); $("api-key").focus(); toast("当前标签页的模型凭证已清除"); };
$("discovered-models").onchange = () => { if ($("discovered-models").value) { $("model-id").value = $("discovered-models").value; state.model = $("discovered-models").value; saveCredential(); } };
$("toggle-advanced-model").onclick = () => {
  const panel = $("advanced-model-panel");
  const expanded = panel.hidden;
  panel.hidden = !expanded;
  $("toggle-advanced-model").setAttribute("aria-expanded", String(expanded));
};
$("provider-select").onchange = () => chooseProvider($("provider-select").value);
$("auto-summary-enabled").onchange = async () => {
  state.autoSummaryEnabled = $("auto-summary-enabled").checked;
  await storePut("app_state", { key: "preferences", autoSummaryEnabled: state.autoSummaryEnabled });
  if (state.autoSummaryEnabled) toast("将在下一轮仍有模型调用余量时整理连续性摘要");
};
$("history-retention").onchange = async () => {
  state.historyRetentionDays = [30, 90].includes(Number($("history-retention").value)) ? Number($("history-retention").value) : 0;
  await saveUiPreferences();
  await pruneExpiredMessages();
  toast(state.historyRetentionDays ? `将自动清理超过 ${state.historyRetentionDays} 天的本地消息` : "已关闭自动清理");
};
document.querySelectorAll("[data-settings-tab]").forEach((button) => { button.onclick = () => openSettings(button.dataset.settingsTab); });
$("delete-character-history").onclick = async () => { if (globalRequestBusy()) return; const characterId = $("history-character").value; await deleteMessagesForCharacter(characterId); await storeDelete("threads", characterId); state.threads.delete(characterId); if (characterId === state.selected) { await dbGetThread(characterId); renderAll(); } openSettings("history"); toast("该角色本地历史已删除"); };
$("clear-all-history").onclick = async () => { if (globalRequestBusy()) return; if (!window.confirm("确定清空全部 Project Snow 本地历史与世界状态吗？")) return; await storeClear("threads"); await storeClear("messages"); await storeClear("app_state"); state.threads.clear(); state.worldPackage = ""; state.drafts.clear(); state.pinnedCharacters.clear(); state.favoriteStickerIds.clear(); state.favoriteStickers.clear(); state.recentStickerIds = []; state.rendezvousDismissals.clear(); if (state.selected) { await dbGetThread(state.selected); await resolvePresence(); } renderAll(); openSettings("history"); toast("全部本地历史已清空"); };
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  if (!button.getAttribute("aria-label")) button.setAttribute("aria-label", "关闭对话框");
  button.onclick = () => $(button.dataset.closeDialog).close();
});
$("sticker-picker").addEventListener("close", () => $("toggle-sticker").setAttribute("aria-expanded", "false"));
window.addEventListener("keydown", (event) => {
  if (trapDrawerFocus(event)) return;
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

async function boot() {
  await openDB();
  await migrateBrowserState();
  await pruneExpiredMessages();
  await loadConfig();
  await showExperienceNoticeIfNeeded();
  await loadCharacters();
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
  renderOnboarding();
  if (!state.storageAvailable) showBanner("浏览器未开放本地存储，本次聊天不会保存；仍可继续使用。 ");
}
boot().catch((error) => {
  $("connection-status").textContent = "连接失败";
  showBanner(displayError(error));
});
