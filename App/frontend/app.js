import {
  api,
  byId,
  escapeHtml,
  formatBytes,
  formatTime,
  renderIcons,
  storageGet,
  storageSet,
  textPortrait,
} from "./ui-core.js";
import { modeForSurface, surfaceFromPath } from "./ui-router.js";

const activeSurface = surfaceFromPath();

const state = {
  clientVersion: "v0.5.0",
  registryVersion: "",
  enabled: false,
  characters: [],
  characterMap: new Map(),
  selectedCharacterId: "",
  surface: activeSurface,
  mode: modeForSurface(activeSurface),
  worldSessionId: "",
  threads: new Map(),
  feedbackCategories: [],
  feedbackTarget: null,
  infoResult: null,
  search: "",
  revealTimers: new Map(),
  models: [],
  modelDefaults: {},
  recording: null,
};

const MODE_LABELS = { immersive: "沉浸式", assistant: "助手" };
const CHANNEL_LABELS = { in_person: "面对面", text: "文字通讯" };
const TOOL_LABELS = {
  get_current_time: "当前时间",
  web_search: "网页搜索",
  research_current_info: "实时资料研究",
  fetch_web_page: "网页读取",
  get_market_history: "历史行情",
  calculator: "计算器",
};

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

function draftKey(characterId, mode, channel, inputType) {
  return `project_snow:draft:${characterId}:${mode}:${channel}:${inputType}`;
}

function getThread(characterId) {
  if (!state.threads.has(characterId)) {
    const character = state.characterMap.get(characterId);
    // v0.5 surfaces must never fall back to the legacy mixed summary: doing
    // so would show an assistant task as the first immersive preview (or the
    // reverse) before the mode-specific history has loaded.
    const summary = character?.conversations?.[state.mode] || {};
    state.threads.set(characterId, {
      characterId,
      conversationId: summary.conversation_id || "",
      sessionId: summary.session_id || "",
      worldSessionId: summary.world_session_id || "",
      channel: summary.communication_channel || channelPreference(characterId),
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
      attachments: [],
      uploadingAttachments: 0,
      modelOverride: storageGet(`project_snow:model:${characterId}`, ""),
      voiceReply: storageGet(`project_snow:voice:${characterId}`, "false") === "true",
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

function updateComposerFields() {
  const thread = currentThread();
  const actionField = byId("analyst-action-field");
  const speechInput = byId("message-input");
  const inPerson = Boolean(thread && thread.channel === "in_person");
  actionField.hidden = !inPerson;
  speechInput.placeholder = inPerson ? "输入对白（可选）…" : "输入消息…";
  speechInput.setAttribute("aria-label", inPerson ? "输入面对面对白" : "输入文字消息");
}

function avatarMarkup(character, size = "") {
  return textPortrait(character, size);
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
  const text = online ? (detail || "本地服务已连接") : (detail || "本地服务未连接");
  if (target) {
    target.className = `connection-status ${online ? "online" : "offline"}`;
    target.textContent = text;
  }
  const landing = byId("landing-status");
  if (landing) {
    landing.className = `connection-pill ${online ? "online" : "offline"}`;
    landing.textContent = text;
  }
}

function renderLandingCharacters() {
  const target = byId("landing-character-marks");
  if (!target) return;
  target.innerHTML = state.characters.map((character) => (
    `<span class="landing-character-mark">${textPortrait(character)}<span>${escapeHtml(character.character_name)}</span></span>`
  )).join("");
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
    const conversation = thread || character.conversations?.[state.mode] || {};
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
}

function updateControls() {
  const channel = currentThread()?.channel || "text";
  document.querySelectorAll("#channel-control [data-channel]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.channel === channel ? "true" : "false");
  });
  updateComposerFields();
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
    const params = new URLSearchParams({ limit: "50", mode: state.mode });
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
      thread.messages.filter((item) => item.role === "assistant" && item.result?.agent_run_id && !["succeeded", "failed", "cancelled"].includes(item.result?.agent_status)).forEach((item) => monitorAgentRun(item, thread));
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
  if (Array.isArray(message.attachments) && message.attachments.length && !message.text) return [];
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
    const attachments = Array.isArray(message.attachments) && message.attachments.length
      ? `<div class="message-attachments">${message.attachments.map((item) => `<span><i data-lucide="paperclip"></i>${escapeHtml(item.original_name || "附件")}</span>`).join("")}</div>` : "";
    return `<article class="message user ${escapeHtml(message.channel)}${isActionOnly ? " analyst-action-message" : ""}${statusClass}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta"><span>${escapeHtml(label)}</span><span>${escapeHtml(modeLabel)}</span><span>${escapeHtml(channelLabel)}</span>${kindLabel}${status}</div>${renderedBlocks}${attachments}${retry}</article>`;
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
  const result = message.result || {};
  const traceSummary = message.mode === "assistant" ? String(result.work_summary || "").trim() : "";
  const traceSteps = message.mode === "assistant" && Array.isArray(result.work_steps) ? result.work_steps.filter(Boolean).slice(0, 5) : [];
  const toolCalls = message.mode === "assistant" && Array.isArray(result.tool_calls) ? result.tool_calls : [];
  const trace = traceSummary || traceSteps.length || toolCalls.length
    ? `<details class="work-trace" open><summary><i data-lucide="sparkles"></i> ${toolCalls.length ? "角色化处理摘要 · 已使用只读工具" : "角色化处理摘要"}</summary>${traceSummary ? `<p>${escapeHtml(traceSummary)}</p>` : ""}${traceSteps.length ? `<ol>${traceSteps.map((step) => `<li>${escapeHtml(String(step))}</li>`).join("")}</ol>` : ""}${toolCalls.length ? `<div class="tool-trace">${toolCalls.map((call) => `<span class="tool-chip ${call.status === "failed" ? "failed" : ""}">${escapeHtml(TOOL_LABELS[call.name] || call.name || "只读工具")} · ${call.status === "completed" ? "完成" : "未完成"}</span>`).join("")}</div>` : ""}</details>`
    : "";
  const run = result.agent || null;
  const runSteps = run?.state?.steps || [];
  const approvals = (run?.state?.approvals || []).filter((item) => item.status === "pending");
  const artifacts = run?.state?.artifacts || [];
  const agentStatus = run?.status || result.agent_status || "queued";
  const agentOpen = !["succeeded", "failed", "cancelled"].includes(agentStatus) || approvals.length;
  const agentCard = message.mode === "assistant" && result.agent_run_id
    ? `<details class="agent-run"${agentOpen ? " open" : ""}><summary><strong>Agent 任务</strong><span>${escapeHtml(agentStatus)}</span></summary><div class="agent-run-body">${runSteps.length ? `<ol>${runSteps.map((step) => `<li><span>${escapeHtml(step.tool_name || step.kind || "步骤")}</span><small>${escapeHtml(step.status || "pending")}</small></li>`).join("")}</ol>` : '<p>正在准备可审计的执行步骤…</p>'}${artifacts.length ? `<div class="artifact-list">${artifacts.map((item) => `<a href="/api/v1/artifacts/${escapeHtml(item.artifact_id)}" download>${escapeHtml(item.file_name)} · ${escapeHtml(formatBytes(item.size_bytes))}</a>`).join("")}</div>` : ""}${approvals.map((approval) => `<div class="approval-card"><p>${escapeHtml(approval.summary)}</p><button type="button" data-agent-approval="${escapeHtml(approval.approval_id)}" data-run-id="${escapeHtml(result.agent_run_id)}" data-decision="approved">允许</button><button type="button" data-agent-approval="${escapeHtml(approval.approval_id)}" data-run-id="${escapeHtml(result.agent_run_id)}" data-decision="rejected">拒绝</button></div>`).join("")}${!["succeeded", "failed", "cancelled"].includes(agentStatus) ? `<button type="button" class="secondary-button" data-cancel-run="${escapeHtml(result.agent_run_id)}">停止任务</button>` : ["failed", "cancelled"].includes(agentStatus) ? `<button type="button" class="secondary-button" data-retry-agent="${escapeHtml(result.agent_run_id)}">重试任务</button>` : ""}</div></details>` : "";
  const usage = result.usage || {};
  const usageText = usage.total_tokens || usage.prompt_tokens || usage.completion_tokens
    ? `输入 ${usage.prompt_tokens ?? "-"} · 输出 ${usage.completion_tokens ?? "-"} · 合计 ${usage.total_tokens ?? "-"} tokens`
    : "供应商未返回用量";
  const routingReason = result.routing_decision?.fallback_reason || result.routing_decision?.reason || "质量优先能力路由";
  const modelMeta = message.mode === "assistant" && result.actual_model?.model_name
    ? `<details class="model-meta"><summary>模型、路由与用量${result.routing_decision?.fallback ? " · 已回退" : ""}</summary><p>${escapeHtml(result.actual_model.provider_name || result.actual_model.provider_id || "模型")} · ${escapeHtml(result.actual_model.model_name)}</p><p>${escapeHtml(routingReason)} · ${escapeHtml(usageText)}</p></details>`
    : "";
  const audio = result.audio?.status === "completed" ? `<audio class="voice-reply" controls preload="metadata" src="${escapeHtml(result.audio.content_url)}"></audio>` : "";
  return `<article class="message assistant ${escapeHtml(message.channel)}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta"><span>${escapeHtml(label)}</span><span>${escapeHtml(modeLabel)}</span><span>${escapeHtml(channelLabel)}</span></div>${blocks}${trace}${agentCard}${audio}${modelMeta}<div class="message-actions"><button type="button" data-message-info="${escapeHtml(message.id)}">查看依据</button><button type="button" data-message-feedback="${escapeHtml(message.id)}">反馈</button></div></article>`;
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

function renderAttachments() {
  const thread = currentThread();
  const target = byId("attachment-preview");
  if (!thread) { target.innerHTML = ""; return; }
  const attachments = thread.attachments || [];
  target.innerHTML = [
    ...attachments.map((item) => `<div class="attachment-chip ${String(item.mime_type || "").startsWith("audio/") ? "audio-attachment" : ""}"><i data-lucide="${String(item.mime_type || "").startsWith("image/") ? "image" : String(item.mime_type || "").startsWith("audio/") ? "audio-lines" : "file-text"}"></i><span><strong>${escapeHtml(item.original_name)}</strong><small>${escapeHtml(formatBytes(item.size_bytes))} · ${escapeHtml(item.transcribing ? "正在转写" : item.transcript_error ? "转写待处理" : item.parse_status || "已保存")}</small></span><button type="button" data-remove-attachment="${escapeHtml(item.attachment_id)}" aria-label="移除附件"><i data-lucide="x"></i></button>${String(item.mime_type || "").startsWith("audio/") ? `<label class="transcript-editor"><span>发送前可编辑转写</span><textarea data-attachment-transcript="${escapeHtml(item.attachment_id)}" rows="2" placeholder="语音转写将在这里显示；也可手动输入">${escapeHtml(item.edited_transcript ?? item.extracted_text ?? "")}</textarea>${item.transcript_error ? `<button type="button" data-retry-transcription="${escapeHtml(item.attachment_id)}">重新转写</button>` : ""}</label>` : ""}</div>`),
    ...(thread.uploadingAttachments ? [`<span class="attachment-chip uploading"><i data-lucide="loader-circle"></i><span><strong>正在处理附件</strong><small>${thread.uploadingAttachments} 个文件</small></span></span>`] : []),
  ].join("");
  renderIcons();
}

async function uploadFiles(fileList) {
  const thread = currentThread();
  if (!thread) return;
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if ((thread.attachments?.length || 0) + files.length > 10) {
    showToast("每轮最多添加 10 个附件。");
    return;
  }
  const total = files.reduce((sum, file) => sum + file.size, 0) + (thread.attachments || []).reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
  if (total > 100 * 1024 * 1024) {
    showToast("本轮附件总大小不能超过 100 MB。");
    return;
  }
  thread.uploadingAttachments += files.length;
  renderAttachments();
  for (const file of files) {
    try {
      const result = await api("/api/v1/attachments", {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": file.name },
        body: file,
      }, 120000);
      if (!thread.attachments.some((item) => item.attachment_id === result.attachment_id)) thread.attachments.push(result);
      const stored = thread.attachments.find((item) => item.attachment_id === result.attachment_id);
      if (stored && String(stored.mime_type || "").startsWith("audio/")) transcribeAudioAttachment(thread, stored);
    } catch (error) {
      showToast(`${file.name} 上传失败：${error.message}`);
    } finally {
      thread.uploadingAttachments -= 1;
      renderAttachments();
      updateRequestStatus();
    }
  }
}

async function transcribeAudioAttachment(thread, attachment) {
  if (!thread || !attachment || attachment.transcribing) return;
  attachment.transcribing = true;
  attachment.transcript_error = "";
  renderAttachments();
  try {
    const modelOverride = thread.modelOverride ? (() => { const [provider_id, model_name] = thread.modelOverride.split("::"); return { provider_id, model_name }; })() : null;
    const result = await api(`/api/v1/attachments/${encodeURIComponent(attachment.attachment_id)}/transcription`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ transcript: null, model_override: modelOverride }),
    }, 180000);
    Object.assign(attachment, result, { edited_transcript: result.extracted_text || "" });
  } catch (error) {
    attachment.transcript_error = error.message;
    showToast(`语音转写失败：${error.message}`);
  } finally {
    attachment.transcribing = false;
    if (state.selectedCharacterId === thread.characterId) {
      renderAttachments();
      updateRequestStatus();
    }
  }
}

async function removeAttachment(attachmentId) {
  const thread = currentThread();
  if (!thread) return;
  thread.attachments = thread.attachments.filter((item) => item.attachment_id !== attachmentId);
  renderAttachments();
  try { await api(`/api/v1/attachments/${encodeURIComponent(attachmentId)}`, { method: "DELETE" }); } catch (_) { /* it may be shared by a deduplicated upload */ }
}

async function toggleRecording() {
  const button = byId("record-audio");
  if (state.recording) {
    state.recording.recorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    showToast("当前浏览器不支持录音。");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const recorder = new MediaRecorder(stream);
    recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener("stop", async () => {
      stream.getTracks().forEach((track) => track.stop());
      state.recording = null;
      button.classList.remove("recording");
      button.innerHTML = '<i data-lucide="mic"></i>';
      renderIcons();
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      const file = new File([blob], `recording-${Date.now()}.webm`, { type: blob.type });
      await uploadFiles([file]);
    });
    recorder.start();
    state.recording = { recorder, stream };
    button.classList.add("recording");
    button.innerHTML = '<i data-lucide="square"></i>';
    renderIcons();
  } catch (error) {
    showToast(`无法开始录音：${error.message}`);
  }
}

function restoreDraft() {
  const speechInput = byId("message-input");
  const actionInput = byId("action-input");
  const thread = currentThread();
  speechInput.value = state.selectedCharacterId && thread
    ? storageGet(draftKey(state.selectedCharacterId, state.mode, thread.channel, "speech"), "")
    : "";
  actionInput.value = state.selectedCharacterId && thread && thread.channel === "in_person"
    ? storageGet(draftKey(state.selectedCharacterId, state.mode, thread.channel, "action"), "")
    : "";
  updateComposerFields();
  resizeComposer();
}

function saveDraft() {
  const thread = currentThread();
  if (!state.selectedCharacterId || !thread) return;
  storageSet(
    draftKey(state.selectedCharacterId, state.mode, thread.channel, "speech"),
    byId("message-input").value,
  );
  if (thread.channel === "in_person") {
    storageSet(
      draftKey(state.selectedCharacterId, state.mode, thread.channel, "action"),
      byId("action-input").value,
    );
  }
}

function resizeComposer() {
  [byId("message-input"), byId("action-input")].forEach((input) => {
    if (!input) return;
    input.rows = 1;
    const computed = window.getComputedStyle(input);
    const lineHeight = Number.parseFloat(computed.lineHeight) || 24;
    const verticalPadding = Number.parseFloat(computed.paddingTop) + Number.parseFloat(computed.paddingBottom);
    input.rows = Math.min(5, Math.max(1, Math.ceil((input.scrollHeight - verticalPadding) / lineHeight)));
  });
  if (state.mode !== "assistant") byId("agent-mode").checked = false;
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
  const attachmentBusy = Boolean(thread?.attachments?.some((item) => item.transcribing));
  byId("send-message").disabled = !state.selectedCharacterId || Boolean(thread?.pending) || Boolean(thread?.uploadingAttachments) || attachmentBusy;
}

async function selectCharacter(characterId) {
  if (!state.characterMap.has(characterId)) return;
  saveDraft();
  state.selectedCharacterId = characterId;
  storageSet(`project_snow:selected_character:${state.mode}`, characterId);
  const thread = getThread(characterId);
  byId("model-override").value = thread.modelOverride || "";
  byId("voice-reply").checked = Boolean(thread.voiceReply);
  if (!state.worldSessionId && thread.worldSessionId) state.worldSessionId = thread.worldSessionId;
  renderCharacterList();
  renderHeader();
  restoreDraft();
  renderTimeline(false);
  renderInfo();
  updateRequestStatus();
  renderAttachments();
  closeDrawers();
  byId("message-input").focus();
  await loadConversation(characterId);
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
    attachments: pending.attachments || [],
  };
}

async function monitorAgentRun(message, thread) {
  const runId = message?.result?.agent_run_id;
  if (!runId) return;
  for (let attempt = 0; attempt < 900; attempt += 1) {
    try {
      const snapshot = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}`, {}, 15000);
      message.result.agent = snapshot;
      message.result.agent_status = snapshot.status;
      message.result.artifacts = snapshot.state?.artifacts || [];
      message.result.actual_model = snapshot.state?.actual_model || message.result.actual_model || {};
      message.result.usage = snapshot.state?.usage || message.result.usage || {};
      message.result.audio = snapshot.state?.audio || message.result.audio || null;
      if (snapshot.status === "succeeded") {
        message.text = snapshot.state?.final_answer || "任务已经完成，执行步骤和结果都记录在下方。";
        message.blocks = [{ type: "message", text: message.text }];
      } else if (snapshot.status === "failed") {
        message.text = `任务未能完成：${snapshot.state?.error || "请查看执行步骤。"}`;
        message.blocks = [{ type: "message", text: message.text }];
      } else if (snapshot.status === "cancelled") {
        message.text = "任务已停止。";
        message.blocks = [{ type: "message", text: message.text }];
      }
      if (state.selectedCharacterId === thread.characterId) {
        renderTimeline(false);
        if (snapshot.status === "succeeded" && thread.voiceReply && message.result.audio?.status === "completed") {
          requestAnimationFrame(() => {
            const audio = document.querySelector(`[data-message-id="${CSS.escape(message.id)}"] audio.voice-reply`);
            audio?.play().catch(() => {});
          });
        }
      }
      if (["succeeded", "failed", "cancelled"].includes(snapshot.status)) return;
    } catch (error) {
      message.result.agent_error = error.message;
      if (state.selectedCharacterId === thread.characterId) renderTimeline(false);
    }
    await new Promise((resolve) => window.setTimeout(resolve, 900));
  }
}

async function decideAgentApproval(runId, approvalId, decision) {
  try {
    await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, note: "客户端审批" }),
    });
    showToast(decision === "approved" ? "已允许该步骤。" : "已拒绝并停止任务。");
  } catch (error) { showToast(`审批失败：${error.message}`); }
}

async function cancelAgentRun(runId) {
  try {
    await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    showToast("已请求停止任务。");
  } catch (error) { showToast(`停止失败：${error.message}`); }
}

async function retryAgentRun(runId) {
  try {
    const snapshot = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" });
    const thread = currentThread();
    const message = thread?.messages.find((item) => item.result?.agent_run_id === runId);
    if (message) {
      message.result.agent_run_id = snapshot.run_id;
      message.result.agent_status = snapshot.status;
      message.result.agent = snapshot;
      message.text = "任务已重新进入执行队列。";
      message.blocks = [{ type: "message", text: message.text }];
      renderTimeline(false);
      monitorAgentRun(message, thread);
    }
  } catch (error) { showToast(`重试失败：${error.message}`); }
}

async function queueMessage() {
  const character = currentCharacter();
  const thread = currentThread();
  const speechInput = byId("message-input");
  const actionInput = byId("action-input");
  const speech = speechInput.value.trim();
  const action = thread?.channel === "in_person" ? actionInput.value.trim() : "";
  const attachments = [...(thread?.attachments || [])];
  if (!character || !thread || (!speech && !action && !attachments.length) || thread.pending || thread.uploadingAttachments || attachments.some((item) => item.transcribing)) return;
  const blocks = thread.channel === "text"
    ? [{ type: "message", text: speech }]
    : [
        ...(action ? [{ type: "action", text: action }] : []),
        ...(speech ? [{ type: "speech", text: speech }] : []),
      ];
  const message = blocks.map((block) => block.text).join("\n");
  const messageLimit = state.mode === "assistant" ? 12000 : 4000;
  if (message.length > messageLimit) {
    thread.error = `本轮动作与对白合计不能超过 ${messageLimit} 个字符。`;
    updateRequestStatus();
    return;
  }
  const pending = {
    message,
    clientMessageId: newClientMessageId(),
    userMessageId: `local_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    mode: state.mode,
    channel: thread.channel,
    blocks,
    attachments,
    attachmentIds: attachments.map((item) => item.attachment_id),
    attachmentTranscripts: Object.fromEntries(attachments.filter((item) => String(item.mime_type || "").startsWith("audio/") && String(item.edited_transcript || item.extracted_text || "").trim()).map((item) => [item.attachment_id, String(item.edited_transcript || item.extracted_text).trim()])),
    agentMode: state.mode === "assistant" && byId("agent-mode").checked,
    voiceReply: byId("voice-reply").checked,
    modelOverride: byId("model-override").value,
    modelOnce: byId("model-once").checked,
    sending: false,
  };
  thread.pending = pending;
  thread.retry = null;
  thread.conflict = null;
  thread.error = "";
  thread.messages.push(optimisticUserMessage(pending));
  speechInput.value = "";
  actionInput.value = "";
  saveDraft();
  resizeComposer();
  speechInput.focus();
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
        attachment_ids: pending.attachmentIds || [],
        attachment_transcripts: pending.attachmentTranscripts || {},
        voice_reply: Boolean(pending.voiceReply),
        agent_mode: Boolean(pending.agentMode),
        model_override: pending.modelOverride ? (() => { const [provider_id, model_name] = pending.modelOverride.split("::"); return { provider_id, model_name }; })() : null,
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
    if (result.agent_run_id) monitorAgentRun(assistantMessage, thread);
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
    if (pending.attachmentIds?.length) thread.attachments = [];
    if (pending.modelOnce) {
      thread.modelOverride = "";
      storageSet(`project_snow:model:${thread.characterId}`, "");
      byId("model-override").value = "";
      byId("model-once").checked = false;
    }
    renderAttachments();
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
  const latest = thread?.messages?.slice(-1)[0];
  if (latest?.role === "assistant" && !latest.result?.agent_run_id && latest.result?.audio?.status === "completed" && thread.voiceReply) {
    requestAnimationFrame(() => {
      const audio = document.querySelector(`[data-message-id="${CSS.escape(latest.id)}"] audio.voice-reply`);
      audio?.play().catch(() => {});
    });
  }
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
  const webSources = Array.isArray(result.web_sources) ? result.web_sources : [];
  const toolCalls = state.mode === "assistant" && Array.isArray(result.tool_calls) ? result.tool_calls : [];
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
    <section class="info-section"><h3>本条回答依据</h3>${citations.length ? citations.map((item) => `<article class="citation"><strong>${escapeHtml(item.source_type || "资料")} · ${escapeHtml(item.title || "未命名来源")}</strong><blockquote>${escapeHtml(item.excerpt || "")}</blockquote></article>`).join("") : "<p>当前未选择带引用的回答。</p>"}${webSources.length ? `<h4 class="info-subheading">联网参考</h4>${webSources.map((item) => `<article class="citation"><strong>${escapeHtml(item.title || "网页")}</strong><a class="source-link" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(item.url || "")}</a><blockquote>${escapeHtml(item.snippet || "")}</blockquote></article>`).join("")}` : ""}${toolCalls.length ? `<p class="tool-note">本轮只读工具：${toolCalls.map((item) => `${escapeHtml(item.name || "工具")}（${item.status === "completed" ? "完成" : "未完成"}）`).join("、")}</p>` : ""}</section>`;
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

function openFeedback(message = null, forceProduct = false) {
  const character = currentCharacter();
  const productScope = forceProduct || state.surface === "landing" || !character;
  state.feedbackTarget = productScope ? { scope: "product", message: null } : {
    scope: message ? "message" : "conversation",
    message,
  };
  renderFeedbackCategories();
  byId("feedback-text").value = "";
  byId("feedback-context").textContent = productScope
    ? `这是关于 ${state.surface === "landing" ? "入口页" : "客户端"} 的产品反馈，将附带版本和界面来源。`
    : message
      ? `将附带 ${character.character_name} 的当前问题、回答、模式和交流媒介。`
      : `这是关于 ${character.character_name} 当前会话的整体反馈。`;
  byId("feedback-dialog").showModal();
  setTimeout(() => byId("feedback-text").focus(), 0);
}

async function submitFeedback(event) {
  event.preventDefault();
  const character = currentCharacter();
  const thread = currentThread();
  const category = document.querySelector('input[name="feedback-category"]:checked')?.value;
  const freeText = byId("feedback-text").value.trim();
  if (!category || !freeText) {
    showToast("请选择问题范围并填写具体说明。");
    return;
  }
  const scope = state.feedbackTarget?.scope || (character ? "conversation" : "product");
  const message = state.feedbackTarget?.message || null;
  if (scope !== "product" && (!character || !thread)) {
    showToast("当前会话尚未准备好，请稍后再试。");
    return;
  }
  const result = message?.result || null;
  byId("submit-feedback").disabled = true;
  try {
    await api("/api/v1/mvp/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope,
        ui_surface: state.surface,
        character_id: scope === "product" ? null : character.character_id,
        session_id: scope === "product" ? null : (thread.sessionId || `client_ui_${character.character_id}`),
        message_id: message?.id || null,
        selected_options: [],
        category,
        free_text: freeText,
        mode: scope === "product" ? null : (message?.mode || state.mode),
        communication_channel: scope === "product" ? null : (message?.channel || thread.channel),
        registry_version: state.registryVersion,
        client_version: state.clientVersion,
        message_excerpt: message && thread ? (thread.messages.slice(0, thread.messages.indexOf(message)).reverse().find((item) => item.role === "user")?.text || "") : "",
        answer_excerpt: result?.answer || message?.text || "",
        agent_run_id: result?.agent_run_id || null,
        actual_model: result?.actual_model || {},
        attachment_ids: (result?.attachment_results || []).map((item) => item.attachment_id).filter(Boolean),
        failed_stage: result?.agent?.status === "failed" ? (result?.agent?.state?.events || []).slice(-1)[0]?.kind || "agent" : null,
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
      if (character.conversations) character.conversations[mode] = null;
    } else {
      thread.messages = [];
      thread.sessionId = "";
      thread.conversationId = "";
      thread.latestResult = null;
      character.conversation = null;
      character.conversations = { immersive: null, assistant: null };
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

function providerPayload() {
  const kind = byId("provider-kind").value;
  const builtinUrls = {
    openai: "https://api.openai.com/v1",
    dashscope: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    zhipu: "https://open.bigmodel.cn/api/paas/v4",
    deepseek: "https://api.deepseek.com/v1",
    moonshot: "https://api.moonshot.cn/v1",
  };
  return {
    provider_id: kind === "openai-compatible" ? "custom-ui" : kind,
    display_name: byId("provider-name").value.trim() || kind,
    kind,
    base_url: byId("provider-url").value.trim() || builtinUrls[kind] || "",
    api_key: byId("provider-key").value.trim(),
    enabled: true,
    trusted_data_types: byId("trust-images").checked ? ["text", "image", "document", "audio"] : ["text"],
    config: {
      tts_voice: byId("tts-voice").value.trim() || undefined,
      voice_by_character: state.selectedCharacterId && byId("character-voice").value.trim()
        ? { [state.selectedCharacterId]: byId("character-voice").value.trim() }
        : {},
    },
  };
}

async function configureProvider(andProbe = false) {
  const status = byId("provider-status");
  try {
    const payload = providerPayload();
    if (!payload.base_url) throw new Error("请填写 Provider Base URL。");
    status.textContent = "正在保存本地 Provider 配置…";
    const saved = await api("/api/v1/providers", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }, 30000);
    byId("provider-key").value = "";
    if (andProbe) {
      const model = byId("provider-model").value.trim();
      if (!model) throw new Error("探测前请填写模型名称。");
      status.textContent = "正在执行真实文本能力探测…";
      await api(`/api/v1/providers/${encodeURIComponent(saved.provider_id)}/probe`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_name: model,
          quality_score: Number(byId("provider-quality").value || 50),
          context_window: Number(byId("provider-context").value || 0) || null,
          max_output_tokens: Number(byId("provider-max-output").value || 0) || null,
          capabilities: {
          structured_output: true,
          native_tool_calling: byId("cap-tools").checked,
          streaming: byId("cap-streaming").checked,
          vision: byId("cap-vision").checked,
          speech_to_text: byId("cap-stt").checked,
          text_to_speech: byId("cap-tts").checked,
        } }),
      }, 120000);
      status.textContent = "探测成功；已记录真实文本探测和用户声明能力。";
      await loadModels();
    } else {
      status.textContent = "Provider 已保存；API Key 不会在页面回显。";
    }
  } catch (error) {
    status.textContent = `配置失败：${error.message}`;
  }
}

async function loadModels() {
  try {
    const result = await api("/api/v1/models", {}, 30000);
    state.models = result.models || [];
    state.modelDefaults = result.defaults || {};
    const verified = state.models.filter((item) => item.probe_status === "verified");
    const picker = byId("model-override");
    const selected = currentThread()?.modelOverride || picker.value;
    picker.innerHTML = '<option value="">质量优先自动路由</option>' + verified.map((item) => `<option value="${escapeHtml(`${item.provider_id}::${item.model_name}`)}">${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)}</option>`).join("");
    picker.value = [...picker.options].some((option) => option.value === selected) ? selected : "";
    byId("active-model").textContent = verified.length ? `自动路由 · ${verified.length} 个已验证模型` : "自动路由 · 当前环境模型";
    const capabilityLabels = { text: "文本", structured_output: "结构化", native_tool_calling: "工具", vision: "视觉", speech_to_text: "STT", text_to_speech: "TTS", streaming: "流式" };
    byId("model-capability-list").innerHTML = verified.map((item) => {
      const enabled = Object.entries(item.capabilities || {}).filter(([, value]) => value === true).map(([key]) => capabilityLabels[key] || key).join(" · ");
      const latency = item.probe?.latency_ms ? `${Math.round(item.probe.latency_ms)} ms` : "延迟未知";
      return `<div><strong>${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)}</strong><small>${escapeHtml(enabled || "仅文本待验证")} · 质量 ${Number(item.quality_score || 0)} · ${escapeHtml(latency)}</small></div>`;
    }).join("") || "<p>尚无已验证模型。</p>";
    const defaultBindings = [
      ["default-text-model", "text", "text"],
      ["default-vision-model", "vision", "vision"],
      ["default-stt-model", "speech_to_text", "speech_to_text"],
      ["default-tts-model", "text_to_speech", "text_to_speech"],
    ];
    defaultBindings.forEach(([id, key, capability]) => {
      const target = byId(id);
      const eligible = verified.filter((item) => item.capabilities?.[capability]);
      target.innerHTML = `<option value="">${key === "text" ? "自动选择文本模型" : `不指定 ${capability}`}</option>` + eligible.map((item) => `<option value="${escapeHtml(`${item.provider_id}::${item.model_name}`)}">${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)}</option>`).join("");
      const current = state.modelDefaults[key];
      target.value = current ? `${current.provider_id}::${current.model_name}` : "";
    });
  } catch (_) {
    byId("active-model").textContent = "自动路由";
  }
}

async function saveModelDefaults() {
  const bindings = { text: "default-text-model", vision: "default-vision-model", speech_to_text: "default-stt-model", text_to_speech: "default-tts-model" };
  const payload = {};
  Object.entries(bindings).forEach(([key, id]) => {
    const value = byId(id).value;
    if (!value) return;
    const [provider_id, model_name] = value.split("::");
    payload[key] = { provider_id, model_name };
  });
  try {
    await api("/api/v1/models/defaults", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }, 30000);
    showToast("默认模型已保存。后续自动路由仍会检查模态与 Provider 授权。");
    await loadModels();
  } catch (error) { showToast(`默认模型保存失败：${error.message}`); }
}

async function previewVoice() {
  const character = currentCharacter();
  if (!character) return showToast("请先选择角色。");
  try {
    const result = await api("/api/v1/voices/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: character.character_id, text: `你好，分析员。我是${character.character_name}。` }),
    }, 180000);
    if (result.content_url) {
      const audio = new Audio(result.content_url);
      await audio.play();
    }
  } catch (error) { showToast(`试听失败：${error.message}`); }
}

async function bootstrap() {
  document.body.dataset.surface = state.surface;
  byId("landing-view").hidden = state.surface !== "landing";
  byId("chat-app").hidden = state.surface === "landing";
  byId("surface-label").textContent = state.mode === "assistant" ? "角色助手" : "沉浸式陪伴";
  try {
    const result = await api("/api/v1/mvp/bootstrap", {}, 30000);
    state.clientVersion = result.client_version || state.clientVersion;
    state.registryVersion = result.registry_version || "";
    state.enabled = Boolean(result.enabled && result.provider_configured);
    state.characters = (result.characters || []).filter((item) => item.selector_enabled !== false && item.view_available !== false);
    state.characterMap = new Map(state.characters.map((item) => [item.character_id, item]));
    state.feedbackCategories = result.feedback_categories || [];
    state.worldSessionId = result.active_world_session_id || "";
    await loadModels();
    setConnection(true, state.enabled ? `已连接 · ${result.model || "模型已配置"}` : "已连接 · 模型未开启");
    renderLandingCharacters();
    renderFeedbackCategories();
    if (state.surface === "landing") return;
    const savedCharacter = storageGet(`project_snow:selected_character:${state.mode}`, "");
    const selected = state.characterMap.has(savedCharacter) ? savedCharacter : state.characters[0]?.character_id;
    renderCharacterList();
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
byId("channel-control").addEventListener("click", (event) => {
  const button = event.target.closest("[data-channel]");
  if (button) setChannel(button.dataset.channel);
});
byId("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  queueMessage();
});
byId("message-input").addEventListener("input", () => { saveDraft(); resizeComposer(); });
byId("action-input").addEventListener("input", () => { saveDraft(); resizeComposer(); });
[byId("message-input"), byId("action-input")].forEach((input) => {
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      queueMessage();
    }
  });
});
byId("timeline").addEventListener("click", (event) => {
  const feedback = event.target.closest("[data-message-feedback]");
  const info = event.target.closest("[data-message-info]");
  const retry = event.target.closest("[data-retry-message]");
  const presence = event.target.closest("[data-presence]");
  const suggestion = event.target.closest("[data-suggestion]");
  const older = event.target.closest("[data-load-older]");
  const approval = event.target.closest("[data-agent-approval]");
  const cancelRun = event.target.closest("[data-cancel-run]");
  const retryRun = event.target.closest("[data-retry-agent]");
  if (approval) decideAgentApproval(approval.dataset.runId, approval.dataset.agentApproval, approval.dataset.decision);
  else if (cancelRun) cancelAgentRun(cancelRun.dataset.cancelRun);
  else if (retryRun) retryAgentRun(retryRun.dataset.retryAgent);
  else if (feedback) openFeedback(findAssistantMessage(feedback.dataset.messageFeedback));
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
byId("floating-feedback").addEventListener("click", () => openFeedback(null, state.surface === "landing"));
byId("landing-open-feedback").addEventListener("click", () => openFeedback(null, true));
[byId("open-settings"), byId("landing-open-settings")].forEach((button) => button?.addEventListener("click", () => byId("settings-dialog").showModal()));
const allAttachmentTypes = "image/jpeg,image/png,image/webp,image/gif,.pdf,.txt,.md,.csv,.json,.docx,.xlsx,.pptx,.py,.js,.ts,.html,.css,audio/wav,audio/mpeg,audio/mp4,audio/webm,audio/ogg";
byId("add-image").addEventListener("click", () => {
  byId("attachment-input").accept = "image/jpeg,image/png,image/webp,image/gif";
  byId("attachment-input").click();
});
byId("add-attachment").addEventListener("click", () => {
  byId("attachment-input").accept = allAttachmentTypes;
  byId("attachment-input").click();
});
byId("add-more-attachment").addEventListener("click", () => {
  byId("attachment-input").accept = allAttachmentTypes;
  byId("attachment-input").click();
  byId("immersive-attachment-menu").removeAttribute("open");
});
byId("assistant-tool-toggle").addEventListener("click", () => {
  const panel = byId("assistant-tool-panel");
  const expanded = panel.hidden;
  panel.hidden = !expanded;
  byId("assistant-tool-toggle").setAttribute("aria-expanded", String(expanded));
});
byId("attachment-input").addEventListener("change", async (event) => {
  await uploadFiles(event.target.files);
  event.target.value = "";
});
byId("record-audio").addEventListener("click", toggleRecording);
byId("voice-reply").addEventListener("change", (event) => {
  const thread = currentThread();
  if (!thread) return;
  thread.voiceReply = Boolean(event.target.checked);
  storageSet(`project_snow:voice:${thread.characterId}`, String(thread.voiceReply));
});
byId("model-override").addEventListener("change", (event) => {
  const thread = currentThread();
  if (!thread) return;
  thread.modelOverride = event.target.value;
  if (!byId("model-once").checked) storageSet(`project_snow:model:${thread.characterId}`, thread.modelOverride);
});
byId("attachment-preview").addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove-attachment]");
  const retry = event.target.closest("[data-retry-transcription]");
  if (remove) removeAttachment(remove.dataset.removeAttachment);
  else if (retry) {
    const thread = currentThread();
    const attachment = thread?.attachments.find((item) => item.attachment_id === retry.dataset.retryTranscription);
    if (attachment) transcribeAudioAttachment(thread, attachment);
  }
});
byId("attachment-preview").addEventListener("input", (event) => {
  const editor = event.target.closest("[data-attachment-transcript]");
  const thread = currentThread();
  if (!editor || !thread) return;
  const attachment = thread.attachments.find((item) => item.attachment_id === editor.dataset.attachmentTranscript);
  if (attachment) attachment.edited_transcript = editor.value;
});
byId("composer").addEventListener("dragover", (event) => { event.preventDefault(); byId("composer").classList.add("dragging"); });
byId("composer").addEventListener("dragleave", () => byId("composer").classList.remove("dragging"));
byId("composer").addEventListener("drop", (event) => {
  event.preventDefault();
  byId("composer").classList.remove("dragging");
  uploadFiles(event.dataTransfer?.files);
});
byId("message-input").addEventListener("paste", (event) => {
  const files = Array.from(event.clipboardData?.items || []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
  if (files.length) uploadFiles(files);
});
byId("save-provider").addEventListener("click", () => configureProvider(false));
byId("probe-provider").addEventListener("click", () => configureProvider(true));
byId("save-model-defaults").addEventListener("click", saveModelDefaults);
byId("preview-voice").addEventListener("click", previewVoice);
byId("feedback-form").addEventListener("submit", submitFeedback);
byId("clear-current-mode").addEventListener("click", () => clearConversation(state.mode));
byId("clear-character").addEventListener("click", () => clearConversation(null));
document.querySelectorAll("[data-settings-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-settings-tab]").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll("[data-settings-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.settingsPanel !== button.dataset.settingsTab;
    });
  });
});
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => byId(button.dataset.closeDialog)?.close());
});

window.addEventListener("online", () => setConnection(true));
window.addEventListener("offline", () => setConnection(false, "网络不可用"));

renderIcons();
bootstrap();
