const apiRoot = "/public/v1";
const dbName = "project-snow-public";
const dbVersion = 2;
const SCENE_KEYS = new Set(["generic", "quarters", "lounge", "training", "archive", "canteen", "observation", "medical", "corridor"]);
const state = {
  config: null,
  credential: "",
  credentialExpiresAt: 0,
  provider: "",
  model: "",
  characters: [],
  selected: "",
  threads: new Map(),
  worldPackage: "",
  scene: null,
  sceneByCharacter: new Map(),
  latest: new Map(),
  feedbackMessageId: "",
  arrivalPending: false,
  autoSummaryEnabled: true,
  selectionSequence: 0,
  selectionController: null,
  typewriter: { key: "", timer: 0, fullText: "", displayedText: "" },
  summaryInFlight: new Set(),
};

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
  experience_notice_required: "请先阅读并确认体验说明。",
};

function id() {
  return window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
function plain(value) { return String(value ?? ""); }
function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = plain(value);
  return span.innerHTML;
}
function displayError(error) {
  const code = error instanceof Error ? error.message : plain(error);
  return errorMessages[code] || (/^[a-z][a-z0-9_]*$/.test(code) ? errorMessages.request_failed : code);
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
function reducedMotion() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") headers["Content-Type"] = "application/json";
  const response = await fetch(`${apiRoot}${path}`, { credentials: "same-origin", ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail?.code || "request_failed");
  return payload;
}

function openDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(dbName, dbVersion);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("threads")) db.createObjectStore("threads", { keyPath: "characterId" });
      if (!db.objectStoreNames.contains("app_state")) db.createObjectStore("app_state", { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function storeGet(storeName, key) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const request = db.transaction(storeName, "readonly").objectStore(storeName).get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  });
}
async function storeAll(storeName) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const request = db.transaction(storeName, "readonly").objectStore(storeName).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}
async function storePut(storeName, value) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, "readwrite");
    tx.objectStore(storeName).put(value);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}
async function storeDelete(storeName, key) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, "readwrite");
    tx.objectStore(storeName).delete(key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}
async function storeClear(storeName) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, "readwrite");
    tx.objectStore(storeName).clear();
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

function normalizeBlocks(blocks, channel, fallback = "") {
  const allowed = channel === "text" ? new Set(["message"]) : new Set(["speech", "action"]);
  const normalized = Array.isArray(blocks) ? blocks
    .filter((item) => item && allowed.has(item.type) && plain(item.text).trim())
    .slice(0, 8)
    .map((item) => ({ type: item.type, text: plain(item.text).trim() })) : [];
  if (!normalized.length && plain(fallback).trim()) normalized.push({ type: channel === "text" ? "message" : "speech", text: plain(fallback).trim() });
  return normalized;
}
function renderBlocksText(blocks) { return (blocks || []).map((block) => block.text).filter(Boolean).join("\n"); }
function normalizeMessage(message) {
  const channel = message.communicationChannel || message.communication_channel || "text";
  const blocks = normalizeBlocks(message.contentBlocks || message.content_blocks, channel, message.content || "");
  return {
    id: message.id || id(),
    role: message.role === "assistant" ? "assistant" : "user",
    content: renderBlocksText(blocks),
    contentBlocks: blocks,
    communicationChannel: channel,
    createdAt: Number(message.createdAt || Date.now()),
    status: message.status || "sent",
    requestId: message.requestId || "",
    errorCode: message.errorCode || "",
    source: message.source || "chat",
  };
}
function normalizeThread(record, characterId) {
  return {
    characterId,
    messages: (record?.messages || []).map(normalizeMessage),
    summary: plain(record?.summary),
    channel: record?.channel === "in_person" ? "in_person" : "text",
    turnCount: Number(record?.turnCount || 0),
    summarizedThroughMessageId: plain(record?.summarizedThroughMessageId),
    summarizedThroughTurnCount: Number(record?.summarizedThroughTurnCount || 0),
    summaryRequestId: plain(record?.summaryRequestId),
    summaryCheckpointMessageId: plain(record?.summaryCheckpointMessageId),
    summaryUpdatedAt: Number(record?.summaryUpdatedAt || 0),
    legacyStatePackage: plain(record?.statePackage),
  };
}
async function dbGetThread(characterId) {
  if (state.threads.has(characterId)) return state.threads.get(characterId);
  const thread = normalizeThread(await storeGet("threads", characterId), characterId);
  state.threads.set(characterId, thread);
  return thread;
}
async function dbPutThread(thread) {
  state.threads.set(thread.characterId, thread);
  await storePut("threads", {
    characterId: thread.characterId,
    messages: thread.messages,
    summary: thread.summary,
    channel: thread.channel,
    turnCount: thread.turnCount,
    summarizedThroughMessageId: thread.summarizedThroughMessageId,
    summarizedThroughTurnCount: thread.summarizedThroughTurnCount,
    summaryRequestId: thread.summaryRequestId,
    summaryCheckpointMessageId: thread.summaryCheckpointMessageId,
    summaryUpdatedAt: thread.summaryUpdatedAt,
  });
}
function decodeStatePackage(token) {
  try {
    const encoded = token.split(".", 1)[0].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded + "=".repeat((4 - encoded.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return {}; }
}
async function saveWorldPackage(token) {
  if (!token) return;
  const decoded = decodeStatePackage(token);
  const incomingRevision = Number(decoded.revision || 0);
  const currentRevision = Number(decodeStatePackage(state.worldPackage || "").revision || 0);
  // A slow response from a character the user has already left must not roll
  // the shared world back over a newer signed package. Every server mutation
  // increments revision, so equal revisions are safe to keep as-is.
  if (state.worldPackage && incomingRevision < currentRevision) return;
  state.worldPackage = token;
  await storePut("app_state", { key: "world", statePackage: token, revision: Number(decoded.revision || 0) });
}
async function migrateBrowserState() {
  const preferences = await storeGet("app_state", "preferences");
  state.autoSummaryEnabled = preferences?.autoSummaryEnabled !== false;
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
  const payload = { threads: await storeAll("threads"), appState: await storeAll("app_state") };
  return new Blob([JSON.stringify(payload)]).size;
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
    const expires = new Date(state.credentialExpiresAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
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
  return `project-snow-public:notice:${state.config?.experience_notice_version || "0.8"}`;
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

async function loadConfig() {
  state.config = await api("/config", { headers: {} });
  $("version-badge").textContent = state.config.app_version || "0.8.0";
  $("github-link").href = state.config.source_links.project_snow;
  $("website-github-link").href = state.config.source_links.mywebsite;
  $("releases-link").href = state.config.source_links.releases;
  $("provider-select").innerHTML = state.config.providers.length
    ? state.config.providers.map((provider) => `<option value="${escapeHtml(provider.provider_id)}">${escapeHtml(provider.display_name)}</option>`).join("")
    : '<option value="">暂未开放模型厂商</option>';
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
        $("model-id").value = state.model;
      } else clearCredential();
    } catch { clearCredential(); }
  }
  refreshCredentialStatus();
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
    select.innerHTML = `<option value="">选择已发现模型（也可手工填写）</option>${payload.models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")}`;
    select.hidden = false;
  } catch (error) { showError("setup-error", error); }
}
async function saveModelSession() {
  showError("setup-error", "");
  try {
    const model = $("model-id").value.trim();
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
function avatarMarkup(character, { thumbnail = true, priority = false, className = "" } = {}) {
  const avatar = character?.avatar || null;
  const src = avatar ? (thumbnail ? avatar.thumbnail_src : avatar.src) : "";
  const focus = avatar ? ` style="--portrait-focus-x:${Number(avatar.portrait_focus_x || 50)}%;--portrait-focus-y:${Number(avatar.portrait_focus_y || 50)}%;--portrait-scale:${Number(avatar.portrait_scale || 1)}"` : "";
  const image = src ? `<img src="${escapeHtml(src)}" alt="" loading="${priority ? "eager" : "lazy"}" decoding="async"${priority ? " fetchpriority=\"high\"" : ""} />` : "";
  return `<span class="portrait portrait-text ${className}${src ? " has-image" : ""}"${focus}><span class="portrait-fallback">${escapeHtml(character?.display_name?.slice(0, 1) || "?")}</span>${image}</span>`;
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
}
function renderCharacters() {
  const query = $("character-search").value.trim().toLocaleLowerCase("zh-CN");
  $("character-list").innerHTML = state.characters
    .filter((character) => !query || `${character.display_name} ${(character.aliases || []).join(" ")}`.toLocaleLowerCase("zh-CN").includes(query))
    .map((character) => {
      const thread = state.threads.get(character.character_id);
      const last = [...(thread?.messages || [])].reverse().find((message) => message.status === "sent");
      const summary = last?.content ? last.content.replace(/\s+/g, " ").slice(0, 28) : "尚未开始对话";
      return `<button class="character" role="option" aria-selected="${character.character_id === state.selected}" data-character="${escapeHtml(character.character_id)}">${avatarMarkup(character, { thumbnail: true, priority: character.character_id === state.selected })}<span><strong>${escapeHtml(character.display_name)}</strong><small>${escapeHtml(summary)}</small></span></button>`;
    }).join("");
  bindAvatarImages($("character-list"));
  document.querySelectorAll("[data-character]").forEach((button) => { button.onclick = () => selectCharacter(button.dataset.character); });
}
async function loadCharacters() {
  const payload = await api("/characters", { headers: {} });
  state.characters = payload.characters || [];
  await Promise.all(state.characters.map((character) => dbGetThread(character.character_id)));
  $("history-character").innerHTML = state.characters.map((character) => `<option value="${escapeHtml(character.character_id)}">${escapeHtml(character.display_name)}</option>`).join("");
  renderCharacters();
  if (!state.selected && state.characters[0]) await selectCharacter(state.characters[0].character_id);
}
function sceneKeyForLocation(location) {
  const value = plain(location);
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
async function resolvePresence(characterId = state.selected, signal = undefined) {
  if (!characterId) return null;
  const result = await api("/presence/resolve", { method: "POST", signal, body: JSON.stringify({ request_id: id(), character_id: characterId, state_package: state.worldPackage || "" }) });
  await saveWorldPackage(result.state_package);
  state.sceneByCharacter.set(characterId, result.scene_state || {});
  if (characterId === state.selected) {
    state.scene = result.scene_state;
    renderScene();
  }
  return result;
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
async function selectCharacter(characterId) {
  if (!characterId || characterId === state.selected && !state.selectionController) return;
  const sequence = ++state.selectionSequence;
  state.selectionController?.abort();
  state.selectionController = new AbortController();
  const controller = state.selectionController;
  const previousId = state.selected;
  const previousThread = currentThread();
  const switchingFromStage = Boolean(previousId && previousId !== characterId && previousThread?.channel === "in_person");
  const previousScene = state.scene;
  state.selected = characterId;
  try {
    const thread = await dbGetThread(characterId);
    if (sequence !== state.selectionSequence) return;
    const character = currentCharacter();
    renderCharacters();
    $("active-character").innerHTML = `${avatarMarkup(character, { thumbnail: false, priority: true, className: "large" })}<div><h1>${escapeHtml(character.display_name)}</h1><p>文字通讯</p></div>`;
    fillAvatar($("stage-header-avatar"), character, { thumbnail: true, priority: true });
    fillAvatar($("stage-portrait-avatar"), character, { thumbnail: false, priority: true });
    $("stage-character-name").textContent = character.display_name;
    $("stage-speaker").textContent = character.display_name;
    const cached = state.sceneByCharacter.get(characterId) || cachedSceneForCharacter(characterId);
    if (cached) {
      state.scene = cached;
      renderScene();
    }
    const resolving = resolvePresence(characterId, controller.signal);
    if (switchingFromStage) {
      await runSceneTransition(resolving, "正在前往……", sequence);
    } else {
      await resolving;
    }
    if (sequence !== state.selectionSequence) return;
    const scene = state.scene || {};
    if (switchingFromStage && !scene.co_located) {
      await setChannel("in_person", false);
      window.setTimeout(() => { if (sequence === state.selectionSequence) openPresenceDialog(); }, 0);
    } else {
      await setChannel(thread.channel || "text", false);
    }
    renderAll();
    updateComposerAvailability();
    $("contact-panel").classList.remove("open");
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.selectionSequence) return;
    state.selected = previousId;
    state.scene = previousScene;
    if (previousId) {
      const oldThread = await dbGetThread(previousId);
      await setChannel(oldThread.channel || "text", false);
      renderAll();
    }
    showBanner(displayError(error));
  }
}

function blockHtml(block) {
  const className = block.type === "action" ? "content-action" : block.type === "speech" ? "content-speech" : "content-message";
  return `<div class="${className}">${escapeHtml(block.text)}</div>`;
}
function renderTimeline() {
  const thread = currentThread();
  const messages = (thread?.messages || []).filter((message) => message.communicationChannel === "text");
  if (!messages.length) {
    $("timeline").innerHTML = '<div class="empty-conversation"><p>从一句问候开始吧。历史只保存在此浏览器。</p></div>';
    return;
  }
  $("timeline").innerHTML = messages.map((message) => {
    const failed = message.status === "failed";
    const tools = failed
      ? `<button type="button" data-retry-message="${escapeHtml(message.id)}">重试</button>`
      : message.role === "assistant" ? `<button type="button" data-feedback-message="${escapeHtml(message.id)}">反馈本条</button>` : "";
    return `<article class="message ${message.role} ${message.status}"><span class="meta"><span>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")}</span><span>${failed ? "生成失败" : "文字通讯"}</span></span>${message.contentBlocks.map(blockHtml).join("")}<div class="message-tools">${tools}</div></article>`;
  }).join("");
  document.querySelectorAll("[data-retry-message]").forEach((button) => { button.onclick = () => retryMessage(button.dataset.retryMessage); });
  document.querySelectorAll("[data-feedback-message]").forEach((button) => { button.onclick = () => openFeedback(button.dataset.feedbackMessage); });
  $("timeline").scrollTop = $("timeline").scrollHeight;
}
function latestInPersonMessage(role = "") {
  return [...(currentThread()?.messages || [])].reverse().find((message) => message.communicationChannel === "in_person" && message.status !== "failed" && (!role || message.role === role)) || null;
}
function finishTypewriter() {
  if (!state.typewriter.fullText) return;
  window.clearInterval(state.typewriter.timer);
  state.typewriter.timer = 0;
  $("stage-speech").textContent = state.typewriter.fullText;
  state.typewriter.displayedText = state.typewriter.fullText;
  state.typewriter.fullText = "";
}
function renderTypewriter(text, key) {
  const value = plain(text);
  if (state.typewriter.key === key && (state.typewriter.fullText === value || state.typewriter.displayedText === value)) return;
  window.clearInterval(state.typewriter.timer);
  state.typewriter.key = key;
  state.typewriter.fullText = value;
  state.typewriter.displayedText = "";
  if (!value || reducedMotion()) {
    $("stage-speech").textContent = value;
    state.typewriter.displayedText = value;
    state.typewriter.fullText = "";
    return;
  }
  let index = 0;
  $("stage-speech").textContent = "";
  state.typewriter.timer = window.setInterval(() => {
    index += 1;
    $("stage-speech").textContent = value.slice(0, index);
    if (index >= value.length) {
      window.clearInterval(state.typewriter.timer);
      state.typewriter.timer = 0;
      state.typewriter.displayedText = value;
      state.typewriter.fullText = "";
    }
  }, 24);
}
function renderStage() {
  const assistantMessage = latestInPersonMessage("assistant");
  const latestMessage = latestInPersonMessage();
  const actions = (latestMessage?.contentBlocks || []).filter((block) => block.type === "action").map((block) => block.text);
  const speeches = (assistantMessage?.contentBlocks || []).filter((block) => block.type === "speech").map((block) => block.text);
  $("stage-narration").textContent = actions.join("\n") || plain(state.scene?.character_activity);
  const speech = speeches.join("\n") || "场景已经建立。你可以说些什么，也可以只描述一个动作。";
  renderTypewriter(speech, assistantMessage?.id || `scene:${state.selected}:${state.scene?.visual_key || "generic"}`);
  $("stage-message-feedback").disabled = !assistantMessage;
  $("stage-message-feedback").dataset.messageId = assistantMessage?.id || "";
}
function renderTranscript() {
  const messages = currentThread()?.messages || [];
  $("transcript-content").innerHTML = messages.length ? messages.map((message) => `<article class="transcript-entry"><strong>${message.role === "user" ? "你" : escapeHtml(currentCharacter()?.display_name || "角色")} · ${message.communicationChannel === "text" ? "文字通讯" : "面对面"}</strong><p>${message.contentBlocks.map((block) => `${block.type === "action" ? "〔动作〕" : ""}${block.text}`).join("\n")}</p></article>`).join("") : "<p>当前角色还没有本地记录。</p>";
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
  $("go-in-person-label").textContent = scene.character_location ? `去见她 · ${scene.character_location}` : "去见她";
  const character = currentCharacter();
  if (character?.avatar?.src) {
    const image = new Image();
    image.src = character.avatar.src;
  }
  renderStage();
  renderInfo();
}
function renderAll() { renderTimeline(); renderStage(); renderTranscript(); renderInfo(); }

function updateComposerAvailability() {
  const selected = Boolean(state.selected);
  $("message-input").disabled = !selected;
  $("send-message").disabled = !selected || state.arrivalPending;
  $("message-input").placeholder = !selected ? "选择角色后输入消息……" : configured() ? (currentThread()?.channel === "in_person" ? "说些什么，也可只填写动作……" : "输入文字通讯……") : "可浏览历史；发送前请在设置中配置模型";
}
async function setChannel(channel, persist = true) {
  const thread = currentThread();
  if (thread) {
    thread.channel = channel === "in_person" ? "in_person" : "text";
    if (persist) await dbPutThread(thread);
  }
  $("chat-app").dataset.channel = channel;
  $("text-surface").hidden = channel === "in_person";
  $("in-person-surface").hidden = channel !== "in_person";
  $("analyst-action-field").hidden = true;
  $("toggle-action").setAttribute("aria-expanded", "false");
  renderAll();
  updateComposerAvailability();
}
function requestHistory(messages, excludedId = "") {
  return messages.filter((message) => message.id !== excludedId && !["failed", "pending"].includes(message.status)).slice(-24).map((message) => ({
    role: message.role,
    content: message.content,
    communication_channel: message.communicationChannel,
    content_blocks: message.contentBlocks,
  }));
}
function inputBlocks() {
  const channel = currentThread()?.channel || "text";
  const speech = $("message-input").value.trim();
  const action = $("action-input").value.trim();
  if (channel === "text") return speech ? [{ type: "message", text: speech }] : [];
  return [action ? { type: "action", text: action } : null, speech ? { type: "speech", text: speech } : null].filter(Boolean);
}
function updateInputCount() {
  const count = $("message-input").value.length + ($("action-input").value.length || 0);
  $("input-count").textContent = `${count} / 2000`;
  $("input-count").style.color = count > 2000 ? "#ff9dac" : "";
}
async function runChat(thread, userMessage) {
  const requestId = id();
  // Capture request ownership before any asynchronous work. The user can
  // switch characters while a provider is still generating; the completed
  // turn must be written to its original thread and never overwrite the new
  // character's visible state.
  const characterId = thread.characterId;
  const provider = state.provider;
  const credential = state.credential;
  const model = state.model;
  userMessage.requestId = requestId;
  userMessage.status = "pending";
  userMessage.errorCode = "";
  await dbPutThread(thread);
  renderAll();
  $("send-message").disabled = true;
  $("request-status").textContent = "正在连接……";
  try {
    const response = await fetch(`${apiRoot}/chat/stream`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        provider,
        credential,
        model,
        character_id: characterId,
        message: userMessage.content,
        communication_channel: userMessage.communicationChannel,
        content_blocks: userMessage.contentBlocks,
        recent_history: requestHistory(thread.messages, userMessage.id),
        history_summary: thread.summary || "",
        state_package: state.worldPackage || "",
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload?.detail?.code || "chat_failed");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const streamedBlocks = new Map();
    let contentBlocks = [];
    let degraded = [];
    let returnedChannel = userMessage.communicationChannel;
    const isCurrentCharacter = () => state.selected === characterId;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const packets = buffer.split("\n\n");
      buffer = packets.pop();
      for (const packet of packets) {
        const event = (packet.match(/^event: (.+)$/m) || [])[1];
        const data = (packet.match(/^data: (.+)$/m) || [])[1];
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (event === "meta") {
          if (isCurrentCharacter()) $("request-status").textContent = "她正在回应……";
        }
        if (event === "delta") {
          const blockIndex = Number.isFinite(Number(payload.block_index)) ? Number(payload.block_index) : 0;
          const current = streamedBlocks.get(blockIndex) || { type: payload.block_type || (returnedChannel === "text" ? "message" : "speech"), text: "" };
          current.text += plain(payload.text);
          if (payload.block_type) current.type = payload.block_type;
          streamedBlocks.set(blockIndex, current);
          if (isCurrentCharacter()) $("request-status").textContent = "她正在回应……";
        }
        if (event === "state") await saveWorldPackage(payload.state_package);
        if (event === "done") {
          degraded = payload.degraded_services || [];
          returnedChannel = payload.communication_channel || returnedChannel;
          contentBlocks = normalizeBlocks(payload.content_blocks, returnedChannel, renderBlocksText([...streamedBlocks.values()]));
        }
        if (event === "error") throw new Error(payload.code || "chat_failed");
      }
    }
    if (!contentBlocks.length) contentBlocks = normalizeBlocks([], returnedChannel, renderBlocksText([...streamedBlocks.values()]));
    if (!contentBlocks.length) throw new Error("upstream_invalid_response");
    userMessage.status = "sent";
    thread.messages.push(normalizeMessage({ id: id(), role: "assistant", contentBlocks, communicationChannel: returnedChannel, createdAt: Date.now(), requestId }));
    thread.turnCount += 1;
    state.latest.set(characterId, { requestId, errorCode: "", degraded });
    await dbPutThread(thread);
    if (state.selected === characterId) {
      renderAll();
      $("request-status").textContent = degraded.length ? `已完成，降级服务：${degraded.join("、")}` : "已完成";
    }
    scheduleAutoSummary(thread);
  } catch (error) {
    userMessage.status = "failed";
    userMessage.errorCode = error instanceof Error ? error.message : "chat_failed";
    state.latest.set(characterId, { requestId, errorCode: userMessage.errorCode, degraded: [] });
    await dbPutThread(thread);
    if (state.selected === characterId) {
      renderAll();
      $("request-status").textContent = displayError(error);
    }
  } finally { updateComposerAvailability(); }
}
async function sendMessage(event) {
  event.preventDefault();
  if (!state.selected) return;
  const blocks = inputBlocks();
  const total = renderBlocksText(blocks).length;
  if (!blocks.length) return;
  if (total > 2000) return showBanner("单轮动作与对白合计不能超过 2,000 字。");
  if (!configured()) {
    openSettings("models");
    return showError("setup-error", "请先配置当前标签页使用的模型会话。");
  }
  const thread = await dbGetThread(state.selected);
  const userMessage = normalizeMessage({ id: id(), role: "user", contentBlocks: blocks, communicationChannel: thread.channel, createdAt: Date.now(), status: "pending" });
  thread.messages.push(userMessage);
  $("message-input").value = "";
  $("action-input").value = "";
  updateInputCount();
  await runChat(thread, userMessage);
}
async function retryMessage(messageId) {
  if (!configured()) return openSettings("models");
  const thread = currentThread();
  const message = thread?.messages.find((item) => item.id === messageId && item.role === "user");
  if (!message || message.status !== "failed") return;
  await runChat(thread, message);
}
function successfulChatMessages(thread) {
  return (thread.messages || []).filter((message) => message.role === "assistant" && message.status === "sent" && message.source !== "presence_arrival");
}
function scheduleAutoSummary(thread) {
  if (!state.autoSummaryEnabled || !configured()) return;
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
  if (!thread.summaryRequestId || !state.autoSummaryEnabled || !configured() || state.summaryInFlight.has(thread.characterId)) return;
  state.summaryInFlight.add(thread.characterId);
  const requestId = thread.summaryRequestId;
  try {
    const turns = thread.messages.filter((message) => message.status === "sent").slice(-24).map((message) => ({
      role: message.role,
      content: message.content,
      communication_channel: message.communicationChannel,
      content_blocks: message.contentBlocks,
    }));
    const payload = await api("/chat/summarize", { method: "POST", body: JSON.stringify({ request_id: requestId, provider: state.provider, credential: state.credential, model: state.model, character_id: thread.characterId, turns, previous_summary: thread.summary || "" }) });
    thread.summary = payload.summary || thread.summary;
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
    if (thread.characterId === state.selected) $("request-status").textContent = "对话已完成；连续性整理稍后重试。";
    if (error?.message === "credential_invalid") clearCredential();
  } finally {
    state.summaryInFlight.delete(thread.characterId);
  }
}

async function transitionPresence(targetChannel) {
  const result = await api("/presence/transition", { method: "POST", body: JSON.stringify({ request_id: id(), character_id: state.selected, target_channel: targetChannel, action: targetChannel === "in_person" ? "join_character" : "open_communicator", state_package: state.worldPackage || "" }) });
  await saveWorldPackage(result.state_package);
  state.scene = result.scene_state;
  await setChannel(targetChannel);
  renderScene();
  return result;
}
async function arriveInPerson() {
  if (!state.selected || state.arrivalPending) return;
  state.arrivalPending = true;
  const started = performance.now();
  const characterId = state.selected;
  $("presence-arrival-loading").hidden = false;
  $("stage-presence-status").hidden = false;
  $("stage-presence-status").textContent = "正在前往……";
  updateComposerAvailability();
  const thread = currentThread();
  try {
    if (!configured()) {
      await transitionPresence("in_person");
      showBanner("场景已更新。配置模型后可以开始对话。");
      return;
    }
    const result = await api("/presence/arrival", { method: "POST", body: JSON.stringify({ arrival_id: id(), provider: state.provider, credential: state.credential, model: state.model, character_id: state.selected, recent_history: requestHistory(thread.messages), history_summary: thread.summary || "", state_package: state.worldPackage || "" }) });
    await saveWorldPackage(result.state_package);
    state.scene = result.scene_state;
    if (result.reaction) {
      thread.messages.push(normalizeMessage({ id: result.reaction.message_id || id(), role: "assistant", contentBlocks: result.reaction.content_blocks, communicationChannel: "in_person", createdAt: Date.now(), requestId: result.arrival_id, source: "presence_arrival" }));
      await dbPutThread(thread);
    }
    // Populate the arrival turn before exposing the stage. Otherwise
    // setChannel() can make the scene visible for one paint while the
    // reaction is still being persisted, producing an empty dialogue box.
    await setChannel("in_person");
    if (result.terminal_error) showBanner(displayError(new Error(result.terminal_error)));
    else if (result.decision === "unnoticed") showBanner("你来到她身边，她暂时没有注意到。位置切换已经完成。");
    else showBanner("她注意到了你的到来。");
    renderAll();
  } catch (error) {
    if ((error instanceof Error ? error.message : "") === "credential_invalid") {
      clearCredential();
      await transitionPresence("in_person");
      showBanner("模型凭证已过期，位置切换已完成；重新配置后才能生成到场反应。");
    } else showBanner(displayError(error));
  } finally {
    const minimum = reducedMotion() ? 0 : 900;
    const elapsed = performance.now() - started;
    if (elapsed < minimum) await delay(minimum - elapsed);
    state.arrivalPending = false;
    $("presence-arrival-loading").hidden = true;
    $("stage-presence-status").hidden = true;
    updateComposerAvailability();
    if (characterId === state.selected) renderStage();
  }
}
async function openPresenceDialog() {
  if (!state.selected) return;
  try { await resolvePresence(); } catch (error) { return showBanner(displayError(error)); }
  $("presence-dialog-location").textContent = state.scene?.character_location || "当前位置尚未建立";
  $("presence-dialog-activity").textContent = state.scene?.character_activity || "场景建立后即可前往。";
  $("presence-dialog").showModal();
}

function openDrawer(name) {
  for (const panel of [$("info-panel"), $("transcript-panel")]) panel.classList.toggle("open", panel.id === name);
  $("drawer-scrim").hidden = false;
  renderInfo();
  renderTranscript();
}
function closeDrawers() {
  $("info-panel").classList.remove("open");
  $("transcript-panel").classList.remove("open");
  $("drawer-scrim").hidden = true;
}
function openSettings(tab = "models") {
  document.querySelectorAll("[data-settings-tab]").forEach((button) => button.classList.toggle("active", button.dataset.settingsTab === tab));
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== tab; });
  if (tab === "history") storageBytes().then((bytes) => { $("storage-usage").textContent = `当前浏览器记录约占 ${formatBytes(bytes)}。`; });
  const activeThread = currentThread();
  $("auto-summary-enabled").checked = state.autoSummaryEnabled;
  $("summary-last-updated").textContent = activeThread?.summaryUpdatedAt ? `最近更新：${new Date(activeThread.summaryUpdatedAt).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}` : "尚未生成摘要";
  $("provider-select").value = state.provider || $("provider-select").value;
  $("model-id").value = state.model || $("model-id").value;
  refreshCredentialStatus();
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
}
function openFeedback(messageId = "") {
  state.feedbackMessageId = messageId;
  const message = currentThread()?.messages.find((item) => item.id === messageId);
  $("feedback-context").textContent = message ? `将附带 ${message.communicationChannel === "text" ? "文字通讯" : "面对面"}中的本条回复及其请求编号。` : "将附带当前一问一答和不含密钥的请求诊断。";
  $("feedback-dialog").showModal();
}
async function submitFeedback(event) {
  event.preventDefault();
  showError("feedback-error", "");
  const thread = currentThread() || { messages: [] };
  const target = thread.messages.find((message) => message.id === state.feedbackMessageId) || [...thread.messages].reverse().find((message) => message.role === "assistant") || null;
  const targetIndex = target ? thread.messages.indexOf(target) : thread.messages.length;
  const userMessage = [...thread.messages.slice(0, targetIndex)].reverse().find((message) => message.role === "user") || [...thread.messages].reverse().find((message) => message.role === "user") || {};
  const latest = state.latest.get(state.selected) || {};
  const chatRequestId = target?.requestId || latest.requestId || userMessage.requestId || null;
  const userBlocks = normalizeBlocks(userMessage.contentBlocks, userMessage.communicationChannel || thread.channel || "text", userMessage.content || "");
  const assistantBlocks = normalizeBlocks(target?.contentBlocks, target?.communicationChannel || thread.channel || "text", target?.content || "");
  const assistantSpeech = renderBlocksText(assistantBlocks.filter((block) => block.type !== "action"));
  try {
    const payload = await api("/feedback", { method: "POST", body: JSON.stringify({ request_id: id(), chat_request_id: chatRequestId || null, body: $("feedback-body").value, qq: $("feedback-qq").value, turnstile_token: await tokenFor("feedback"), character_id: state.selected || "", provider: state.provider || "", model: state.model || "", user_message: userMessage.content || "", assistant_answer: assistantSpeech, user_content_blocks: userBlocks, assistant_content_blocks: assistantBlocks, request_stage: target?.communicationChannel || currentThread()?.channel || "immersive-web", error_code: latest.errorCode || userMessage.errorCode || "", degraded_services: latest.degraded || [], ui_surface: "immersive-web" }) });
    $("feedback-dialog").close();
    $("feedback-form").reset();
    toast(`反馈已提交，编号：${payload.feedback_code}`);
  } catch (error) { showError("feedback-error", error); }
}

$("character-search").oninput = renderCharacters;
$("composer").onsubmit = sendMessage;
$("message-input").oninput = updateInputCount;
$("action-input").oninput = updateInputCount;
$("message-input").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); } };
$("toggle-action").onclick = () => { const hidden = !$("analyst-action-field").hidden; $("analyst-action-field").hidden = hidden; $("toggle-action").setAttribute("aria-expanded", String(!hidden)); if (hidden) $("action-input").value = ""; updateInputCount(); };
$("go-in-person").onclick = openPresenceDialog;
$("confirm-presence-transition").onclick = async () => { $("presence-dialog").close(); await arriveInPerson(); };
$("stay-on-communicator").onclick = async () => {
  $("presence-dialog").close();
  try { await transitionPresence("text"); } catch (error) { showBanner(displayError(error)); }
};
$("open-communicator").onclick = async () => { try { await transitionPresence("text"); } catch (error) { showBanner(displayError(error)); } };
$("open-contacts").onclick = $("open-stage-contacts").onclick = () => $("contact-panel").classList.add("open");
$("close-contacts").onclick = () => $("contact-panel").classList.remove("open");
$("open-info").onclick = () => openDrawer("info-panel");
$("open-transcript").onclick = () => openDrawer("transcript-panel");
$("close-info").onclick = $("close-transcript").onclick = $("drawer-scrim").onclick = closeDrawers;
$("toggle-stage-ui").onclick = () => { $("in-person-surface").classList.add("ui-hidden"); $("restore-stage-ui").hidden = false; };
$("restore-stage-ui").onclick = () => { $("in-person-surface").classList.remove("ui-hidden"); $("restore-stage-ui").hidden = true; };
$("stage-message-feedback").onclick = () => openFeedback($("stage-message-feedback").dataset.messageId || "");
$("open-global-feedback").onclick = $("floating-feedback").onclick = () => openFeedback();
$("feedback-form").onsubmit = submitFeedback;
$("open-settings").onclick = () => openSettings("models");
$("header-config-model").onclick = () => openSettings("models");
$("stage-open-settings").onclick = () => { $("stage-menu").open = false; openSettings("models"); };
$("stage-open-feedback").onclick = () => { $("stage-menu").open = false; openFeedback(); };
$("discover-models").onclick = discoverModels;
$("save-model").onclick = saveModelSession;
$("clear-credential").onclick = () => { clearCredential(); $("api-key").focus(); toast("当前标签页的模型凭证已清除"); };
$("discovered-models").onchange = () => { if ($("discovered-models").value) { $("model-id").value = $("discovered-models").value; state.model = $("discovered-models").value; saveCredential(); } };
$("provider-select").onchange = () => { if (state.provider && state.provider !== $("provider-select").value) clearCredential(); };
$("auto-summary-enabled").onchange = async () => {
  state.autoSummaryEnabled = $("auto-summary-enabled").checked;
  await storePut("app_state", { key: "preferences", autoSummaryEnabled: state.autoSummaryEnabled });
  if (state.autoSummaryEnabled) scheduleAutoSummary(currentThread() || { messages: [], summarizedThroughTurnCount: 0 });
};
document.querySelectorAll("[data-settings-tab]").forEach((button) => { button.onclick = () => openSettings(button.dataset.settingsTab); });
$("delete-character-history").onclick = async () => { const characterId = $("history-character").value; await storeDelete("threads", characterId); state.threads.delete(characterId); if (characterId === state.selected) { await dbGetThread(characterId); renderAll(); } openSettings("history"); toast("该角色本地历史已删除"); };
$("clear-all-history").onclick = async () => { if (!window.confirm("确定清空全部 Project Snow 本地历史与世界状态吗？")) return; await storeClear("threads"); await storeClear("app_state"); state.threads.clear(); state.worldPackage = ""; if (state.selected) { await dbGetThread(state.selected); await resolvePresence(); } renderAll(); openSettings("history"); toast("全部本地历史已清空"); };
document.querySelectorAll("[data-close-dialog]").forEach((button) => { button.onclick = () => $(button.dataset.closeDialog).close(); });
window.addEventListener("keydown", (event) => {
  if (event.key.toLocaleLowerCase() === "h" && currentThread()?.channel === "in_person" && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    const hidden = $("in-person-surface").classList.toggle("ui-hidden");
    $("restore-stage-ui").hidden = !hidden;
  }
  if (event.code === "Space" && currentThread()?.channel === "in_person" && !["INPUT", "TEXTAREA", "BUTTON"].includes(document.activeElement?.tagName)) {
    event.preventDefault();
    finishTypewriter();
  }
});
$("stage-dialogue").addEventListener("click", finishTypewriter);

async function boot() {
  await openDB();
  await migrateBrowserState();
  await loadConfig();
  await showExperienceNoticeIfNeeded();
  await loadCharacters();
  $("connection-status").textContent = "服务已连接";
  $("auto-summary-enabled").checked = state.autoSummaryEnabled;
  updateComposerAvailability();
}
boot().catch((error) => {
  $("connection-status").textContent = "连接失败";
  showBanner(displayError(error));
});
