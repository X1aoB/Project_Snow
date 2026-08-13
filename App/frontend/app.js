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
  providers: [],
  models: [],
  modelDefaults: {},
  recording: null,
  personaGateway: null,
  sceneByCharacter: new Map(),
  presenceDialogCharacterId: "",
  transitionPending: false,
  arrivalPending: null,
  stageUiHidden: false,
  stageReveal: {
    messageId: "",
    fullText: "",
    visibleCharacters: 0,
    completed: true,
    timer: null,
  },
};

const MODE_LABELS = { immersive: "沉浸式", assistant: "助手" };
const CHANNEL_LABELS = { in_person: "面对面", text: "文字通讯" };
const SCENE_KEYS = new Set(["generic", "quarters", "lounge", "training", "archive", "canteen", "observation", "medical", "corridor"]);
const TOOL_LABELS = {
  get_current_time: "当前时间",
  web_search: "网页搜索",
  research_current_info: "实时资料研究",
  fetch_web_page: "网页读取",
  get_market_history: "历史行情",
  calculator: "计算器",
};
const PROVIDER_PRESETS = {
  openai: { name: "OpenAI", shortName: "ChatGPT", baseUrl: "https://api.openai.com/v1", modelHint: "填写 OpenAI 模型 ID" },
  deepseek: { name: "DeepSeek", shortName: "DeepSeek", baseUrl: "https://api.deepseek.com/v1", modelHint: "例如厂商控制台中可用的模型 ID" },
  dashscope: { name: "阿里云百炼 Qwen", shortName: "Qwen", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", modelHint: "例如 qwen3.7-max" },
  zhipu: { name: "智谱 GLM", shortName: "GLM", baseUrl: "https://open.bigmodel.cn/api/paas/v4", modelHint: "填写智谱控制台中的模型 ID" },
  moonshot: { name: "Moonshot / Kimi", shortName: "Kimi", baseUrl: "https://api.moonshot.cn/v1", modelHint: "填写 Moonshot 控制台中的模型 ID" },
  "openai-compatible": { name: "自定义兼容接口", shortName: "其他厂商", baseUrl: "", modelHint: "填写兼容接口提供的模型 ID" },
};

function newClientMessageId() {
  if (window.crypto?.randomUUID) return `client_${window.crypto.randomUUID()}`;
  return `client_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function newWorldSessionId() {
  if (window.crypto?.randomUUID) return `world_browser_${window.crypto.randomUUID()}`;
  return `world_browser_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function ensureWorldSessionId() {
  if (state.worldSessionId) return state.worldSessionId;
  const stored = storageGet("project_snow:world_session_id", "");
  state.worldSessionId = stored || newWorldSessionId();
  storageSet("project_snow:world_session_id", state.worldSessionId);
  return state.worldSessionId;
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
      thinkingMode: storageGet(`project_snow:thinking:${characterId}`, "auto"),
      voiceReply: storageGet(`project_snow:voice:${characterId}`, "false") === "true",
      actionExpanded: false,
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
  const expanded = Boolean(inPerson && thread.actionExpanded);
  actionField.hidden = !expanded;
  byId("toggle-action").setAttribute("aria-expanded", String(expanded));
  speechInput.placeholder = inPerson ? "对她说……" : "发送一条文字通讯…";
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
  const app = byId("chat-app");
  if (channel !== "in_person" && state.stageUiHidden) setStageUiHidden(false);
  app.dataset.channel = channel;
  document.body.dataset.channel = channel;
  byId("text-surface").hidden = channel !== "text";
  byId("in-person-surface").hidden = channel !== "in_person";
  byId("floating-feedback").hidden = state.surface === "immersive" && channel === "in_person";
  updateComposerFields();
}

function renderHeader() {
  const character = currentCharacter();
  const thread = currentThread();
  const target = byId("active-character");
  if (!character) {
    target.innerHTML = `${avatarMarkup(null, "large")}<div><h1>选择角色</h1><p>从左侧开始一段通讯</p></div>`;
  } else {
    target.innerHTML = `${avatarMarkup(character, "large")}<div><h1>${escapeHtml(character.character_name)}</h1><p>通讯中 · 文字通讯</p></div>`;
  }
  const scene = state.sceneByCharacter.get(character?.character_id) || {};
  byId("go-in-person-label").textContent = scene.character_location
    ? `去见她 · ${scene.character_location}` : "去见她";
  updateControls();
  renderStage();
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
      if (thread.worldSessionId) {
        state.worldSessionId = thread.worldSessionId;
        storageSet("project_snow:world_session_id", state.worldSessionId);
      }
      const latestAssistant = [...thread.messages].reverse().find((item) => item.role === "assistant");
      thread.latestResult = latestAssistant?.result || null;
      state.infoResult = thread.latestResult;
      thread.messages.filter((item) => item.role === "assistant" && item.result?.agent_run_id && !["succeeded", "failed", "cancelled"].includes(item.result?.agent_status)).forEach((item) => monitorAgentRun(item, thread));
      await loadQuestions(thread);
    }
    await resolveScene(characterId);
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

function messageHtml(message, character, { transcript = false } = {}) {
  const modeLabel = MODE_LABELS[message.mode] || message.mode;
  const channelLabel = CHANNEL_LABELS[message.channel] || message.channel;
  const statusClass = message.status === "failed" ? " failed" : message.status === "sending" ? " sending" : "";
  const label = message.role === "user" ? "你" : character.character_name;
  const timeLabel = formatTime(message.created_at);
  const metaPrefix = transcript
    ? `<span>${escapeHtml(label)}</span><span>${escapeHtml(modeLabel)}</span><span>${escapeHtml(channelLabel)}</span>`
    : `<span>${escapeHtml(label)}</span>${timeLabel ? `<span>${escapeHtml(timeLabel)}</span>` : ""}`;
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
    return `<article class="message user ${escapeHtml(message.channel)}${isActionOnly ? " analyst-action-message" : ""}${statusClass}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta">${metaPrefix}${kindLabel}${status}</div>${renderedBlocks}${attachments}${retry}</article>`;
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
  const analysisProcess = message.mode === "assistant" && result.analysis_process && typeof result.analysis_process === "object"
    ? result.analysis_process : null;
  const traceSummary = message.mode === "assistant" ? String(result.work_summary || "").trim() : "";
  const traceSteps = message.mode === "assistant" && Array.isArray(result.work_steps) ? result.work_steps.filter(Boolean).slice(0, 5) : [];
  const toolCalls = message.mode === "assistant" && Array.isArray(result.tool_calls) ? result.tool_calls : [];
  const analysisSections = Array.isArray(analysisProcess?.sections)
    ? analysisProcess.sections.filter((item) => item && item.title && item.content).slice(0, 7) : [];
  const analysisOverview = String(analysisProcess?.overview || traceSummary || "").trim();
  const legacySections = !analysisSections.length && traceSteps.length
    ? traceSteps.map((step, index) => ({ title: `处理步骤 ${index + 1}`, content: String(step) })) : [];
  const visibleSections = analysisSections.length ? analysisSections : legacySections;
  const traceTitle = String(analysisProcess?.title || `${character.character_name}的分析过程`).trim();
  const trace = analysisProcess || traceSummary || traceSteps.length || toolCalls.length
    ? `<details class="work-trace analysis-trace"><summary><span><i data-lucide="sparkles"></i>${escapeHtml(traceTitle)}</span><small>${toolCalls.length ? `已使用 ${toolCalls.length} 项工具` : "展开查看"}</small></summary><div class="analysis-trace-body"><p class="analysis-disclosure">${escapeHtml(analysisProcess?.disclosure || "这是面向你的可核验分析说明，不包含系统提示或模型隐藏推理。")}</p>${analysisOverview ? `<p class="analysis-overview">${escapeHtml(analysisOverview)}</p>` : ""}${visibleSections.map((section) => `<section><h4>${escapeHtml(String(section.title))}</h4><p>${escapeHtml(String(section.content))}</p></section>`).join("")}${toolCalls.length ? `<div class="tool-trace">${toolCalls.map((call) => `<span class="tool-chip ${call.status === "failed" ? "failed" : ""}">${escapeHtml(TOOL_LABELS[call.name] || call.name || "只读工具")} · ${call.status === "completed" ? "完成" : "未完成"}</span>`).join("")}</div>` : ""}</div></details>`
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
  const thinking = result.thinking_decision || result.routing_decision?.thinking_decision || {};
  const thinkingText = thinking.effective
    ? `Thinking ${thinking.effective === "on" ? "已启用" : thinking.effective === "off" ? "已关闭" : "等待路由"} · ${thinking.reason || "策略决定"}`
    : "";
  const modelMeta = message.mode === "assistant" && result.actual_model?.model_name
    ? `<details class="model-meta"><summary>模型、路由与用量${result.routing_decision?.fallback ? " · 已回退" : ""}</summary><p>${escapeHtml(result.actual_model.provider_name || result.actual_model.provider_id || "模型")} · ${escapeHtml(result.actual_model.model_name)}</p><p>${escapeHtml(routingReason)} · ${escapeHtml(usageText)}</p>${thinkingText ? `<p>${escapeHtml(thinkingText)}</p>` : ""}</details>`
    : "";
  const audio = result.audio?.status === "completed" ? `<audio class="voice-reply" controls preload="metadata" src="${escapeHtml(result.audio.content_url)}"></audio>` : "";
  return `<article class="message assistant ${escapeHtml(message.channel)}" data-message-id="${escapeHtml(message.id)}"><div class="message-meta">${metaPrefix}</div>${blocks}${trace}${agentCard}${audio}${modelMeta}<div class="message-actions"><details class="message-menu"><summary aria-label="消息操作"><i data-lucide="more-horizontal"></i></summary><div><button type="button" data-message-info="${escapeHtml(message.id)}">查看依据</button><button type="button" data-message-feedback="${escapeHtml(message.id)}">反馈</button></div></details></div></article>`;
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
    html.push(`<div class="empty-conversation"><div><strong>给${escapeHtml(character.character_name)}发一条消息</strong><p>当前使用基地通讯器</p>${questions ? `<div class="presence-actions">${questions}</div>` : ""}</div></div>`);
  } else {
    let hiddenInPersonCount = 0;
    const flushInPersonMemory = () => {
      if (!hiddenInPersonCount) return;
      html.push(`<div class="channel-memory-card"><i data-lucide="users"></i><span>期间有 ${hiddenInPersonCount} 条面对面互动，可在“回看”中查看</span></div>`);
      hiddenInPersonCount = 0;
    };
    thread.messages.forEach((message) => {
      if (message.channel === "in_person") {
        hiddenInPersonCount += 1;
        return;
      }
      flushInPersonMemory();
      html.push(messageHtml(message, character));
    });
    flushInPersonMemory();
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
  renderTranscript();
  renderStage();
}

function renderTranscript() {
  const target = byId("transcript-content");
  const character = currentCharacter();
  const thread = currentThread();
  if (!character || !thread) {
    target.innerHTML = '<div class="empty-conversation"><p>选择角色后回看完整记录。</p></div>';
    return;
  }
  if (!thread.messages.length) {
    target.innerHTML = `<div class="empty-conversation"><p>尚未与${escapeHtml(character.character_name)}产生对话记录。</p></div>`;
    return;
  }
  const html = [];
  let previousChannel = null;
  thread.messages.forEach((message) => {
    if (previousChannel && previousChannel !== message.channel) {
      html.push(`<div class="timeline-divider"><span>转为${escapeHtml(CHANNEL_LABELS[message.channel] || message.channel)}</span></div>`);
    }
    html.push(messageHtml(message, character, { transcript: true }));
    previousChannel = message.channel;
  });
  target.innerHTML = html.join("");
  renderIcons();
}

function latestAssistantMessage(channel = null) {
  const messages = currentThread()?.messages || [];
  return [...messages].reverse().find((item) => (
    item.role === "assistant" && (!channel || item.channel === channel)
  )) || null;
}

function headerPortraitInnerHtml(character) {
  const source = character?.avatar?.src;
  if (source && character?.avatar?.publishable !== false) {
    return `<img src="${escapeHtml(source)}" alt="" />`;
  }
  return escapeHtml((character?.character_name || "?").slice(0, 1));
}

function stagePortraitInnerHtml(character) {
  const source = character?.avatar?.src;
  if (source && character?.avatar?.publishable !== false) {
    return `<img src="${escapeHtml(source)}" alt="" loading="eager" />`;
  }
  return escapeHtml((character?.character_name || "?").slice(0, 1));
}

function clearStageRevealTimer() {
  if (state.stageReveal.timer) window.clearInterval(state.stageReveal.timer);
  state.stageReveal.timer = null;
}

function setStageSpeech(text) {
  byId("stage-speech").innerHTML = text
    ? `<p>${escapeHtml(text)}</p>`
    : "<p></p>";
}

function completeStageReveal() {
  if (state.stageReveal.completed) return false;
  clearStageRevealTimer();
  state.stageReveal.visibleCharacters = state.stageReveal.fullText.length;
  state.stageReveal.completed = true;
  const message = currentThread()?.messages.find((item) => item.id === state.stageReveal.messageId);
  if (message) message.fresh = false;
  setStageSpeech(state.stageReveal.fullText);
  byId("stage-dialogue").classList.remove("is-revealing");
  return true;
}

function beginStageReveal(messageId, fullText, fresh, delay = 0) {
  clearStageRevealTimer();
  state.stageReveal = {
    messageId,
    fullText,
    visibleCharacters: fresh ? 0 : fullText.length,
    completed: !fresh,
    timer: null,
  };
  if (!fresh || !fullText || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    state.stageReveal.completed = true;
    state.stageReveal.visibleCharacters = fullText.length;
    setStageSpeech(fullText);
    byId("stage-dialogue").classList.remove("is-revealing");
    const message = currentThread()?.messages.find((item) => item.id === messageId);
    if (message) message.fresh = false;
    return;
  }
  setStageSpeech("");
  byId("stage-dialogue").classList.add("is-revealing");
  const startTyping = () => {
    state.stageReveal.timer = window.setInterval(() => {
      state.stageReveal.visibleCharacters = Math.min(fullText.length, state.stageReveal.visibleCharacters + 1);
      setStageSpeech(fullText.slice(0, state.stageReveal.visibleCharacters));
      if (state.stageReveal.visibleCharacters >= fullText.length) completeStageReveal();
    }, 24);
  };
  state.stageReveal.timer = delay ? window.setTimeout(startTyping, delay) : (startTyping(), state.stageReveal.timer);
}

function setStageUiHidden(hidden) {
  state.stageUiHidden = Boolean(hidden);
  byId("chat-app").classList.toggle("stage-ui-hidden", state.stageUiHidden);
  byId("restore-stage-ui").hidden = !state.stageUiHidden;
  byId("toggle-stage-ui").setAttribute("aria-pressed", String(state.stageUiHidden));
  byId("toggle-stage-ui").setAttribute("aria-label", state.stageUiHidden ? "显示界面" : "隐藏界面");
  if (!state.stageUiHidden && currentThread()?.channel === "in_person") {
    requestAnimationFrame(() => byId("stage-dialogue").focus({ preventScroll: true }));
  }
}

function renderStage() {
  const character = currentCharacter();
  const thread = currentThread();
  const surface = byId("in-person-surface");
  if (!character || !thread) {
    byId("stage-character-name").textContent = "选择角色";
    byId("stage-location").textContent = "场景尚未建立";
    byId("stage-activity").textContent = "选择角色后读取当前位置";
    byId("scene-backdrop").src = "/assets/immersive/scenes/generic.svg";
    byId("stage-portrait-avatar").textContent = "?";
    byId("stage-speech").innerHTML = "<p>选择一位角色，开始一段面对面的对话。</p>";
    return;
  }
  const scene = state.sceneByCharacter.get(character.character_id) || {};
  const visualKey = SCENE_KEYS.has(scene.visual_key) ? scene.visual_key : "generic";
  surface.dataset.scene = visualKey;
  byId("scene-backdrop").src = `/assets/immersive/scenes/${visualKey}.svg`;
  byId("stage-character-name").textContent = character.character_name;
  byId("stage-header-avatar").innerHTML = headerPortraitInnerHtml(character);
  byId("stage-location").textContent = scene.character_location || "当前位置尚未建立";
  byId("stage-activity").textContent = scene.character_activity || "她正在这里";

  const latest = latestAssistantMessage("in_person");
  const blocks = latest ? messageBlocks(latest) : [];
  const actions = blocks.filter((block) => block.type === "action").map((block) => String(block.text || "").trim()).filter(Boolean);
  const speeches = blocks.filter((block) => block.type !== "action").map((block) => String(block.text || "").trim()).filter(Boolean);
  const narration = byId("stage-narration");
  narration.textContent = actions.join("\n") || (latest ? "" : (scene.character_activity || ""));
  narration.classList.remove("scene-reveal");
  void narration.offsetWidth;
  if (narration.textContent) narration.classList.add("scene-reveal");
  const portrait = byId("stage-portrait");
  const portraitKind = character.avatar?.portrait_kind === "full_body" ? "full_body" : "headshot";
  portrait.dataset.portraitKind = portraitKind;
  portrait.setAttribute("aria-label", `${character.character_name}头像`);
  portrait.style.setProperty("--portrait-scale", String(Number(character.avatar?.portrait_scale || (portraitKind === "full_body" ? 1.8 : 1))));
  portrait.style.setProperty("--portrait-focus-x", `${Number(character.avatar?.portrait_focus_x ?? 50)}%`);
  portrait.style.setProperty("--portrait-focus-y", `${Number(character.avatar?.portrait_focus_y ?? (portraitKind === "full_body" ? 22 : 50))}%`);
  byId("stage-portrait-avatar").innerHTML = stagePortraitInnerHtml(character);
  byId("stage-speaker").textContent = latest ? character.character_name : "此刻";
  const fullSpeech = speeches.join("\n\n") || (scene.co_located ? "你已经来到她身边。可以先开口。" : "前往她所在的地点后，才能开始面对面对话。");
  const revealId = latest?.id || `scene:${visualKey}:${scene.co_located ? "together" : "apart"}`;
  if (state.stageReveal.messageId !== revealId) beginStageReveal(revealId, fullSpeech, Boolean(latest?.fresh), actions.length ? 220 : 80);
  else if (state.stageReveal.completed) setStageSpeech(fullSpeech);
  byId("stage-channel-state").textContent = scene.co_located ? "同处一地" : "尚未同处";
  const status = byId("stage-presence-status");
  status.hidden = !(thread.pending?.sending && thread.pending.channel === "in_person") && !state.transitionPending;
  status.textContent = state.transitionPending
    ? "正在切换场景…"
    : `${character.character_name}正在回应你…`;
  surface.classList.toggle("is-waiting", !status.hidden);
  const arrivalLoading = byId("presence-arrival-loading");
  if (arrivalLoading) {
    const active = Boolean(state.arrivalPending && state.arrivalPending.characterId === character.character_id);
    arrivalLoading.hidden = !active;
    arrivalLoading.setAttribute("aria-hidden", String(!active));
  }
  byId("stage-message-info").disabled = !latest;
  byId("stage-message-feedback").disabled = !latest;
  byId("stage-message-info").dataset.messageInfo = latest?.id || "";
  byId("stage-message-feedback").dataset.messageFeedback = latest?.id || "";
  renderIcons();
}

function arrivalId() {
  if (window.crypto?.randomUUID) return `arrival_${window.crypto.randomUUID()}`;
  return `arrival_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function arrivalMessage(result, thread) {
  const reaction = result?.reaction;
  if (!reaction?.message_id || !reaction.answer) return;
  const message = {
    id: reaction.message_id,
    role: "assistant",
    mode: "immersive",
    channel: "in_person",
    text: reaction.answer,
    blocks: reaction.content_blocks || [{ type: "speech", text: reaction.answer }],
    result: { ...reaction, source: "presence_arrival", arrival_id: result.arrival_id },
    clientMessageId: "",
    created_at: new Date().toISOString(),
    status: "sent",
    fresh: true,
  };
  if (!thread.messages.some((item) => item.id === message.id)) thread.messages.push(message);
}

async function arriveInPerson(characterId = state.selectedCharacterId) {
  const character = state.characterMap.get(characterId);
  if (!character || state.arrivalPending) return false;
  const thread = getThread(characterId);
  const startedAt = performance.now();
  const currentArrivalId = arrivalId();
  state.arrivalPending = { characterId, arrivalId: currentArrivalId, startedAt, phase: "transitioning" };
  state.transitionPending = true;
  byId("presence-dialog").close();
  thread.channel = "in_person";
  storageSet(`project_snow:channel:${thread.characterId}`, "in_person");
  renderHeader();
  renderTimeline(false);
  let result = null;
  try {
    state.arrivalPending.phase = "resolving";
    result = await api("/api/v1/mvp/presence/arrival", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: characterId,
        arrival_id: currentArrivalId,
        session_id: thread.sessionId || null,
        world_session_id: ensureWorldSessionId(),
      }),
    }, 135000);
    if (state.arrivalPending?.arrivalId !== currentArrivalId) return false;
    const elapsed = performance.now() - startedAt;
    if (result.decision !== "noticed" || result.status !== "completed") {
      await new Promise((resolve) => window.setTimeout(resolve, Math.max(0, 1000 - elapsed)));
    }
    state.worldSessionId = result.world_session_id || state.worldSessionId;
    thread.sessionId = result.session_id || thread.sessionId;
    thread.conversationId = result.conversation_id || thread.conversationId;
    thread.worldSessionId = result.world_session_id || thread.worldSessionId;
    state.sceneByCharacter.set(characterId, result.scene_state || {});
    state.arrivalPending.phase = "completed";
  } catch (error) {
    showToast(`前往失败：${error.message}`);
    if (state.arrivalPending?.arrivalId === currentArrivalId) state.arrivalPending = null;
    state.transitionPending = false;
    renderStage();
    return false;
  }
  if (state.arrivalPending?.arrivalId !== currentArrivalId) return false;
  state.arrivalPending = null;
  state.transitionPending = false;
  // Paint the settled scene without the loading card before inserting the
  // proactive reply. This keeps its typewriter reveal from running behind the
  // translucent loader and appearing to play twice.
  renderStage();
  await new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
  arrivalMessage(result, thread);
  if (result.decision === "noticed") showToast(`${character.character_name}发现你来了。`);
  else if (result.status === "fallback_unnoticed") showToast(`${character.character_name}暂时没有注意到你。`);
  else showToast(`${character.character_name}还在忙自己的事情。`);
  renderCharacterList(); renderHeader(); renderTimeline(false); renderInfo();
  return true;
}

async function resolveScene(characterId) {
  if (!characterId) return null;
  try {
    const result = await api("/api/v1/mvp/presence/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: characterId,
        world_session_id: ensureWorldSessionId(),
      }),
    }, 15000);
    state.worldSessionId = result.world_session_id || state.worldSessionId;
    storageSet("project_snow:world_session_id", state.worldSessionId);
    state.sceneByCharacter.set(characterId, result.scene_state || {});
    return result.scene_state || {};
  } catch (error) {
    showToast(`场景读取失败：${error.message}`);
    return null;
  }
}

function openPresenceDialog(characterId = state.selectedCharacterId) {
  const character = state.characterMap.get(characterId);
  if (!character) return;
  const scene = state.sceneByCharacter.get(characterId) || {};
  state.presenceDialogCharacterId = characterId;
  byId("presence-dialog-title").textContent = `去见${character.character_name}`;
  byId("presence-dialog-location").textContent = scene.character_location || "正在读取她的位置";
  byId("presence-dialog-activity").textContent = scene.character_activity || "场景建立后即可前往。";
  byId("presence-dialog-message").textContent = scene.co_located
    ? "你们已经在同一地点，可以直接进入面对面场景。"
    : "前往后会更新你在当前世界会话中的位置，并进入面对面场景。";
  byId("confirm-presence-transition").textContent = scene.co_located ? "进入面对面" : "前往";
  byId("presence-dialog").showModal();
  renderIcons();
}

async function transitionPresence(targetChannel, characterId = state.selectedCharacterId) {
  const character = state.characterMap.get(characterId);
  if (!character || state.transitionPending) return false;
  const thread = getThread(characterId);
  if (state.arrivalPending) state.arrivalPending = null;
  state.transitionPending = true;
  renderStage();
  try {
    const result = await api("/api/v1/mvp/presence/transition", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: characterId,
        session_id: thread.sessionId || null,
        world_session_id: ensureWorldSessionId(),
        target_channel: targetChannel,
        action: targetChannel === "in_person" ? "join_character" : "open_communicator",
      }),
    }, 15000);
    state.worldSessionId = result.world_session_id || state.worldSessionId;
    storageSet("project_snow:world_session_id", state.worldSessionId);
    thread.worldSessionId = state.worldSessionId;
    thread.channel = result.communication_channel || targetChannel;
    if (thread.channel !== "in_person") thread.actionExpanded = false;
    storageSet(`project_snow:channel:${thread.characterId}`, thread.channel);
    state.sceneByCharacter.set(characterId, result.scene_state || {});
    if (state.selectedCharacterId !== characterId) state.selectedCharacterId = characterId;
    byId("presence-dialog").close();
    renderCharacterList();
    renderHeader();
    restoreDraft();
    renderTimeline(false);
    renderInfo();
    closeDrawers();
    showToast(thread.channel === "in_person"
      ? `已来到${result.scene_state?.character_location || character.character_name}身边。`
      : "已打开通讯器，当前位置保持不变。");
    requestAnimationFrame(() => byId("message-input").focus());
    return true;
  } catch (error) {
    showToast(`场景切换失败：${error.message}`);
    return false;
  } finally {
    state.transitionPending = false;
    renderStage();
  }
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
  if (thread && actionInput.value) thread.actionExpanded = true;
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
  renderStage();
}

async function selectCharacter(characterId) {
  if (!state.characterMap.has(characterId)) return;
  if (state.arrivalPending && state.arrivalPending.characterId !== characterId) {
    state.arrivalPending = null;
    state.transitionPending = false;
  }
  const switchingFromStage = Boolean(
    state.selectedCharacterId
    && state.selectedCharacterId !== characterId
    && currentThread()?.channel === "in_person"
  );
  saveDraft();
  state.selectedCharacterId = characterId;
  storageSet(`project_snow:selected_character:${state.mode}`, characterId);
  const thread = getThread(characterId);
  const scene = await resolveScene(characterId);
  const needsPresenceChoice = Boolean(
    scene && !scene.co_located && (thread.channel === "in_person" || switchingFromStage)
  );
  if (needsPresenceChoice) {
    thread.channel = "text";
    thread.actionExpanded = false;
    storageSet(`project_snow:channel:${thread.characterId}`, "text");
  }
  byId("model-override").value = thread.modelOverride || "";
  byId("thinking-mode").value = thread.thinkingMode || "auto";
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
  const refreshedScene = state.sceneByCharacter.get(characterId) || scene;
  const unresolvedFaceChannel = Boolean(
    thread.channel === "in_person" && refreshedScene && !refreshedScene.co_located
  );
  if (unresolvedFaceChannel) {
    thread.channel = "text";
    thread.actionExpanded = false;
    storageSet(`project_snow:channel:${thread.characterId}`, "text");
    renderHeader();
    restoreDraft();
    renderTimeline(false);
  }
  if (needsPresenceChoice || unresolvedFaceChannel) openPresenceDialog(characterId);
}

function setChannel(channel) {
  const thread = currentThread();
  if (!thread || !CHANNEL_LABELS[channel] || thread.channel === channel) return;
  saveDraft();
  if (channel === "in_person") openPresenceDialog(thread.characterId);
  else transitionPresence("text", thread.characterId);
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
      message.result.thinking_decision = snapshot.state?.routing_decision?.thinking_decision || message.result.thinking_decision || {};
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
    modelOverride: state.mode === "assistant" ? byId("model-override").value : "",
    thinkingMode: byId("thinking-mode").value,
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
  if (thread.channel === "in_person") thread.actionExpanded = false;
  saveDraft();
  updateComposerFields();
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
        thinking_mode: pending.thinkingMode || "auto",
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
    if (result.scene_state) state.sceneByCharacter.set(thread.characterId, result.scene_state);
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
      fresh: pending.channel === "in_person" || (pending.channel === "text" && (result.content_blocks || []).length > 1),
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
      if (error.detail.scene_state) state.sceneByCharacter.set(thread.characterId, error.detail.scene_state);
      thread.channel = "text";
      thread.actionExpanded = false;
      storageSet(`project_snow:channel:${thread.characterId}`, "text");
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
    thread.channel = "in_person";
    storageSet(`project_snow:channel:${thread.characterId}`, "in_person");
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
  const scene = result.scene_state || state.sceneByCharacter.get(character.character_id) || {};
  const style = result.style_context || {};
  const citations = result.citations || [];
  const webSources = Array.isArray(result.web_sources) ? result.web_sources : [];
  const toolCalls = state.mode === "assistant" && Array.isArray(result.tool_calls) ? result.tool_calls : [];
  const sceneText = scene.co_located
    ? `双方同处：${scene.character_location || "未定位"}`
    : `你：${scene.analyst_location || "未定位"}；角色：${scene.character_location || "未定位"}`;
  let styleText = "角色本体设定优先";
  if (style.status === "active") styleText = style.kind === "costume" ? `时装：${style.costume_name || "已识别"}` : `装甲：${style.armor_name || "已识别"}`;
  else if (style.status === "ambiguous") styleText = "检测到多个语境，本轮未自动启用";
  target.innerHTML = `
    <section class="info-section"><h3>资料覆盖</h3><p>${escapeHtml(coverage.label || "资料覆盖状态未知")}</p><div class="info-metrics"><div class="info-metric"><strong>${Number(coverage.direct_document_count || 0)}</strong><span>直接资料</span></div><div class="info-metric"><strong>${Number(coverage.linked_document_count || 0)}</strong><span>关联资料</span></div><div class="info-metric"><strong>${Number(coverage.address_term_count || 0)}</strong><span>称呼证据</span></div><div class="info-metric"><strong>${Number(coverage.voice_evidence_count || 0)}</strong><span>语气证据</span></div></div></section>
    <section class="info-section"><h3>当前场景</h3><p>${escapeHtml(CHANNEL_LABELS[thread.channel])}</p><p>${escapeHtml(sceneText)}</p></section>
    <section class="info-section"><h3>装甲 / 时装语境</h3><p>${escapeHtml(styleText)}</p></section>
    <section class="info-section"><h3>角色视觉素材</h3><p>${escapeHtml(character.avatar?.license || "当前使用界面回退肖像")}</p>${character.avatar?.source_page ? `<a class="source-link" href="${escapeHtml(character.avatar.source_page)}" target="_blank" rel="noreferrer">查看素材来源与页面说明</a>` : ""}</section>
    <section class="info-section"><h3>本条回答依据</h3>${citations.length ? citations.map((item) => `<article class="citation"><strong>${escapeHtml(item.source_type || "资料")} · ${escapeHtml(item.title || "未命名来源")}</strong><blockquote>${escapeHtml(item.excerpt || "")}</blockquote></article>`).join("") : "<p>当前未选择带引用的回答。</p>"}${webSources.length ? `<h4 class="info-subheading">联网参考</h4>${webSources.map((item) => `<article class="citation"><strong>${escapeHtml(item.title || "网页")}</strong><a class="source-link" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(item.url || "")}</a><blockquote>${escapeHtml(item.snippet || "")}</blockquote></article>`).join("")}` : ""}${toolCalls.length ? `<p class="tool-note">本轮只读工具：${toolCalls.map((item) => `${escapeHtml(item.name || "工具")}（${item.status === "completed" ? "完成" : "未完成"}）`).join("、")}</p>` : ""}</section>`;
}

function openInfo(result = null) {
  if (result) state.infoResult = result;
  renderInfo();
  byId("info-panel").classList.add("open");
  byId("info-panel").setAttribute("aria-hidden", "false");
  byId("drawer-scrim").hidden = false;
}

function openTranscript() {
  renderTranscript();
  byId("transcript-panel").classList.add("open");
  byId("transcript-panel").setAttribute("aria-hidden", "false");
  byId("drawer-scrim").hidden = false;
}

function closeDrawers() {
  byId("contact-panel").classList.remove("open");
  byId("info-panel").classList.remove("open");
  byId("transcript-panel").classList.remove("open");
  byId("info-panel").setAttribute("aria-hidden", "true");
  byId("transcript-panel").setAttribute("aria-hidden", "true");
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

function updateWindowsEnvGuide() {
  const target = byId("windows-env-code");
  if (!target) return;
  const preset = PROVIDER_PRESETS[byId("provider-kind").value] || PROVIDER_PRESETS["openai-compatible"];
  const baseUrl = byId("provider-url").value.trim() || preset.baseUrl || "<兼容接口 Base URL>";
  const modelName = byId("provider-model-select").value
    || byId("provider-model").value.trim()
    || "<从厂商列表选择的模型 ID>";
  const quote = (value) => String(value).replaceAll('"', '`"');
  target.textContent = [
    "# 仅在当前 PowerShell 窗口生效（建议先这样测试）",
    '$env:MVP_CHAT_ENABLED = "true"',
    '$env:MVP_CHAT_PROVIDER = "openai-compatible"',
    `$env:MVP_CHAT_BASE_URL = "${quote(baseUrl)}"`,
    '$env:MVP_CHAT_API_KEY = "<粘贴你的 API Key>"',
    `$env:MVP_CHAT_MODEL = "${quote(modelName)}"`,
    "",
    "# 然后在同一个 PowerShell 窗口中重启 API",
    "python -m uvicorn backend.snow_app.main:app --host 127.0.0.1 --port 8000",
    "",
    "# 如需长期写入当前 Windows 用户环境（执行后新开 PowerShell）",
    `[Environment]::SetEnvironmentVariable("MVP_CHAT_BASE_URL", "${quote(baseUrl)}", "User")`,
    '[Environment]::SetEnvironmentVariable("MVP_CHAT_API_KEY", "<粘贴你的 API Key>", "User")',
    `[Environment]::SetEnvironmentVariable("MVP_CHAT_MODEL", "${quote(modelName)}", "User")`,
    '[Environment]::SetEnvironmentVariable("MVP_CHAT_ENABLED", "true", "User")',
  ].join("\n");
}

function selectProvider(kind, resetModel = true) {
  const preset = PROVIDER_PRESETS[kind] || PROVIDER_PRESETS["openai-compatible"];
  const custom = kind === "openai-compatible";
  byId("provider-kind").value = kind;
  byId("provider-name").value = preset.name;
  byId("provider-url").value = preset.baseUrl;
  byId("provider-model").placeholder = preset.modelHint;
  if (resetModel) byId("provider-model").value = "";
  byId("provider-key").value = "";
  byId("provider-url-field").hidden = !custom;
  byId("provider-model-field").hidden = !custom;
  document.querySelectorAll("[data-provider-choice]").forEach((button) => {
    const active = button.dataset.providerChoice === kind;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const providerId = custom ? "custom-ui" : kind;
  const configured = state.providers.find((item) => item.provider_id === providerId)?.configured;
  byId("provider-key").placeholder = configured
    ? "已配置；留空可沿用 Windows 凭据库中的 Key"
    : "仅发送到本地服务，不会回显";
  byId("provider-status").textContent = configured
    ? `${preset.shortName} 已配置；可重新读取模型或更新 API Key。`
    : `${preset.shortName} 已选中；填写 API Key 后读取模型。`;
  renderProviderModelPicker();
  updateWindowsEnvGuide();
}

function activeProviderId() {
  const kind = byId("provider-kind").value;
  return kind === "openai-compatible" ? "custom-ui" : kind;
}

function modelValue(item) {
  return `${item.provider_id}::${item.model_name}`;
}

function modelStateLabel(item) {
  if (item.text_status === "ready") return "文本可用";
  if (item.text_status === "failed") return "文本验证失败";
  return "已发现 · 待验证";
}

function renderProviderModelPicker() {
  const picker = byId("provider-model-select");
  if (!picker) return;
  const current = picker.value;
  const models = state.models.filter((item) => item.provider_id === activeProviderId() && item.selectable !== false);
  const categoryLabels = { recommended: "推荐", other_text: "其他文本模型", unknown_purpose: "用途待确认" };
  const groups = new Map();
  models.forEach((item) => {
    const category = item.probe?.category || "other_text";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(item);
  });
  const options = [];
  ["recommended", "other_text", "unknown_purpose"].forEach((category) => {
    const items = groups.get(category) || [];
    if (!items.length) return;
    options.push(`<optgroup label="${categoryLabels[category]}">${items.map((item) => `<option value="${escapeHtml(item.model_name)}">${escapeHtml(item.model_name)} · ${escapeHtml(modelStateLabel(item))}</option>`).join("")}</optgroup>`);
  });
  picker.innerHTML = options.join("") || '<option value="">保存 API Key 后读取模型</option>';
  if ([...picker.options].some((option) => option.value === current)) picker.value = current;
  updateWindowsEnvGuide();
}

function renderProviderChoices() {
  const configured = new Set(state.providers.filter((item) => item.configured).map((item) => item.provider_id));
  document.querySelectorAll("[data-provider-choice]").forEach((button) => {
    const providerId = button.dataset.providerChoice === "openai-compatible" ? "custom-ui" : button.dataset.providerChoice;
    button.classList.toggle("configured", configured.has(providerId));
    button.title = configured.has(providerId) ? "已保存到 Windows 凭据库" : "尚未保存";
  });
}

async function loadProviders() {
  try {
    const result = await api("/api/v1/providers", {}, 30000);
    state.providers = result.providers || [];
    renderProviderChoices();
    selectProvider(byId("provider-kind").value, false);
  } catch (_) {
    state.providers = [];
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
    config: {},
  };
}

async function configureProvider() {
  const status = byId("provider-status");
  try {
    const payload = providerPayload();
    if (!payload.base_url) throw new Error("请填写 Provider Base URL。");
    const existing = state.providers.find((item) => item.provider_id === payload.provider_id);
    if (!payload.api_key && !existing?.configured) throw new Error("请填写该厂商的 API Key。");
    status.textContent = "正在保存本地 Provider 配置…";
    const saved = await api("/api/v1/providers", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }, 30000);
    byId("provider-key").value = "";
    status.textContent = "正在从厂商读取当前账户可用模型…";
    const discovery = await api(`/api/v1/providers/${encodeURIComponent(saved.provider_id)}/discover-models`, {
      method: "POST",
    }, 60000);
    await Promise.all([loadModels(), loadProviders()]);
    renderProviderModelPicker();
    if (discovery.status === "failed") {
      status.textContent = `${discovery.error}${discovery.stale ? " 已保留缓存模型。" : ""}`;
      const manual = byId("provider-model").value.trim();
      if (payload.kind === "openai-compatible" && manual) {
        byId("provider-model-select").innerHTML = `<option value="${escapeHtml(manual)}">${escapeHtml(manual)} · 手动模型</option>`;
      }
      return;
    }
    const selectedModel = byId("provider-model-select").value;
    status.textContent = `已读取 ${discovery.models?.length || 0} 个模型；模型现在即可选择，附加能力验证不会阻止文本使用。`;
    if (selectedModel) {
      await ensureInitialModelDefaults(saved.provider_id, selectedModel);
      verifySelectedProviderModel({ background: true });
    }
  } catch (error) {
    status.textContent = `配置失败：${error.message}`;
  }
}

async function verifySelectedProviderModel({ background = false } = {}) {
  const status = byId("provider-status");
  const providerId = activeProviderId();
  const model = byId("provider-model-select").value || byId("provider-model").value.trim();
  if (!model) {
    if (!background) status.textContent = "请先从厂商列表选择模型。";
    return;
  }
  if (!background) status.textContent = "正在验证文本、结构化输出和流式能力…";
  try {
    const result = await api(`/api/v1/providers/${encodeURIComponent(providerId)}/probe`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_name: model,
        quality_score: 50,
        capabilities: { structured_output: true, native_tool_calling: false, streaming: true },
      }),
    }, 120000);
    await loadModels();
    status.textContent = result.capabilities?.structured_output
      ? "文本连接成功；结构化输出可用，其他能力按各自验证结果启用。"
      : "文本连接成功；结构化输出未通过，但该模型仍可用于普通文本。";
  } catch (error) {
    await loadModels();
    status.textContent = `文本验证失败：${error.message}。模型选择会保留，修正 Key、权限或余额后可重试。`;
  }
}

async function ensureInitialModelDefaults(providerId, modelName) {
  if (Object.keys(state.modelDefaults || {}).length) return;
  const value = { provider_id: providerId, model_name: modelName };
  try {
    const result = await api("/api/v1/models/defaults", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ immersive_text: value, assistant_text: value, assistant_agent: value }),
    });
    state.modelDefaults = result.defaults || {};
    renderModelDefaults();
  } catch (_) {
    // Discovery remains useful even when a vendor model is not Agent-ready.
  }
}

async function loadModels() {
  try {
    const result = await api("/api/v1/models", {}, 30000);
    state.models = result.models || [];
    state.modelDefaults = result.defaults || {};
    const selectable = state.models.filter((item) => item.selectable !== false && item.capabilities?.text === true);
    const picker = byId("model-override");
    const selected = currentThread()?.modelOverride || picker.value;
    picker.innerHTML = '<option value="">使用助手默认模型</option>' + selectable.map((item) => `<option value="${escapeHtml(modelValue(item))}">${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)} · ${escapeHtml(modelStateLabel(item))}</option>`).join("");
    picker.value = [...picker.options].some((option) => option.value === selected) ? selected : "";
    const readyCount = selectable.filter((item) => item.text_status === "ready").length;
    byId("active-model").textContent = selectable.length
      ? `${selectable.length} 个可选模型 · ${readyCount} 个文本已验证`
      : "尚未发现模型 · 可沿用环境配置";
    const capabilityLabels = { text: "文本", structured_output: "结构化", native_tool_calling: "工具", vision: "视觉", speech_to_text: "STT", text_to_speech: "TTS", streaming: "流式" };
    const providerModels = selectable.filter((item) => item.provider_id === activeProviderId());
    byId("model-capability-list").innerHTML = providerModels.map((item) => {
      const enabled = Object.entries(item.capabilities || {}).filter(([, value]) => value === true).map(([key]) => capabilityLabels[key] || key).join(" · ");
      const latency = item.probe?.latency_ms ? `${Math.round(item.probe.latency_ms)} ms` : "延迟未知";
      const stateClass = item.text_status === "ready" ? "ready" : (item.text_status === "failed" ? "failed" : "");
      return `<div><strong>${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)}</strong><small>${escapeHtml(enabled || "文本用途待验证")} · ${escapeHtml(latency)}</small><span class="model-state ${stateClass}">${escapeHtml(modelStateLabel(item))}</span></div>`;
    }).join("") || "<p>该厂商尚未读取到模型。</p>";
    renderProviderModelPicker();
    renderModelDefaults();
  } catch (_) {
    byId("active-model").textContent = "自动路由";
  }
}

function renderModelDefaults() {
  const selectable = state.models.filter((item) => item.selectable !== false && item.capabilities?.text === true);
  const options = '<option value="">沿用兼容默认</option>' + selectable.map((item) => `<option value="${escapeHtml(modelValue(item))}">${escapeHtml(item.provider_name || item.provider_id)} · ${escapeHtml(item.model_name)} · ${escapeHtml(modelStateLabel(item))}</option>`).join("");
  const bindings = [
    ["default-immersive-model", "immersive_text"],
    ["default-assistant-model", "assistant_text"],
    ["default-agent-model", "assistant_agent"],
  ];
  bindings.forEach(([id, key]) => {
    const select = byId(id);
    if (!select) return;
    select.innerHTML = options;
    const value = state.modelDefaults?.[key];
    const serialized = value?.provider_id && value?.model_name ? `${value.provider_id}::${value.model_name}` : "";
    select.value = [...select.options].some((option) => option.value === serialized) ? serialized : "";
  });
}

function parseModelSelection(value) {
  if (!value) return null;
  const separator = value.indexOf("::");
  if (separator < 1) return null;
  return { provider_id: value.slice(0, separator), model_name: value.slice(separator + 2) };
}

async function saveModelDefaults() {
  const status = byId("provider-status");
  const immersive = parseModelSelection(byId("default-immersive-model").value);
  const assistant = parseModelSelection(byId("default-assistant-model").value);
  const agent = parseModelSelection(byId("default-agent-model").value) || assistant;
  if (!immersive || !assistant) {
    status.textContent = "请分别选择沉浸式和助手默认模型。";
    return;
  }
  try {
    const result = await api("/api/v1/models/defaults", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ immersive_text: immersive, assistant_text: assistant, assistant_agent: agent }),
    });
    state.modelDefaults = result.defaults || {};
    renderModelDefaults();
    status.textContent = "默认模型已保存；切换模型不会改变角色记忆、关系或场景。";
  } catch (error) {
    status.textContent = `默认模型保存失败：${error.message}`;
  }
}

function setPluginStatus(message, kind = "") {
  const target = byId("plugin-pairing-status");
  if (target) {
    target.textContent = message;
    target.className = `plugin-status ${kind}`.trim();
  }
}

function renderPluginCharacters() {
  const select = byId("plugin-character");
  if (!select) return;
  const saved = storageGet("project_snow:plugin_default_character", "");
  select.innerHTML = '<option value="">请选择默认角色</option>' + state.characters.map((character) => (
    `<option value="${escapeHtml(character.character_id)}">${escapeHtml(character.character_name)}</option>`
  )).join("");
  if (state.characterMap.has(saved)) select.value = saved;
  else select.value = "";
}

async function loadPersonaGatewayStatus() {
  const indicator = byId("plugin-service-status");
  try {
    const result = await api("/api/v1/persona/status", {}, 15000);
    state.personaGateway = result;
    if (indicator) {
      indicator.className = "connection-pill online";
      indicator.textContent = "人格网关已连接";
    }
    const configured = Boolean(result.codex_credential_configured);
    setPluginStatus(
      configured
        ? `Codex 已配对 · 公共知识 ${result.knowledge?.knowledge_version || "版本未知"} · 当前 ${result.active_pairing_count || 0} 个有效令牌`
        : `人格网关已就绪，但 Codex 尚未配对 · 公共知识 ${result.knowledge?.knowledge_version || "版本未知"}`,
      configured ? "success" : "",
    );
  } catch (error) {
    if (indicator) {
      indicator.className = "connection-pill offline";
      indicator.textContent = "人格网关未连接";
    }
    setPluginStatus(`无法读取人格网关：${error.message}`, "error");
  }
}

async function pairCodex() {
  const characterId = byId("plugin-character")?.value || "";
  if (!characterId) {
    setPluginStatus("请先选择默认角色。", "error");
    return;
  }
  const button = byId("pair-codex");
  button.disabled = true;
  setPluginStatus("正在创建可撤销配对并写入 Windows 凭据库…");
  try {
    const result = await api("/api/v1/persona/pairings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "Codex snow-role-assistant", default_character_id: characterId }),
    });
    storageSet("project_snow:plugin_default_character", characterId);
    const manual = byId("plugin-manual-token");
    if (result.credential_saved) {
      manual.hidden = true;
      manual.textContent = "";
      setPluginStatus("配对完成。令牌已写入 Windows 凭据库；现在安装插件并在新任务中输入 @Snow。", "success");
    } else {
      manual.hidden = false;
      manual.textContent = `$env:SNOW_PERSONA_TOKEN='${result.pairing_token}'\n# 仅在启动 Codex 的同一 PowerShell 会话中有效`;
      setPluginStatus(`配对已创建，但 Windows 凭据库写入失败：${result.credential_error || "未知原因"}。可临时使用下方环境变量。`, "error");
    }
    await loadPersonaGatewayStatus();
  } catch (error) {
    setPluginStatus(`配对失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function revokeCodexPairing() {
  if (!window.confirm("撤销后，已安装的 Codex 插件将无法再读取 Snow 人格。继续吗？")) return;
  try {
    await api("/api/v1/persona/pairings/current", { method: "DELETE" });
    byId("plugin-manual-token").hidden = true;
    setPluginStatus("当前 Codex 配对已撤销。", "success");
    await loadPersonaGatewayStatus();
  } catch (error) {
    setPluginStatus(`撤销失败：${error.message}`, "error");
  }
}

function renderPersonaSnapshot(snapshot) {
  const target = byId("plugin-snapshot");
  const forbidden = (snapshot.forbidden_data_types || []).join("、");
  target.innerHTML = `<dl>
    <dt>角色</dt><dd>${escapeHtml(snapshot.character?.display_name || "未知")}</dd>
    <dt>人格版本</dt><dd>${escapeHtml(snapshot.profile_version || "未知")}</dd>
    <dt>关系</dt><dd>${escapeHtml(snapshot.relationship?.status || "未确认")}</dd>
    <dt>有效称呼</dt><dd>${escapeHtml(snapshot.relationship?.preferred_address || "分析员")}</dd>
    <dt>历史写回</dt><dd>${snapshot.relationship?.write_back_allowed ? "允许" : "禁止"}</dd>
    <dt>明确排除</dt><dd>${escapeHtml(forbidden || "沉浸式与 Agent 私有数据")}</dd>
  </dl>`;
}

async function testPersonaSnapshot() {
  const characterId = byId("plugin-character")?.value || "";
  if (!characterId) return;
  setPluginStatus("正在使用 Windows 凭据中的当前配对读取人格快照…");
  try {
    const snapshot = await api(`/api/v1/persona/management/snapshot/${encodeURIComponent(characterId)}`, {}, 15000);
    renderPersonaSnapshot(snapshot);
    setPluginStatus("人格快照测试通过；沉浸式历史和 Agent 数据均不在返回范围内。", "success");
  } catch (error) {
    setPluginStatus(`人格快照测试失败：${error.message}`, "error");
  }
}

async function bootstrap() {
  document.body.dataset.surface = state.surface;
  byId("landing-view").hidden = state.surface !== "landing";
  byId("plugin-center").hidden = state.surface !== "assistant";
  byId("chat-app").hidden = state.surface !== "immersive";
  byId("surface-label").textContent = state.mode === "assistant" ? "角色助手" : "沉浸式陪伴";
  try {
    const result = await api("/api/v1/mvp/bootstrap", {}, 30000);
    state.clientVersion = result.client_version || state.clientVersion;
    state.registryVersion = result.registry_version || "";
    state.enabled = Boolean(result.enabled && result.provider_configured);
    state.characters = (result.characters || []).filter((item) => item.selector_enabled !== false && item.view_available !== false);
    state.characterMap = new Map(state.characters.map((item) => [item.character_id, item]));
    state.feedbackCategories = result.feedback_categories || [];
    state.worldSessionId = storageGet("project_snow:world_session_id", "") || newWorldSessionId();
    storageSet("project_snow:world_session_id", state.worldSessionId);
    if (state.surface !== "assistant") await Promise.all([loadModels(), loadProviders()]);
    setConnection(true, state.enabled ? `已连接 · ${result.model || "模型已配置"}` : "已连接 · 模型未开启");
    renderFeedbackCategories();
    if (state.surface === "landing") return;
    if (state.surface === "assistant") {
      renderPluginCharacters();
      await loadPersonaGatewayStatus();
      return;
    }
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
byId("go-in-person").addEventListener("click", async () => {
  await resolveScene(state.selectedCharacterId);
  openPresenceDialog();
});
byId("confirm-presence-transition").addEventListener("click", () => (
  arriveInPerson(state.presenceDialogCharacterId || state.selectedCharacterId)
));
byId("stay-on-communicator").addEventListener("click", () => (
  transitionPresence("text", state.presenceDialogCharacterId || state.selectedCharacterId)
));
byId("open-communicator").addEventListener("click", () => transitionPresence("text"));
byId("toggle-stage-ui").addEventListener("click", () => setStageUiHidden(!state.stageUiHidden));
byId("restore-stage-ui").addEventListener("click", () => setStageUiHidden(false));
byId("scene-stage").addEventListener("click", () => {
  if (state.stageUiHidden) setStageUiHidden(false);
});
byId("stage-dialogue").addEventListener("click", () => completeStageReveal());
byId("toggle-action").addEventListener("click", () => {
  const thread = currentThread();
  if (!thread || thread.channel !== "in_person") return;
  thread.actionExpanded = !thread.actionExpanded;
  updateComposerFields();
  if (thread.actionExpanded) requestAnimationFrame(() => byId("action-input").focus());
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
byId("transcript-content").addEventListener("click", (event) => {
  const feedback = event.target.closest("[data-message-feedback]");
  const info = event.target.closest("[data-message-info]");
  const retry = event.target.closest("[data-retry-message]");
  if (feedback) openFeedback(findAssistantMessage(feedback.dataset.messageFeedback));
  else if (info) {
    const message = findAssistantMessage(info.dataset.messageInfo);
    openInfo(message?.result || null);
  } else if (retry) retryMessage(retry.dataset.retryMessage);
});
byId("open-info").addEventListener("click", () => openInfo());
byId("open-stage-info").addEventListener("click", () => openInfo());
byId("close-info").addEventListener("click", closeDrawers);
byId("open-transcript").addEventListener("click", openTranscript);
byId("close-transcript").addEventListener("click", closeDrawers);
byId("stage-message-info").addEventListener("click", () => {
  const message = latestAssistantMessage("in_person");
  if (message) openInfo(message.result || null);
});
byId("stage-message-feedback").addEventListener("click", () => {
  const message = latestAssistantMessage("in_person");
  if (message) openFeedback(message);
});
byId("open-contacts").addEventListener("click", () => {
  byId("contact-panel").classList.add("open");
  byId("drawer-scrim").hidden = false;
});
byId("open-stage-contacts").addEventListener("click", () => {
  byId("contact-panel").classList.add("open");
  byId("drawer-scrim").hidden = false;
});
byId("close-contacts").addEventListener("click", closeDrawers);
byId("drawer-scrim").addEventListener("click", closeDrawers);
byId("open-global-feedback").addEventListener("click", () => openFeedback(null));
byId("floating-feedback").addEventListener("click", () => openFeedback(null, state.surface === "landing"));
byId("landing-open-feedback").addEventListener("click", () => openFeedback(null, true));
byId("plugin-open-feedback").addEventListener("click", () => openFeedback(null, true));
byId("plugin-character").addEventListener("change", (event) => {
  storageSet("project_snow:plugin_default_character", event.target.value);
});
byId("pair-codex").addEventListener("click", pairCodex);
byId("revoke-codex").addEventListener("click", revokeCodexPairing);
byId("test-persona-snapshot").addEventListener("click", testPersonaSnapshot);
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
byId("thinking-mode").addEventListener("change", (event) => {
  const thread = currentThread();
  if (!thread) return;
  thread.thinkingMode = event.target.value;
  storageSet(`project_snow:thinking:${thread.characterId}`, thread.thinkingMode);
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
document.querySelectorAll("[data-provider-choice]").forEach((button) => {
  button.addEventListener("click", () => selectProvider(button.dataset.providerChoice));
});
byId("provider-model").addEventListener("input", updateWindowsEnvGuide);
byId("provider-model-select").addEventListener("change", updateWindowsEnvGuide);
byId("provider-url").addEventListener("input", updateWindowsEnvGuide);
byId("probe-provider").addEventListener("click", configureProvider);
byId("verify-provider-model").addEventListener("click", () => verifySelectedProviderModel());
byId("save-model-defaults").addEventListener("click", saveModelDefaults);
byId("copy-windows-env").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(byId("windows-env-code").textContent);
    showToast("PowerShell 命令已复制；请先替换 API Key 占位符。");
  } catch (_) {
    showToast("浏览器未允许复制，请手动选择命令文本。");
  }
});
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

window.addEventListener("keydown", (event) => {
  const target = event.target;
  const editing = target?.matches?.("input, textarea, select, [contenteditable='true']");
  if (editing) return;
  if (state.stageUiHidden && ["Escape", " ", "h", "H"].includes(event.key)) {
    event.preventDefault();
    setStageUiHidden(false);
    return;
  }
  if (currentThread()?.channel === "in_person" && event.key === " ") {
    event.preventDefault();
    if (!completeStageReveal()) setStageUiHidden(false);
    return;
  }
  if (currentThread()?.channel === "in_person" && ["h", "H"].includes(event.key)) {
    event.preventDefault();
    setStageUiHidden(!state.stageUiHidden);
    return;
  }
  if (event.key === "Escape") closeDrawers();
});

window.addEventListener("online", () => setConnection(true));
window.addEventListener("offline", () => setConnection(false, "网络不可用"));

selectProvider(byId("provider-kind").value, false);
renderIcons();
bootstrap();
