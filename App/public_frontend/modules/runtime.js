// src/transport.ts
function createHttpClient(root, fetcher = fetch) {
  return {
    async request(path, options = {}) {
      const { timeoutMs = 2e4, signal: callerSignal, ...fetchOptions } = options;
      const headers = new Headers(fetchOptions.headers);
      if (fetchOptions.method && fetchOptions.method !== "GET" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const controller = new AbortController();
      const relayAbort = () => controller.abort();
      if (callerSignal?.aborted) controller.abort();
      else callerSignal?.addEventListener("abort", relayAbort, { once: true });
      const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        const response = await fetcher(`${root}${path}`, {
          credentials: "same-origin",
          ...fetchOptions,
          headers,
          signal: controller.signal
        });
        let payload = {};
        if (response.status !== 204) {
          try {
            payload = await response.json();
          } catch (error) {
            if (controller.signal.aborted) throw error;
            const failure = new Error(response.ok ? "invalid_response" : "request_failed");
            failure.errorStage = "transport";
            failure.httpStatus = response.status;
            throw failure;
          }
        }
        if (!response.ok) {
          const detail = payload?.detail;
          const failure = new Error(typeof detail?.code === "string" ? detail.code : "request_failed");
          failure.requestId = typeof detail?.request_id === "string" ? detail.request_id : "";
          failure.errorStage = typeof detail?.stage === "string" ? detail.stage : "";
          failure.retryable = typeof detail?.retryable === "boolean" ? detail.retryable : null;
          const retryAfter = Number(detail?.retry_after_seconds);
          failure.retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter > 0 ? Math.min(300, Math.floor(retryAfter)) : 0;
          failure.httpStatus = response.status;
          throw failure;
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("invalid_response");
        return payload;
      } catch (error) {
        if (controller.signal.aborted && !callerSignal?.aborted && timeoutMs > 0) {
          const failure = new Error("request_timeout");
          failure.errorStage = "transport";
          throw failure;
        }
        throw error;
      } finally {
        if (timeout !== null) clearTimeout(timeout);
        callerSignal?.removeEventListener("abort", relayAbort);
      }
    }
  };
}

// src/stage.ts
var STAGE_STATES = ["neutral", "gentle_smile", "happy", "amused", "teasing", "relieved", "serious", "focused", "thinking", "confused", "skeptical", "concerned", "surprised", "embarrassed", "sad", "disappointed", "annoyed", "angry"];
var STAGE_MOTIONS = ["none", "lean_in", "tremble", "recoil", "startle"];
function record(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function text(value) {
  return typeof value === "string" && value.trim().length > 0;
}
function assetUrl(value) {
  return typeof value === "string" && /^\/assets\/stage\/[a-zA-Z0-9_./-]+$/.test(value) && !value.includes("..");
}
function hash(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}
function validateStageRelease(value, expectedIds) {
  if (!record(value) || value.schema !== "project-snow-stage-1" || !text(value.version) || !Array.isArray(value.characters) || value.characters.length !== 22) throw new Error("stage_manifest_invalid");
  const ids = /* @__PURE__ */ new Set();
  for (const character of value.characters) {
    if (!record(character) || typeof character.character_id !== "string" || !/^[0-9a-f]{12}$/.test(character.character_id) || ids.has(character.character_id) || !record(character.states) || !character.states.neutral || !Array.isArray(character.motions) || !character.motions.includes("none") || character.motions.some((motion) => !STAGE_MOTIONS.includes(motion))) throw new Error("stage_manifest_invalid");
    ids.add(character.character_id);
    for (const [state, asset] of Object.entries(character.states)) {
      if (!STAGE_STATES.includes(state) || !record(asset) || !assetUrl(asset.url) || !hash(asset.sha256) || !["image/png", "image/webp"].includes(String(asset.media_type)) || !record(asset.source) || !text(asset.source.kind) || !text(asset.source.reference) || !record(asset.approval) || asset.approval.status !== "approved" || !text(asset.approval.approved_by) || !text(asset.approval.evidence) || !text(asset.approval.approved_at) || !Number.isFinite(Date.parse(asset.approval.approved_at))) throw new Error("stage_manifest_invalid");
    }
  }
  if (expectedIds && (expectedIds.length !== 22 || expectedIds.some((id) => !ids.has(id)))) throw new Error("stage_roster_mismatch");
  return value;
}
async function verifiedBytes(url, expectedHash, maximum) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12e3);
  try {
    const response = await fetch(url, { signal: controller.signal, credentials: "omit", redirect: "error", cache: "force-cache" });
    if (!response.ok || Number(response.headers.get("content-length")) > maximum || !response.body) throw new Error("stage_asset_unavailable");
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    for (; ; ) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) {
        await reader.cancel();
        throw new Error("stage_asset_too_large");
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (digest !== expectedHash) throw new Error("stage_asset_hash_mismatch");
    return bytes.buffer;
  } finally {
    clearTimeout(timer);
  }
}
async function loadStageRelease(config, expectedIds) {
  if (!record(config) || config.enabled !== true) return null;
  if (!assetUrl(config.manifest_url) || !config.manifest_url.endsWith(".json") || !hash(config.sha256)) throw new Error("stage_config_invalid");
  const bytes = await verifiedBytes(config.manifest_url, config.sha256, 2 * 1024 * 1024);
  return validateStageRelease(JSON.parse(new TextDecoder().decode(bytes)), expectedIds);
}
var images = /* @__PURE__ */ new Map();
function verifiedStageImage(asset) {
  const key = `${asset.url}:${asset.sha256}`;
  const cached = images.get(key);
  if (cached) return cached;
  const result = verifiedBytes(asset.url, asset.sha256, 8 * 1024 * 1024).then((bytes) => URL.createObjectURL(new Blob([bytes], { type: asset.media_type })));
  images.set(key, result);
  void result.catch(() => {
    if (images.get(key) === result) images.delete(key);
  });
  while (images.size > 32) {
    const oldestKey = images.keys().next().value;
    const oldest = images.get(oldestKey);
    images.delete(oldestKey);
    void oldest.then((url) => URL.revokeObjectURL(url), () => void 0);
  }
  return result;
}

// src/persistence.ts
var HISTORY_DATABASE_VERSION = 4;
async function writeDraftsDatabase(db, values, revision) {
  const tx = db.transaction("app_state", "readwrite");
  const done = transactionDone(tx);
  const store = tx.objectStore("app_state");
  store.put({ key: "drafts", values, revision });
  store.put({ key: "drafts_recovery_v1", values, revision });
  await done;
}
function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", () => resolve(), { once: true });
    transaction.addEventListener("error", () => reject(transaction.error || new Error("indexeddb_transaction_failed")), { once: true });
    transaction.addEventListener("abort", () => reject(transaction.error || new Error("indexeddb_transaction_aborted")), { once: true });
  });
}
async function pruneHistoryDatabase(db, cutoff) {
  const counts = /* @__PURE__ */ new Map();
  const tx = db.transaction(["threads", "messages"], "readwrite");
  const done = transactionDone(tx);
  const cursorRequest = tx.objectStore("messages").openCursor();
  cursorRequest.onsuccess = () => {
    const cursor = cursorRequest.result;
    if (cursor) {
      if (Number(cursor.value.createdAt || 0) < cutoff) cursor.delete();
      else counts.set(cursor.value.characterId, (counts.get(cursor.value.characterId) || 0) + 1);
      cursor.continue();
    } else {
      const threads = tx.objectStore("threads").openCursor();
      threads.onsuccess = () => {
        const item = threads.result;
        if (!item) return;
        item.update({ ...item.value, messageCount: counts.get(item.value.characterId) || 0 });
        item.continue();
      };
    }
  };
  await done;
  return counts;
}
async function mergeHistoryDatabase(db, backup, normalizeMessage) {
  const tx = db.transaction(["threads", "messages", "app_state"], "readwrite");
  const done = transactionDone(tx);
  try {
    const threads = tx.objectStore("threads");
    const messages = tx.objectStore("messages");
    const appState = tx.objectStore("app_state");
    for (const raw of backup.threads) {
      const existing = threads.get(String(raw.characterId));
      existing.onsuccess = () => {
        if (!existing.result) threads.put({ ...raw, summaryRequestId: "", summaryCheckpointMessageId: "" });
      };
    }
    for (const raw of backup.messages) {
      const message = normalizeMessage(raw);
      message.requestSnapshot = null;
      if (message.status === "pending") {
        message.status = "failed";
        message.errorCode = "stream_disconnected";
      }
      const existing = messages.get(String(message.id));
      existing.onsuccess = () => {
        if (!existing.result) messages.put(message);
      };
    }
    for (const entry of backup.appState) {
      const existing = appState.get(String(entry.key));
      existing.onsuccess = () => {
        if (entry.key === "drafts") appState.put({ key: "drafts", values: { ...entry.values || {}, ...existing.result?.values || {} } });
        else if (!existing.result) appState.put(entry);
      };
    }
  } catch (error) {
    tx.abort();
    await done.catch(() => void 0);
    throw error;
  }
  await done;
}
async function clearHistoryDatabase(db, writerLease) {
  const tx = db.transaction(["threads", "messages", "app_state"], "readwrite");
  const done = transactionDone(tx);
  for (const name of ["threads", "messages", "app_state"]) tx.objectStore(name).clear();
  if (writerLease) tx.objectStore("app_state").put(writerLease);
  await done;
}

// src/view.ts
var MAX_VISIBLE_MESSAGES = 200;
function historyWindow(messages, endId = "", limit = MAX_VISIBLE_MESSAGES) {
  const anchor = endId ? messages.findIndex((message) => message.id === endId) : -1;
  const end = anchor < 0 ? messages.length : anchor + 1;
  const start = Math.max(0, end - Math.min(MAX_VISIBLE_MESSAGES, Math.max(1, limit)));
  return { messages: messages.slice(start, end), start, end, hasNewer: end < messages.length };
}
var renderedNodes = /* @__PURE__ */ new WeakMap();
var renderedMarkup = /* @__PURE__ */ new WeakMap();
function reconcileMarkup(root, markup) {
  const previousMarkup = renderedMarkup.get(root);
  if (previousMarkup?.markup === markup && previousMarkup.children.length === root.childNodes.length && previousMarkup.children.every((node, index) => root.childNodes[index] === node)) return;
  const template = root.ownerDocument.createElement("template");
  template.innerHTML = markup;
  const previous = renderedNodes.get(root) ?? /* @__PURE__ */ new Map();
  const next = /* @__PURE__ */ new Map();
  let position = root.firstChild;
  for (const candidate of Array.from(template.content.children)) {
    const key = candidate.getAttribute("data-message-id") || candidate.getAttribute("data-ui-key") || candidate.id;
    const candidateMarkup = candidate.outerHTML;
    const cached = key ? previous.get(key) : void 0;
    let node = cached?.markup === candidateMarkup && cached.node.parentNode === root ? cached.node : candidate;
    if (cached && node === candidate && cached.node.parentNode === root && candidate.hasAttribute("data-message-id")) {
      const previousTemplate = root.ownerDocument.createElement("template");
      previousTemplate.innerHTML = cached.markup;
      const prior = previousTemplate.content.querySelector(".message-content-stack");
      const live = cached.node.querySelector(".message-content-stack");
      const incoming = candidate.querySelector(".message-content-stack");
      if (prior && live && incoming && cached.node.querySelector(".message-body")?.childElementCount === candidate.querySelector(".message-body")?.childElementCount) {
        const oldMarkup = Array.from(prior.children).map((item) => item.outerHTML);
        const existing = Array.from(live.children);
        const incomingChildren = Array.from(incoming.children);
        incomingChildren.forEach((item, index) => {
          if (existing[index] && oldMarkup[index] === item.outerHTML) return;
          if (existing[index]) existing[index].replaceWith(item);
          else live.append(item);
        });
        for (let index = incomingChildren.length; index < existing.length; index += 1) existing[index]?.remove();
        cached.node.setAttribute("class", candidate.getAttribute("class") || "");
        for (const selector of [".meta", ".message-tools", ".rendezvous-card"]) {
          const nextPart = candidate.querySelector(selector), previousPart = cached.node.querySelector(selector);
          if (nextPart && previousPart && nextPart.outerHTML !== previousPart.outerHTML) previousPart.replaceWith(nextPart);
        }
        node = cached.node;
      }
    }
    if (node !== position) root.insertBefore(node, position);
    position = node.nextSibling;
    if (key) next.set(key, { node, markup: candidateMarkup });
  }
  while (position) {
    const unwanted = position;
    position = position.nextSibling;
    unwanted.remove();
  }
  renderedNodes.set(root, next);
  renderedMarkup.set(root, { markup, children: Array.from(root.childNodes) });
}
function focusWithin(event, panel) {
  if (event.key !== "Tab") return false;
  const controls = Array.from(panel.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter((item) => !item.hidden && !item.closest("[hidden], [inert]") && item.getClientRects().length > 0);
  if (!controls.length) return false;
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

// src/presentation.ts
var PRESENTATION_TIMING = Object.freeze({ feedbackMin: 250, feedbackMax: 400, firstReply: 600, segmentMin: 200, segmentMax: 450, segmentBudget: 1200, mode: 360 });
function remainingNaturalWait(startedAt, now, reduced = false) {
  return reduced ? 0 : Math.max(0, PRESENTATION_TIMING.firstReply - Math.max(0, now - startedAt));
}
function segmentWait(textLength, spent, reduced = false) {
  return reduced ? 0 : Math.max(0, Math.min(PRESENTATION_TIMING.segmentBudget - spent, Math.max(PRESENTATION_TIMING.segmentMin, Math.min(PRESENTATION_TIMING.segmentMax, 200 + textLength * 3))));
}
var CommittedReply = class {
  constructor(requestId, characterId) {
    this.requestId = requestId;
    this.characterId = characterId;
  }
  requestId;
  characterId;
  events = [];
  accept(value) {
    const event = value;
    if (!event || event.request_id !== this.requestId || event.character_id !== this.characterId || !Number.isSafeInteger(event.sequence) || event.sequence < 0 || typeof event.block_id !== "string" || !event.block_id || !event.block || !["message", "speech"].includes(event.block.type) || typeof event.block.text !== "string" || !event.block.text.trim()) throw new Error("stream_commit_invalid");
    const previous = this.events.find((item) => item.block_id === event.block_id || item.sequence === event.sequence);
    if (previous) {
      if (previous.block_id !== event.block_id || previous.sequence !== event.sequence || previous.block.type !== event.block.type || previous.block.text !== event.block.text) throw new Error("stream_commit_conflict");
      return false;
    }
    const last = this.events.at(-1);
    if (last ? event.sequence !== last.sequence + 1 : event.sequence !== 0) throw new Error("stream_commit_sequence");
    this.events.push({ ...event, block: { ...event.block } });
    return true;
  }
  get blocks() {
    return this.events.map((item) => ({ ...item.block }));
  }
  get snapshot() {
    return this.events.map((item) => ({ ...item, block: { ...item.block } }));
  }
  verifyFinal(blocks) {
    for (let index = 0; index < this.events.length; index += 1) {
      const expected = this.events[index].block;
      if (blocks[index]?.type !== expected.type || blocks[index]?.text !== expected.text) throw new Error("stream_commit_conflict");
    }
  }
};
var MotionScope = class {
  animations = /* @__PURE__ */ new Set();
  cleanups = /* @__PURE__ */ new Set();
  cancel() {
    for (const animation of this.animations) animation.cancel();
    this.animations.clear();
    for (const cleanup of this.cleanups) cleanup();
    this.cleanups.clear();
  }
  play(node, frames, options) {
    if (typeof node.animate !== "function") return Promise.resolve();
    const animation = node.animate(frames, options);
    this.animations.add(animation);
    return animation.finished.catch(() => {
    }).then(() => {
      this.animations.delete(animation);
      animation.cancel();
    });
  }
  cleanup(callback) {
    this.cleanups.add(callback);
  }
};

// src/performance.ts
var SERVER_TIMING_KEYS = Object.freeze([
  "queue_ms",
  "context_build_ms",
  "retrieval_ms",
  "prompt_build_ms",
  "hybrid_total",
  "initial_model_ms",
  "provider_headers_ms",
  "provider_first_content_ms",
  "provider_first_event_ms",
  "guard_rewrite_ms",
  "empty_recovery_ms",
  "validation_ms",
  "validation_local_ms",
  "validation_total_ms",
  "first_verified_block_ms",
  "durable_commit_ms",
  "first_committed_block_ms",
  "total"
]);
function anonymousServerTimings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const source = value;
  const result = {};
  for (const key of SERVER_TIMING_KEYS) {
    const item = source[key];
    if (typeof item === "number" && Number.isFinite(item) && item >= 0 && item <= 36e5) {
      result[key] = Math.trunc(item);
    }
  }
  return result;
}

// src/runtime.ts
function createSafeStorage(resolve) {
  const memory = /* @__PURE__ */ new Map();
  return {
    getItem(key) {
      if (memory.has(key)) return memory.get(key) ?? null;
      try {
        return resolve().getItem(key);
      } catch {
        return null;
      }
    },
    setItem(key, value) {
      try {
        resolve().setItem(key, String(value));
        memory.delete(key);
      } catch {
        memory.set(key, String(value));
      }
    },
    removeItem(key) {
      try {
        resolve().removeItem(key);
        memory.delete(key);
      } catch {
        memory.set(key, null);
      }
    }
  };
}
function compareMessageCursor(a, b) {
  return Number(a.createdAt) - Number(b.createdAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}
function beforeMessageCursor(message, before) {
  return typeof before === "number" ? message.createdAt < before : compareMessageCursor(message, before) < 0;
}
function buildIdentity(info) {
  return [info.app_version || "", info.revision || ""].join(":");
}
function portableCopy(value) {
  if (Array.isArray(value)) return value.map(portableCopy);
  if (!value || typeof value !== "object") return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (/^(credential|api_?key|authorization|cookie|state_?package|legacyStatePackage)$/i.test(key)) continue;
    if (["__proto__", "constructor", "prototype"].includes(key)) continue;
    out[key] = portableCopy(item);
  }
  return out;
}
function validateHistoryBackup(value) {
  if (!value || typeof value !== "object") throw new Error("backup_invalid");
  const data = value;
  if (data.schema !== "project-snow-history-1" || data.databaseVersion !== 4 || !Array.isArray(data.threads) || !Array.isArray(data.messages) || !Array.isArray(data.appState)) throw new Error("backup_invalid");
  if (data.messages.length > 1e5 || data.threads.length > 1e3 || data.appState.length > 20) throw new Error("backup_too_large");
  const characters = /* @__PURE__ */ new Set();
  for (const thread of data.threads) {
    if (!thread || typeof thread.characterId !== "string" || !thread.characterId || characters.has(thread.characterId)) throw new Error("backup_invalid");
    characters.add(thread.characterId);
  }
  const ids = /* @__PURE__ */ new Set();
  for (const message of data.messages) {
    if (!message || typeof message.id !== "string" || !message.id || ids.has(message.id) || typeof message.characterId !== "string" || !characters.has(message.characterId) || !Number.isFinite(message.createdAt) || Number(message.createdAt) <= 0 || !["assistant", "user"].includes(String(message.role))) throw new Error("backup_invalid");
    ids.add(message.id);
  }
  if (data.appState.some((item) => !item || !["drafts", "preferences", "ui_preferences"].includes(String(item.key)))) throw new Error("backup_invalid");
  return portableCopy(data);
}
export {
  CommittedReply,
  HISTORY_DATABASE_VERSION,
  MAX_VISIBLE_MESSAGES,
  MotionScope,
  PRESENTATION_TIMING,
  SERVER_TIMING_KEYS,
  STAGE_MOTIONS,
  STAGE_STATES,
  anonymousServerTimings,
  beforeMessageCursor,
  buildIdentity,
  clearHistoryDatabase,
  compareMessageCursor,
  createHttpClient,
  createSafeStorage,
  focusWithin,
  historyWindow,
  loadStageRelease,
  mergeHistoryDatabase,
  portableCopy,
  pruneHistoryDatabase,
  reconcileMarkup,
  remainingNaturalWait,
  segmentWait,
  transactionDone,
  validateHistoryBackup,
  validateStageRelease,
  verifiedStageImage,
  writeDraftsDatabase
};
