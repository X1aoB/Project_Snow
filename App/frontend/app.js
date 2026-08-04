"use strict";

const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderIcons() {
  if (window.lucide?.createIcons) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
}

async function api(path, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const raw = response.status === 204 ? "" : await response.text();
    let payload = null;
    try { payload = raw ? JSON.parse(raw) : null; } catch (_) { payload = raw; }
    if (!response.ok) {
      const error = new Error(
        typeof payload?.detail === "string"
          ? payload.detail
          : payload?.detail?.message || payload?.message || raw || `HTTP ${response.status}`
      );
      error.status = response.status;
      error.detail = payload?.detail || payload;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("等待回复超时，可以使用原消息重试。重试不会重复写入已完成的回答。");
      timeoutError.code = "timeout";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

const state = {
  clientVersion: "preview-0.2.2",
  registryVersion: "",
  enabled: false,
  characters: [],
  characterMap: new Map(),
  selectedCharacterId: "",
  // v2 deliberately starts the redesigned client in immersive + text.  The
  // old key was written by the evidence-workbench preview and could leave a
  // fresh character chat in assistant mode after a client upgrade.
  mode: localStorage.getItem("project_snow:mode:v2") || "immersive",
  worldSessionId: "",
  threads: new Map(),
  feedbackCategories: [],
  feedbackTarget: null,
  infoResult: null,
  search: "",
  revealTimers: new Map(),
};

const MODE_LABELS = { immersive: "沉浸式", assistant: "助手" };
const CHANNEL_LABELS = { in_person: "面对面", text: "文字通讯" };

function storageGet(key, fallback = "") {
  try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* local storage is optional */ }
}

function newClientMessageId() {
  if (window.crypto?.randomUUID) return `client_${window.crypto.randomUUID()}`;
  return `client_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function channelPreference(characterId) {
  // New conversations start as text messages.  An explicitly stored choice
  // (including an older face-to-face session) still wins, so existing history
  // is not silently rewritten.
  const value = storageGet(`project_snow:channel:${characterId}`, "text");
  return value === "text" ? "text" : "in_person";
}

function inputKindPreference(characterId) {
  return storageGet(`project_snow:input-kind:${characterId}`, "speech") === "action"
    ? "action"
    : "speech";
}

function draftKey(characterId, mode, channel, inputKind) {
  return `project_snow:draft:${characterId}:${mode}:${channel}:${inputKind}`;
}

function getThread(characterId) {
  if (!state.threads.has(characterId)) {
    const character = state.characterMap.get(characterId);
    const summary = character?.conversation || {};
    state.threads.set(characterId, {
      characterId,
      conversationId: summary.conversation_id || "",
      sessionId: summary.session_id || "",
      worldSessionId: summary.world_session_id || "",
      channel: summary.communication_channel || channelPreference(characterId),
      inputKind: inputKindPreference(characterId),
      messages: [],
      questions: [],
      loaded: false,
      loading: false,
      loadingOlder: false,
      nextBefore: null,
      pending: null,
      retry: null,
      conflict: null,
      error: "",
      latestResult: null,
    });
  }
  return state.threads.get(characterId);
}

function currentCharacter() {
  return state.characterMap.get(state.selectedCharacterId) || null;
}

function currentThread() {
  return state.selectedCharacterId ? getThread(state.selectedCharacterId) : null;
}

function currentInputKind() {
  const thread = currentThread();
  if (!thread || thread.channel === "text") return "message";
  return thread.inputKind === "action" ? "action" : "speech";
}

function updateInputKindControl() {
  const thread = currentThread();
  const control = byId("analyst-input-kind");
  const input = byId("message-input");
  const inPerson = Boolean(thread && thread.channel === "in_person");
  control.hidden = !inPerson;
  const kind = currentInputKind();
  control.querySelectorAll("[data-input-kind]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.inputKind === kind ? "true" : "false");
  });
  input.classList.toggle("action-input", kind === "action");
  input.placeholder = kind === "action" ? "描述你的动作或神态…" : "输入消息…";
  input.setAttribute("aria-label", kind === "action" ? "输入面对面动作或神态" : "输入消息");
}

function avatarMarkup(character, size = "") {
  const source = character?.avatar?.src || "";
  const fallback = character?.avatar?.fallback || character?.character_name?.slice(0, 1) || "?";
  return `<div class="avatar ${size}">${source ? `<img src="${escapeHtml(source)}" alt="" /><span hidden>${escapeHtml(fallback)}</span>` : `<span>${escapeHtml(fallback)}</span>`}</div>`;
}

function bindAvatarFallbacks(container = document) {
  container.querySelectorAll(".avatar img").forEach((image) => {
    if (image.dataset.fallbackBound) return;
    image.dataset.fallbackBound = "true";
    image.addEventListener("error", () => {
      image.hidden = true;
      if (image.nextElementSibling) image.nextElementSibling.hidden = false;
    });
  });
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 3200);
}

function setConnection(online, detail = "") {
  const target = byId("connection-status");
  target.className = `connection-status ${online ? "online" : "offline"}`;
  target.textContent = online ? (detail || "本地服务已连接") : (detail || "本地服务未连接");
}

function renderCharacterList() {
  const target = byId("character-list");
  const query = state.search.trim().toLocaleLowerCase("zh-CN");
  const characters = state.characters.filter((character) => {
    if (!query) return true;
    return [character.character_name, character.source_name, ...(character.aliases || [])]
      .some((name) => String(name || "").toLocaleLowerCase("zh-CN").includes(query));
  });
  target.innerHTML = characters.map((character) => {
    const thread = state.threads.get(character.character_id);
    const conversation = thread || character.conversation || {};
    const latest = thread?.messages?.slice(-1)[0];
    const preview = latest?.text || conversation.last_message || "开始对话";
    const updated = latest?.created_at || conversation.updated_at;
    const level = character.coverage?.level === "limited" ? "limited" : "";
    return `<button type="button" class="character-item" role="option" data-character-id="${escapeHtml(character.character_id)}" aria-selected="${character.character_id === state.selectedCharacterId}">
      ${avatarMarkup(character)}
      <span class="character-copy"><span class="character-line"><span class="character-name">${escapeHtml(character.character_name)}</span><span class="character-time">${escapeHtml(formatTime(updated))}</span></span><span class="character-preview">${escapeHtml(preview)}</span></span>
      <span class="coverage-dot ${level}" title="${escapeHtml(character.coverage?.label || "资料覆盖状态未知")}"></span>
    </button>`;
  }).join("") || '<div class="empty-conversation"><p>没有匹配的角色。</p></div>';
  bindAvatarFallbacks(target);
}

function updateControls() {
  document.querySelectorAll("#mode-control [data-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.mode === state.mode ? "true" : "false");
  });
  const channel = currentThread()?.channel || "text";
  document.querySelectorAll("#channel-control [data-channel]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.channel === channel ? "true" : "false");
  });
  updateInputKindControl();
}

function renderHeader() {
  const character = currentCharacter();
  const thread = currentThread();
  const target = byId("active-character");
  if (!character) {
    target.innerHTML = `${avatarMarkup(null, "large")}<div><h1>选择角色</h1><p>从左侧开始一段对话</p></div>`;
  } else {
    const coverage = character.coverage?.label || "资料覆盖状态未知";
    target.innerHTML = `${avatarMarkup(character, "large")}<div><h1>${escapeHtml(character.character_name)}</h1><p>${escapeHtml(coverage)} · ${escapeHtml(CHANNEL_LABELS[thread.channel])}</p></div>`;
  }
  bindAvatarFallbacks(target);
  updateControls();
}

function normalizeHistoryMessage(item) {
  return {
    id: item.message_id,
    role: item.role,
    mode: item.mode || "immersive",
    channel: item.communication_channel || "in_person",
    text: item.text || "",
    blocks: item.content_blocks || [],
    result: item.response || null,
    clientMessageId: item.client_message_id || "",
    created_at: item.created_at || "",
    status: "sent",
  };
}

async function loadQuestions(thread) {
  try {
    const result = await api(`/api/v1/mvp/questions?character_id=${encodeURIComponent(thread.characterId)}`);
    thread.questions = (result.questions || []).slice(0, 4);
  } catch (_) {
    thread.questions = [];
  }
}

async function loadConversation(characterId, { older = false } = {}) {
  const thread = getThread(characterId);
  if (older && (!thread.nextBefore || thread.loadingOlder)) return;
  if (!older && (thread.loaded || thread.loading)) return;
  if (older) thread.loadingOlder = true;
  else thread.loading = true;
  if (characterId === state.selectedCharacterId) renderTimeline(false);
  try {
    const params = new URLSearchParams({ limit: "50" });
    if (thread.sessionId) params.set("session_id", thread.sessionId);
    if (older && thread.nextBefore) params.set("before", String(thread.nextBefore));
    const result = await api(`/api/v1/mvp/conversations/${encodeURIComponent(characterId)}?${params}`);
    const messages = (result.messages || []).map(normalizeHistoryMessage);
    if (older) {
      const existing = new Set(thread.messages.map((item) => item.id));
      thread.messages = [...messages.filter((item) => !existing.has(item.id)), ...thread.messages];
    } else {
      thread.messages = messages;
      thread.loaded = true;
      const conversation = result.conversation || {};
      thread.conversationId = conversation.conversation_id || thread.conversationId;
      thread.sessionId = conversation.session_id || thread.sessionId;
      thread.worldSessionId = conversation.world_session_id || thread.worldSessionId;
      thread.channel = conversation.communication_channel || thread.channel;
      if (thread.worldSessionId) state.worldSessionId = thread.worldSessionId;
      const latestAssistant = [...thread.messages].reverse().find((item) => item.role === "assistant");
      thread.latestResult = latestAssistant?.result || null;
      state.infoResult = thread.latestResult;
      await loadQuestions(thread);
    }
    thread.nextBefore = result.next_before;
  } catch (error) {
    thread.error = `历史记录读取失败：${error.message}`;
  } finally {
    thread.loading = false;
    thread.loadingOlder = false;
    if (characterId === state.selectedCharacterId) {
      renderCharacterList();
      renderHeader();
      renderTimeline(!older);
      renderInfo();
    }
  }
}

function messageBlocks(message) {
  if (Array.isArray(message.blocks) && message.blocks.length) return message.blocks;
  return [{ type: message.channel === "text" ? "message" : "speech", text: message.text || "" }];
}

function messageHtml(message, character) {
  const modeLabel = MODE_LABELS[message.mode] || message.mode;
  const channelLabel = CHANNEL_LABELS[message.channel] || message.channel;
  const statusClass = message.status === "failed" ? " failed" : message.status === "sending" ? " sending" : "";
  const label = message.role === "user" ? "分析员" : character.character_name;
  if (message.role === "user") {
    const blocks = messageBlocks(message);
    const isActionOnly = blocks.length > 0 && blocks.every((block) => block.type === "action");
    const kindLabel = isActionOnly ? '<span>动作</span>' : "";
    const status = message.status === "sending" && message.channel === "text"
      ? '<span class="message-status">发送中…</span>'
      : message.status === "failed"
        ? '<span class="message-status error">发送失败</span>'
        : message.status === "awaiting_choice"
          ? '<span class="message-status">等待选择交流方式</span>'
          : "";
    const retry = message.status === "failed"
      ? `<button type="button" class="retry-button" data-retry-message="${escapeHtml(message.id)}">重试</button>`
      : "";
    const renderedBlocks = blocks.map((block) => block.type === "action"
      ? `<div class="message-action analyst-action">${escapeHtml(block.text || "")}</div>`
      : `<div class="message-bubble">${escapeHtml(block.text || "")}</div>`
    ).join("");
    return `<article class="message user ${escapeHtml(message.channel)}${isActionOnly ? " analyst-action-message" : ""}${statusClass}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta"><span>${escapeHtml(label)}</span><span>${escapeHtml(modeLabel)}</span><span>${escapeHtml(channelLabel)}</span>${kindLabel}${status}</div>${renderedBlocks}${retry}</article>`;
  }
  let revealOffset = 0;
  const freshText = Boolean(message.fresh && message.channel === "text");
  const blocks = messageBlocks(message).map((block) => {
    const text = String(block.text || "");
    const delay = revealOffset;
    // A short, deterministic pause between text bubbles makes multi-part
    // replies read like messages rather than one dumped JSON payload.
    revealOffset += Math.min(900, Math.max(140, text.length * 16));
    const revealClass = freshText ? " message-block-reveal" : "";
    const revealStyle = freshText ? ` style="animation-delay:${delay}ms"` : "";
    return block.type === "action"
      ? `<div class="message-action${revealClass}"${revealStyle}>${escapeHtml(text)}</div>`
      : `<div class="message-bubble${revealClass}"${revealStyle}>${escapeHtml(text)}</div>`;
  }).join("");
  return `<article class="message assistant ${escapeHtml(message.channel)}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta"><span>${escapeHtml(label)}</span><span>${escapeHtml(modeLabel)}</span><span>${escapeHtml(channelLabel)}</span></div>${blocks}<div class="message-actions"><button type="button" data-message-info="${escapeHtml(message.id)}">查看依据</button><button type="button" data-message-feedback="${escapeHtml(message.id)}">反馈</button></div></article>`;
}

function renderTimeline(scrollToBottom = false) {
  const target = byId("timeline");
  const character = currentCharacter();
  const thread = currentThread();
  if (!character || !thread) {
    target.innerHTML = '<div class="empty-conversation"><p>选择一位角色开始聊天。</p></div>';
    return;
  }
  if (thread.loading && !thread.loaded) {
    target.innerHTML = '<div class="empty-conversation"><p>正在读取本地会话…</p></div>';
    return;
  }
  const html = [];
  if (thread.nextBefore) html.push(`<button class="load-older" type="button" data-load-older>${thread.loadingOlder ? "正在读取…" : "加载更早消息"}</button>`);
  if (!thread.messages.length && !thread.pending) {
    const questions = thread.questions.map((item) => `<button type="button" class="retry-button" data-suggestion="${escapeHtml(item.text)}">${escapeHtml(item.text)}</button>`).join("");
    html.push(`<div class="empty-conversation"><div><strong>和${escapeHtml(character.character_name)}开始一段对话</strong><p>当前为${escapeHtml(MODE_LABELS[state.mode])} · ${escapeHtml(CHANNEL_LABELS[thread.channel])}</p>${questions ? `<div class="presence-actions">${questions}</div>` : ""}</div></div>`);
  } else {
    let previousMode = null;
    let previousChannel = null;
    thread.messages.forEach((message) => {
      if (previousMode && message.mode !== previousMode) {
        html.push(`<div class="timeline-divider"><span>切换为${escapeHtml(MODE_LABELS[message.mode] || message.mode)}</span></div>`);
      } else if (previousChannel && message.channel !== previousChannel) {
        html.push(`<div class="timeline-divider"><span>改用${escapeHtml(CHANNEL_LABELS[message.channel] || message.channel)}</span></div>`);
      }
      html.push(messageHtml(message, character));
      previousMode = message.mode;
      previousChannel = message.channel;
    });
  }
  if (thread.pending?.sending && thread.pending.channel === "text") {
    html.push(`<article class="message assistant ${escapeHtml(thread.pending.channel)}"><div class="message-meta"><span>${escapeHtml(character.character_name)}</span><span>${escapeHtml(CHANNEL_LABELS[thread.pending.channel])}</span></div><div class="typing" aria-label="正在输入"><span></span><span></span><span></span></div></article>`);
  }
  if (thread.conflict) {
    const detail = thread.conflict;
    html.push(`<div class="presence-choice"><p>${escapeHtml(detail.message || "当前地点不支持面对面交谈。")}</p><div class="presence-actions"><button type="button" data-presence="join_character">去找她</button><button type="button" data-presence="switch_to_text">使用文字通讯</button></div></div>`);
  }
  if (thread.error) html.push(`<div class="presence-choice"><p>${escapeHtml(thread.error)}</p></div>`);
  target.innerHTML = html.join("");
  renderIcons();
  if (scrollToBottom) requestAnimationFrame(() => { target.scrollTop = target.scrollHeight; });
}

function restoreDraft() {
  const input = byId("message-input");
  const thread = currentThread();
  const kind = currentInputKind();
  input.value = state.selectedCharacterId && thread
    ? storageGet(draftKey(state.selectedCharacterId, state.mode, thread.channel, kind), "")
    : "";
  updateInputKindControl();
  resizeComposer();
}

function saveDraft() {
  const thread = currentThread();
  if (!state.selectedCharacterId || !thread) return;
  storageSet(
    draftKey(state.selectedCharacterId, state.mode, thread.channel, currentInputKind()),
    byId("message-input").value,
  );
}

function resizeComposer() {
  const input = byId("message-input");
  input.rows = 1;
  const computed = window.getComputedStyle(input);
  const lineHeight = Number.parseFloat(computed.lineHeight) || 24;
  const verticalPadding = Number.parseFloat(computed.paddingTop) + Number.parseFloat(computed.paddingBottom);
  input.rows = Math.min(5, Math.max(1, Math.ceil((input.scrollHeight - verticalPadding) / lineHeight)));
}

function updateRequestStatus() {
  const thread = currentThread();
  const target = byId("request-status");
  target.className = "request-status";
  if (!thread) {
    target.textContent = "";
  } else if (thread.pending?.sending && thread.pending.channel === "text") {
    target.textContent = `${currentCharacter().character_name}正在回复。你仍可编辑下一条消息。`;
  } else if (thread.pending?.sending) {
    // Face-to-face replies should not look like a network message exchange.
    target.textContent = "";
  } else if (thread.conflict) {
    target.textContent = "请选择见面或改用文字通讯。";
  } else if (thread.error) {
    target.textContent = thread.error;
    target.classList.add("error");
  } else {
    target.textContent = "";
  }
  byId("send-message").disabled = !state.selectedCharacterId || Boolean(thread?.pending);
}

async function selectCharacter(characterId) {
  if (!state.characterMap.has(characterId)) return;
  saveDraft();
  state.selectedCharacterId = characterId;
  storageSet("project_snow:selected_character", characterId);
  const thread = getThread(characterId);
  if (!state.worldSessionId && thread.worldSessionId) state.worldSessionId = thread.worldSessionId;
  renderCharacterList();
  renderHeader();
  restoreDraft();
  renderTimeline(false);
  renderInfo();
  updateRequestStatus();
  closeDrawers();
  byId("message-input").focus();
  await loadConversation(characterId);
}

function setMode(mode) {
  if (!MODE_LABELS[mode] || mode === state.mode) return;
  saveDraft();
  state.mode = mode;
  storageSet("project_snow:mode:v2", mode);
  updateControls();
  restoreDraft();
  renderTimeline(false);
  updateRequestStatus();
  byId("message-input").focus();
}

function setChannel(channel) {
  const thread = currentThread();
  if (!thread || !CHANNEL_LABELS[channel] || thread.channel === channel) return;
  saveDraft();
  thread.channel = channel;
  storageSet(`project_snow:channel:${thread.characterId}`, channel);
  renderHeader();
  restoreDraft();
  renderTimeline(false);
  renderInfo();
  byId("message-input").focus();
}

function setInputKind(kind) {
  const thread = currentThread();
  if (!thread || thread.channel !== "in_person" || !["speech", "action"].includes(kind)) return;
  if (thread.inputKind === kind) return;
  saveDraft();
  thread.inputKind = kind;
  storageSet(`project_snow:input-kind:${thread.characterId}`, kind);
  restoreDraft();
  byId("message-input").focus();
}

function optimisticUserMessage(pending) {
  return {
    id: pending.userMessageId,
    role: "user",
    mode: pending.mode,
    channel: pending.channel,
    text: pending.message,
    blocks: pending.blocks || [{ type: pending.channel === "text" ? "message" : "speech", text: pending.message }],
    result: null,
    clientMessageId: pending.clientMessageId,
    created_at: new Date().toISOString(),
    status: pending.sending ? "sending" : "sent",
  };
}

async function queueMessage() {
  const character = currentCharacter();
  const thread = currentThread();
  const input = byId("message-input");
  const message = input.value.trim();
  if (!character || !thread || !message || thread.pending) return;
  const pending = {
    message,
    clientMessageId: newClientMessageId(),
    userMessageId: `local_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    mode: state.mode,
    channel: thread.channel,
    blocks: [{ type: currentInputKind(), text: message }],
    sending: false,
  };
  thread.pending = pending;
  thread.retry = null;
  thread.conflict = null;
  thread.error = "";
  thread.messages.push(optimisticUserMessage(pending));
  input.value = "";
  saveDraft();
  resizeComposer();
  input.focus();
  await dispatchPending();
}

async function dispatchPending({ channel = null, presenceAction = null } = {}) {
  const thread = currentThread();
  if (!thread?.pending || thread.pending.sending) return;
  const pending = thread.pending;
  pending.channel = channel || pending.channel || thread.channel;
  pending.sending = true;
  const userMessage = thread.messages.find((item) => item.id === pending.userMessageId);
  if (userMessage) {
    userMessage.status = "sending";
    userMessage.channel = pending.channel;
  }
  thread.error = "";
  thread.conflict = null;
  renderTimeline(true);
  updateRequestStatus();
  try {
    const result = await api("/api/v1/mvp/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: thread.characterId,
        message: pending.message,
        session_id: thread.sessionId || null,
        world_session_id: state.worldSessionId || thread.worldSessionId || null,
        mode: pending.mode,
        communication_channel: pending.channel,
        presence_action: presenceAction,
        client_message_id: pending.clientMessageId,
        analyst_content_blocks: pending.blocks,
      }),
    }, 135000);
    if (userMessage) userMessage.status = "sent";
    thread.pending = null;
    thread.retry = null;
    thread.conflict = null;
    thread.sessionId = result.session_id || thread.sessionId;
    thread.conversationId = result.conversation_id || thread.conversationId;
    thread.worldSessionId = result.world_session_id || thread.worldSessionId;
    state.worldSessionId = thread.worldSessionId || state.worldSessionId;
    const resultingChannel = result.channel_transition?.to || result.communication_channel || pending.channel;
    if (thread.channel === pending.channel) thread.channel = resultingChannel;
    storageSet(`project_snow:channel:${thread.characterId}`, thread.channel);
    const assistantMessage = {
      id: result.message_id,
      role: "assistant",
      mode: result.mode || pending.mode,
      channel: result.communication_channel || pending.channel,
      text: result.answer || "",
      blocks: result.content_blocks || [],
      result,
      clientMessageId: pending.clientMessageId,
      created_at: new Date().toISOString(),
      status: "sent",
      fresh: pending.channel === "text" && (result.content_blocks || []).length > 1,
    };
    if (!thread.messages.some((item) => item.id === assistantMessage.id)) thread.messages.push(assistantMessage);
    if (assistantMessage.fresh) {
      const totalDelay = messageBlocks(assistantMessage).reduce(
        (sum, block) => sum + Math.min(900, Math.max(140, String(block.text || "").length * 16)),
        0,
      );
      const oldTimer = state.revealTimers.get(assistantMessage.id);
      if (oldTimer) window.clearTimeout(oldTimer);
      state.revealTimers.set(assistantMessage.id, window.setTimeout(() => {
        assistantMessage.fresh = false;
        state.revealTimers.delete(assistantMessage.id);
        if (state.selectedCharacterId === thread.characterId) renderTimeline(false);
      }, totalDelay + 220));
    }
    thread.latestResult = result;
    state.infoResult = result;
    const character = state.characterMap.get(thread.characterId);
    character.conversation = {
      conversation_id: thread.conversationId,
      session_id: thread.sessionId,
      world_session_id: thread.worldSessionId,
      communication_channel: resultingChannel,
      last_message: result.answer || pending.message,
      last_role: "assistant",
      updated_at: assistantMessage.created_at,
    };
  } catch (error) {
    pending.sending = false;
    if (userMessage) userMessage.status = "failed";
    if (error.status === 409 && error.detail?.code === "communication_context_conflict") {
      thread.conflict = error.detail;
      if (userMessage) userMessage.status = "awaiting_choice";
    } else {
      thread.pending = null;
      thread.retry = { ...pending, sending: false };
      thread.error = error.message;
    }
  }
  renderCharacterList();
  renderHeader();
  renderTimeline(true);
  renderInfo();
  updateRequestStatus();
  byId("message-input").focus();
}

function retryMessage(messageId) {
  const thread = currentThread();
  if (!thread?.retry || thread.pending || thread.retry.userMessageId !== messageId) return;
  thread.pending = { ...thread.retry, sending: false };
  thread.retry = null;
  dispatchPending();
}

function presenceChoice(action) {
  const thread = currentThread();
  if (!thread?.pending) return;
  if (action === "switch_to_text") {
    thread.channel = "text";
    storageSet(`project_snow:channel:${thread.characterId}`, "text");
    dispatchPending({ channel: "text" });
  } else if (action === "join_character") {
    dispatchPending({ channel: "in_person", presenceAction: "join_character" });
  }
}

function renderInfo() {
  const target = byId("info-content");
  const character = currentCharacter();
  const thread = currentThread();
  if (!character || !thread) {
    target.innerHTML = '<div class="info-section"><p>选择角色后查看会话信息。</p></div>';
    return;
  }
  const result = state.infoResult || thread.latestResult || {};
  const coverage = result.coverage || character.coverage || {};
  const scene = result.scene_state || {};
  const style = result.style_context || {};
  const citations = result.citations || [];
  const sceneText = scene.location_visibility === "visible_for_current_turn"
    ? (scene.co_located ? `双方同处：${scene.character_location || "未定位"}` : `分析员：${scene.analyst_location || "未定位"}；角色：${scene.character_location || "未定位"}`)
    : "当前位置未在本轮对话中公开";
  let styleText = "角色本体设定优先";
  if (style.status === "active") styleText = style.kind === "costume" ? `时装：${style.costume_name || "已识别"}` : `装甲：${style.armor_name || "已识别"}`;
  else if (style.status === "ambiguous") styleText = "检测到多个语境，本轮未自动启用";
  target.innerHTML = `
    <section class="info-section"><h3>资料覆盖</h3><p>${escapeHtml(coverage.label || "资料覆盖状态未知")}</p><div class="info-metrics"><div class="info-metric"><strong>${Number(coverage.direct_document_count || 0)}</strong><span>直接资料</span></div><div class="info-metric"><strong>${Number(coverage.linked_document_count || 0)}</strong><span>关联资料</span></div><div class="info-metric"><strong>${Number(coverage.address_term_count || 0)}</strong><span>称呼证据</span></div><div class="info-metric"><strong>${Number(coverage.voice_evidence_count || 0)}</strong><span>语气证据</span></div></div></section>
    <section class="info-section"><h3>当前场景</h3><p>${escapeHtml(CHANNEL_LABELS[thread.channel])}</p><p>${escapeHtml(sceneText)}</p></section>
    <section class="info-section"><h3>装甲 / 时装语境</h3><p>${escapeHtml(styleText)}</p></section>
    <section class="info-section"><h3>本条回答依据</h3>${citations.length ? citations.map((item) => `<article class="citation"><strong>${escapeHtml(item.source_type || "资料")} · ${escapeHtml(item.title || "未命名来源")}</strong><blockquote>${escapeHtml(item.excerpt || "")}</blockquote></article>`).join("") : "<p>当前未选择带引用的回答。</p>"}</section>`;
}

function openInfo(result = null) {
  if (result) state.infoResult = result;
  renderInfo();
  byId("info-panel").classList.add("open");
  byId("info-panel").setAttribute("aria-hidden", "false");
  byId("drawer-scrim").hidden = false;
}

function closeDrawers() {
  byId("contact-panel").classList.remove("open");
  byId("info-panel").classList.remove("open");
  byId("info-panel").setAttribute("aria-hidden", "true");
  byId("drawer-scrim").hidden = true;
}

function findAssistantMessage(messageId) {
  return currentThread()?.messages.find((item) => item.id === messageId && item.role === "assistant") || null;
}

function renderFeedbackCategories() {
  byId("feedback-categories").innerHTML = `<legend>问题范围</legend>${state.feedbackCategories.map((category) => `<label class="feedback-category"><input type="radio" name="feedback-category" value="${escapeHtml(category.id)}" required /><span><strong>${escapeHtml(category.label)}</strong><span>${escapeHtml(category.description || "")}</span></span></label>`).join("")}`;
}

function openFeedback(message = null) {
  const character = currentCharacter();
  if (!character) {
    showToast("请先选择角色。");
    return;
  }
  state.feedbackTarget = message;
  renderFeedbackCategories();
  byId("feedback-text").value = "";
  byId("feedback-context").textContent = message
    ? `将附带 ${character.character_name} 的当前问题、回答、模式和交流媒介。`
    : `这是关于 ${character.character_name} 当前客户端会话的整体反馈。`;
  byId("feedback-dialog").showModal();
  setTimeout(() => byId("feedback-text").focus(), 0);
}

async function submitFeedback(event) {
  event.preventDefault();
  const character = currentCharacter();
  const thread = currentThread();
  const category = document.querySelector('input[name="feedback-category"]:checked')?.value;
  const freeText = byId("feedback-text").value.trim();
  if (!character || !thread || !category || !freeText) {
    showToast("请选择问题范围并填写具体说明。");
    return;
  }
  const message = state.feedbackTarget;
  const result = message?.result || null;
  byId("submit-feedback").disabled = true;
  try {
    await api("/api/v1/mvp/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: character.character_id,
        session_id: thread.sessionId || `client_ui_${character.character_id}`,
        message_id: message?.id || null,
        selected_options: [],
        category,
        free_text: freeText,
        mode: message?.mode || state.mode,
        communication_channel: message?.channel || thread.channel,
        registry_version: state.registryVersion,
        client_version: state.clientVersion,
        message_excerpt: message ? (thread.messages.slice(0, thread.messages.indexOf(message)).reverse().find((item) => item.role === "user")?.text || "") : "",
        answer_excerpt: result?.answer || message?.text || "",
      }),
    });
    byId("feedback-dialog").close();
    showToast("反馈已保存，后续可在后台工作台处理。 ");
  } catch (error) {
    showToast(`反馈提交失败：${error.message}`);
  } finally {
    byId("submit-feedback").disabled = false;
  }
}

async function clearConversation(mode = null) {
  const character = currentCharacter();
  const thread = currentThread();
  if (!character || !thread) return;
  const scopeText = mode ? `${MODE_LABELS[mode]}记录` : "该角色全部对话";
  if (!window.confirm(`确定清空${character.character_name}的${scopeText}吗？此操作不会删除反馈和资料。`)) return;
  try {
    const query = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    await api(`/api/v1/mvp/conversations/${encodeURIComponent(character.character_id)}${query}`, { method: "DELETE" });
    if (mode) {
      thread.messages = thread.messages.filter((item) => item.mode !== mode);
    } else {
      thread.messages = [];
      thread.sessionId = "";
      thread.conversationId = "";
      thread.latestResult = null;
      character.conversation = null;
    }
    thread.pending = null;
    thread.retry = null;
    thread.conflict = null;
    thread.error = "";
    state.infoResult = thread.latestResult;
    byId("settings-dialog").close();
    renderCharacterList();
    renderTimeline(false);
    renderInfo();
    showToast("本地会话记录已清理。");
  } catch (error) {
    showToast(`清理失败：${error.message}`);
  }
}

async function bootstrap() {
  try {
    const result = await api("/api/v1/mvp/bootstrap", {}, 30000);
    state.clientVersion = result.client_version || state.clientVersion;
    state.registryVersion = result.registry_version || "";
    state.enabled = Boolean(result.enabled && result.provider_configured);
    state.characters = (result.characters || []).filter((item) => item.selector_enabled !== false && item.view_available !== false);
    state.characterMap = new Map(state.characters.map((item) => [item.character_id, item]));
    state.feedbackCategories = result.feedback_categories || [];
    state.worldSessionId = result.active_world_session_id || "";
    setConnection(true, state.enabled ? `已连接 · ${result.model || "模型已配置"}` : "已连接 · 模型未开启");
    const savedCharacter = storageGet("project_snow:selected_character", "");
    const selected = state.characterMap.has(savedCharacter) ? savedCharacter : state.characters[0]?.character_id;
    renderCharacterList();
    renderFeedbackCategories();
    if (selected) await selectCharacter(selected);
  } catch (error) {
    setConnection(false, "服务连接失败");
    byId("system-banner").hidden = false;
    byId("system-banner").textContent = `无法连接本地服务：${error.message}`;
    byId("timeline").innerHTML = '<div class="empty-conversation"><div><strong>本地服务尚未就绪</strong><p>请确认 API 与 Web 服务均已启动，然后刷新客户端。</p></div></div>';
  }
  renderIcons();
}

byId("character-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-character-id]");
  if (button) selectCharacter(button.dataset.characterId);
});
byId("character-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderCharacterList();
});
byId("mode-control").addEventListener("click", (event) => {
  const button = event.target.closest("[data-mode]");
  if (button) setMode(button.dataset.mode);
});
byId("channel-control").addEventListener("click", (event) => {
  const button = event.target.closest("[data-channel]");
  if (button) setChannel(button.dataset.channel);
});
byId("analyst-input-kind").addEventListener("click", (event) => {
  const button = event.target.closest("[data-input-kind]");
  if (button) setInputKind(button.dataset.inputKind);
});
byId("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  queueMessage();
});
byId("message-input").addEventListener("input", () => { saveDraft(); resizeComposer(); });
byId("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    queueMessage();
  }
});
byId("timeline").addEventListener("click", (event) => {
  const feedback = event.target.closest("[data-message-feedback]");
  const info = event.target.closest("[data-message-info]");
  const retry = event.target.closest("[data-retry-message]");
  const presence = event.target.closest("[data-presence]");
  const suggestion = event.target.closest("[data-suggestion]");
  const older = event.target.closest("[data-load-older]");
  if (feedback) openFeedback(findAssistantMessage(feedback.dataset.messageFeedback));
  else if (info) {
    const message = findAssistantMessage(info.dataset.messageInfo);
    openInfo(message?.result || null);
  } else if (retry) retryMessage(retry.dataset.retryMessage);
  else if (presence) presenceChoice(presence.dataset.presence);
  else if (suggestion) {
    byId("message-input").value = suggestion.dataset.suggestion;
    saveDraft();
    resizeComposer();
    byId("message-input").focus();
  } else if (older) loadConversation(state.selectedCharacterId, { older: true });
});
byId("open-info").addEventListener("click", () => openInfo());
byId("close-info").addEventListener("click", closeDrawers);
byId("open-contacts").addEventListener("click", () => {
  byId("contact-panel").classList.add("open");
  byId("drawer-scrim").hidden = false;
});
byId("close-contacts").addEventListener("click", closeDrawers);
byId("drawer-scrim").addEventListener("click", closeDrawers);
byId("open-global-feedback").addEventListener("click", () => openFeedback(null));
byId("open-settings").addEventListener("click", () => byId("settings-dialog").showModal());
byId("feedback-form").addEventListener("submit", submitFeedback);
byId("clear-current-mode").addEventListener("click", () => clearConversation(state.mode));
byId("clear-character").addEventListener("click", () => clearConversation(null));
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => byId(button.dataset.closeDialog)?.close());
});

window.addEventListener("online", () => setConnection(true));
window.addEventListener("offline", () => setConnection(false, "网络不可用"));

renderIcons();
bootstrap();
