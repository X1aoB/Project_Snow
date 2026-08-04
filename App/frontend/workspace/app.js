// The browser always talks to its own origin. Docker Nginx and the local
// development server proxy /api and /health to the private API service.
const apiBase = "";
// The MVP provider has its own server-side timeout, but the browser must also
// recover its sending/typing state when a network request is orphaned.
const API_REQUEST_TIMEOUT_MS = 150000;
const byId = (id) => document.getElementById(id);

const tierLabels = {
  high: "高价值叙事关系",
  normal: "常规叙事事实",
  low: "低价值提及",
};
const riskLabels = {
  high: "高风险：证据缺失",
  medium: "中风险：需复核语境",
  low: "低风险：证据可用",
};
const machineVerdictLabels = {
  recommend_approve: "模型建议批准（仍需抽检/人工）",
  recommend_reject: "模型建议驳回（不自动生效）",
  abstain_or_incomplete: "模型保留/审核未完成",
  mixed: "同组模型结论不一致",
  unreviewed: "尚未进行二次模型审核",
  abstain: "模型保留人工判断",
};
let reviewListState = { filters: {}, groups: [], total: 0, sampleMode: false };
let entityReviewListState = { candidates: [], total: 0 };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}

async function api(path, options = {}) {
  const { timeoutMs = API_REQUEST_TIMEOUT_MS, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${apiBase}${path}`, { ...fetchOptions, signal: controller.signal });
    if (!response.ok) {
      const raw = await response.text();
      let detail = raw;
      try {
        const payload = JSON.parse(raw);
        detail = payload.detail ?? payload;
      } catch (_) {
        // Keep the gateway's plain-text error when it is not JSON.
      }
      const message = typeof detail === "string" ? detail : (detail?.message || raw || `HTTP ${response.status}`);
      const error = new Error(message);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  } catch (error) {
    if (error?.name === "AbortError") {
      const timeoutError = new Error("请求等待超时，已结束本次发送。请稍后重试。");
      timeoutError.code = "request_timeout";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function queryString(values) {
  const parameters = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).trim()) parameters.set(key, String(value).trim());
  });
  const encoded = parameters.toString();
  return encoded ? `?${encoded}` : "";
}

async function loadHealth() {
  try {
    const health = await api("/health");
    const unavailable = Object.entries(health.artifacts).filter(([, available]) => !available).map(([key]) => key);
    byId("health").textContent = unavailable.length ? `等待工件：${unavailable.join("、")}` : "B/C 工件已就绪";
  } catch (error) {
    byId("health").textContent = "API 未连接";
  }
}

async function loadCharacters() {
  try {
    const characters = await api("/api/v1/characters");
    byId("character").innerHTML = '<option value="">不限定角色</option>' + characters.map((item) => (
      `<option value="${escapeHtml(item.character_id)}">${escapeHtml(item.character_name)}</option>`
    )).join("");
  } catch (error) {
    byId("character").innerHTML = '<option value="">角色资料尚未构建</option>';
  }
}

function currentCharacter() {
  return byId("character").value;
}

async function loadPersona() {
  const characterId = currentCharacter();
  if (!characterId) {
    byId("persona").textContent = "请先选择角色。";
    return;
  }
  try {
    const profile = await api(`/api/v1/personas/${encodeURIComponent(characterId)}`);
    const evidence = Object.entries(profile.evidence).map(([kind, ids]) => (
      `<li><strong>${escapeHtml(kind)}</strong>：${ids.length} 个来源片段</li>`
    )).join("");
    byId("persona").innerHTML = `<p><strong>${escapeHtml(profile.character_name)}</strong> · ${escapeHtml(profile.review_status)}</p><p>${escapeHtml(profile.relationship_invariant.policy)}</p><ul>${evidence}</ul>`;
  } catch (error) {
    byId("persona").textContent = "人格档案读取失败。";
  }
}

async function loadGraph() {
  const characterId = currentCharacter();
  if (!characterId) {
    byId("graph").textContent = "请先选择角色。";
    return;
  }
  try {
    const graph = await api(`/api/v1/graph/neighborhood/character%3A${encodeURIComponent(characterId)}`);
    const rows = graph.edges.map((edge) => {
      const scope = edge.narrative_scope && edge.narrative_scope !== "unknown"
        ? ` · 语境 ${escapeHtml(edge.narrative_scope)}`
        : "";
      return `<li><code>${escapeHtml(edge.relation_type)}</code> ⇒ ${escapeHtml(edge.to_id)} <span class="muted">证据页 ${edge.evidence_page_ids.join(", ")}${scope}</span></li>`;
    }).join("");
    byId("graph").innerHTML = `<p><strong>${escapeHtml(graph.node.name)}</strong> · <code>${escapeHtml(graph.node.node_id)}</code> · 已验证关系 ${graph.edges.length} 条</p><ul>${rows || "<li>暂未找到已验证关系。</li>"}</ul>`;
  } catch (error) {
    byId("graph").textContent = "图谱工件尚未构建或角色节点不存在。";
  }
}

async function search() {
  const query = byId("query").value.trim();
  if (!query) return;
  byId("results").textContent = "正在检索…";
  try {
    const result = await api("/api/v1/retrieval/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, character_id: currentCharacter() || null, mode: byId("mode").value }),
    });
    byId("retrieval-meta").textContent = `${result.fusion} · 向量 ${result.vector_available ? "可用" : "未构建"} · 你=${result.conversation_identity.user_role} · ${result.results.length} 条`;
    byId("results").innerHTML = result.results.length ? result.results.map((hit) => (
      `<article class="hit"><div><span class="chip">${escapeHtml(hit.citation.source_type)}</span><span class="chip">${escapeHtml(hit.score)}</span></div><h3>${escapeHtml(hit.citation.title)}</h3><p>${escapeHtml(hit.text)}</p>${hit.citation.canonical_url ? `<a href="${escapeHtml(hit.citation.canonical_url)}" target="_blank" rel="noreferrer">查看 Wiki 来源</a>` : ""}</article>`
    )).join("") : "未检索到符合当前角色条件的来源。";
  } catch (error) {
    byId("results").textContent = `检索失败：${error.message}`;
  }
}

function reviewFilters() {
  return {
    review_status: "pending_review",
    tier: byId("review-tier").value,
    relation_type: byId("review-relation-type").value,
    source_type: byId("review-source-type").value,
    risk_level: byId("review-risk-level").value,
    machine_verdict: byId("review-machine-verdict").value,
  };
}

function replaceSelectOptions(id, entries, emptyLabel) {
  const select = byId(id);
  const selected = select.value;
  const options = Object.keys(entries || {}).sort().map((value) => (
    `<option value="${escapeHtml(value)}">${escapeHtml(value)}（${escapeHtml(entries[value])}）</option>`
  ));
  select.innerHTML = `<option value="">${escapeHtml(emptyLabel)}</option>${options.join("")}`;
  if (selected && entries && Object.prototype.hasOwnProperty.call(entries, selected)) select.value = selected;
}

function groupRelation(group) {
  return `${escapeHtml(group.subject)} <code>${escapeHtml(group.relation_type)}</code> ${escapeHtml(group.object)}`;
}

function groupRiskFlags(group) {
  if (!group.risk_flags?.length) return "无额外风险标记";
  return group.risk_flags.map((flag) => escapeHtml(flag)).join("、");
}

function machineReviewSummary(machineReview) {
  if (!machineReview) return "";
  const verdict = machineReview.group_verdict || "unreviewed";
  const models = (machineReview.reviewer_models || []).join("、") || "未配置";
  const coverage = `${machineReview.completed_candidate_count ?? 0}/${machineReview.candidate_count ?? 0}`;
  const eligible = machineReview.audit_eligible_candidate_count ?? 0;
  return `<div class="machine-review-summary">
    <strong>独立二次审核：</strong><span class="chip machine-${escapeHtml(verdict)}">${escapeHtml(machineVerdictLabels[verdict] || verdict)}</span>
    <span class="muted">覆盖 ${escapeHtml(coverage)}；可进入抽检池 ${escapeHtml(eligible)}；模型 ${escapeHtml(models)}。这只是建议，不会批准候选或写入图谱。</span>
  </div>`;
}

function machineReviewDetail(machineReview) {
  if (!machineReview) {
    return `<div class="machine-review-detail muted">尚无独立二次审核报告。请先运行二次审核流水线；在此之前不要把抽取模型的置信度当作批准依据。</div>`;
  }
  const verdict = machineReview.verdict || "abstain";
  const flags = [...(machineReview.risk_flags || []), ...(machineReview.validation_flags || [])];
  const model = machineReview.model_reviewer || {};
  const quote = machineReview.supporting_quote
    ? `<blockquote class="review-quote">${escapeHtml(machineReview.supporting_quote)}</blockquote>`
    : "";
  return `<div class="machine-review-detail">
    <div><strong>独立模型建议：</strong><span class="chip machine-${escapeHtml(verdict)}">${escapeHtml(machineVerdictLabels[verdict] || verdict)}</span><span class="chip">${escapeHtml(model.provider || "unknown")}/${escapeHtml(model.model || "unknown")}</span></div>
    <p>证据：${escapeHtml(machineReview.evidence_sufficiency || "unknown")}；关系类型：${escapeHtml(String(machineReview.relation_type_valid))}；身份：${escapeHtml(machineReview.identity_mapping_confidence || "unknown")}；语境：${escapeHtml(machineReview.temporal_scope || "unknown")}。</p>
    <p>${escapeHtml(machineReview.verdict_rationale || "模型未提供可用理由。")}</p>
    ${quote}
    ${flags.length ? `<p class="risk-list"><strong>模型/本地校验标记：</strong>${flags.map((flag) => escapeHtml(flag)).join("、")}</p>` : ""}
  </div>`;
}

function mappingSummary(group) {
  const subject = group.mapping_suggestions?.subject || [];
  const object = group.mapping_suggestions?.object || [];
  const subjectText = subject.length ? subject.map((node) => `<code>${escapeHtml(node.node_id)}</code>`).join(" ") : "无精确名称建议";
  const objectText = object.length ? object.map((node) => `<code>${escapeHtml(node.node_id)}</code>`).join(" ") : "无精确名称建议";
  return `<div class="mapping-hint"><strong>节点映射：</strong>${escapeHtml(group.mapping_status)}。主体建议：${subjectText}；客体建议：${objectText}。建议仅按规范化名称匹配，已排除页面/剧情等来源节点，仍须人工确认。</div>`;
}

function groupQuotes(group) {
  const quotes = group.representative_evidence_quotes || [];
  if (!quotes.length) return "<p class=\"muted\">此组没有可用的代表性引文，不能批准。</p>";
  return quotes.map((item) => (
    `<blockquote class="review-quote"><span class="muted">${escapeHtml(item.source_type)} · ${escapeHtml(item.page_id || "未知页面")}</span><br>${escapeHtml(item.quote)}</blockquote>`
  )).join("");
}

function reviewGroupSummary(group) {
  return `<article class="review-group" data-review-group="${escapeHtml(group.review_group_id)}">
    <div class="review-group-head">
      <div>
        <div><span class="chip ${escapeHtml(group.priority_tier)}">${escapeHtml(tierLabels[group.priority_tier] || group.priority_tier)}</span><span class="chip ${group.risk_level === "medium" ? "medium-risk" : group.risk_level === "high" ? "high-risk" : ""}">${escapeHtml(riskLabels[group.risk_level] || group.risk_level)}</span><span class="chip">${escapeHtml(group.relation_type)}</span></div>
        <h3>${groupRelation(group)}</h3>
      </div>
      <button data-review-open="${escapeHtml(group.review_group_id)}" class="secondary">展开逐条审核</button>
    </div>
    <div class="review-group-meta">
      <div><strong>候选 / 证据：</strong>${escapeHtml(group.candidate_count)} 条候选，${escapeHtml(group.evidence_document_count)} 个片段，${escapeHtml(group.evidence_page_count)} 页</div>
      <div><strong>来源：</strong>${escapeHtml((group.source_types || []).join("、"))}</div>
      <div><strong>抽取模型：</strong>${escapeHtml((group.extractor_models || []).join("、"))}</div>
      <div><strong>模型置信度：</strong>${escapeHtml(group.confidence?.mean ?? "未提供")}（仅供参考，不用于自动审批）</div>
    </div>
    ${machineReviewSummary(group.machine_review)}
    <p class="risk-list"><strong>复核提示：</strong>${groupRiskFlags(group)}</p>
    ${mappingSummary(group)}
    ${groupQuotes(group)}
  </article>`;
}

function reviewEvidence(item, quote) {
  const source = `${escapeHtml(item.source_type)} / ${escapeHtml(item.title)}`;
  const link = item.canonical_url ? ` <a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noreferrer">查看 Wiki 来源</a>` : "";
  return `<li><strong>${source}</strong>${link}${quote ? `<blockquote class="review-quote">${escapeHtml(quote)}</blockquote>` : ""}<details><summary>查看完整证据片段</summary><p>${escapeHtml(item.text)}</p></details></li>`;
}

function nodeOptions(nodes) {
  return (nodes || []).map((node) => (
    `<option value="${escapeHtml(node.node_id)}" label="${escapeHtml(`${node.name || ""} · ${node.node_type || ""}`)}"></option>`
  )).join("");
}

function reviewCandidate(candidate, group) {
  const candidateId = escapeHtml(candidate.candidate_id);
  const evidence = candidate.evidence?.length ? `<ul class="review-evidence">${candidate.evidence.map((item) => reviewEvidence(item, candidate.evidence_quote)).join("")}</ul>` : "<p class=\"muted\">候选引用的证据片段已不存在，不能批准。</p>";
  const subjectListId = `mapping-subject-${candidateId}`;
  const objectListId = `mapping-object-${candidateId}`;
  return `<article class="review-item" data-candidate="${candidateId}">
    <div><span class="chip">置信度 ${escapeHtml(candidate.confidence ?? "未提供")}</span><span class="chip">${escapeHtml(candidate.source_type)}</span><span class="chip">${escapeHtml(candidate.extractor?.model || "未知模型")}</span></div>
    <h4>${escapeHtml(candidate.subject)} <code>${escapeHtml(candidate.relation_type)}</code> ${escapeHtml(candidate.object)}</h4>
    <p><strong>抽取理由：</strong>${escapeHtml(candidate.rationale || "未提供抽取理由")}</p>
    ${machineReviewDetail(candidate.machine_review)}
    ${evidence}
    ${mappingSummary(group)}
    <div class="review-mapping">
      <label>主体节点 ID（人工确认后填写）<input data-field="from" list="${subjectListId}" placeholder="例如：character:..." /></label>
      <label>客体节点 ID（人工确认后填写）<input data-field="to" list="${objectListId}" placeholder="例如：location:..." /></label>
    </div>
    <datalist id="${subjectListId}">${nodeOptions(group.mapping_suggestions?.subject)}</datalist>
    <datalist id="${objectListId}">${nodeOptions(group.mapping_suggestions?.object)}</datalist>
    <div class="review-actions"><button data-review-decision="approved">批准并写入图谱</button><button class="secondary" data-review-decision="rejected">驳回</button></div>
  </article>`;
}

function reviewGroupDetail(group) {
  const candidateTotal = group.candidate_total ?? group.candidate_count ?? 0;
  const visibleCandidates = group.candidates || [];
  const nextOffset = (group.candidate_offset || 0) + visibleCandidates.length;
  const moreCandidates = nextOffset < candidateTotal ? `<div class="review-group-pagination"><button class="secondary" data-review-more-candidates="${escapeHtml(group.review_group_id)}" data-candidate-offset="${escapeHtml(nextOffset)}">继续加载该组候选（${escapeHtml(nextOffset)}/${escapeHtml(candidateTotal)}）</button></div>` : "";
  return `<div class="review-group-head">
    <div><div><span class="chip ${escapeHtml(group.priority_tier)}">${escapeHtml(tierLabels[group.priority_tier] || group.priority_tier)}</span><span class="chip">展开审核 · ${escapeHtml(candidateTotal)} 条候选</span></div><h3>${groupRelation(group)}</h3></div>
    <button data-review-collapse="${escapeHtml(group.review_group_id)}" class="secondary">收起</button>
  </div>
  <p class="review-detail-policy">审核原则：每次操作只影响下方的一条候选。即使同组有多条相似证据，也不会自动批量批准；请核对该候选的直接引文、完整片段和图谱节点。</p>
  ${machineReviewSummary(group.machine_review)}
  ${mappingSummary(group)}
  <div data-review-candidate-list>${visibleCandidates.map((candidate) => reviewCandidate(candidate, group)).join("")}</div>
  ${moreCandidates}`;
}

function renderReviewGroups(groups, emptyMessage) {
  byId("review-groups").innerHTML = groups.length ? groups.map(reviewGroupSummary).join("") : `<p class="empty">${escapeHtml(emptyMessage)}</p>`;
}

function updateReviewPagination() {
  const button = byId("load-more-review");
  const shown = reviewListState.groups.length;
  button.hidden = reviewListState.sampleMode || shown >= reviewListState.total;
  button.disabled = reviewListState.sampleMode || shown >= reviewListState.total;
  byId("review-pagination-meta").textContent = reviewListState.sampleMode
    ? "分层样本不会替代完整待审核队列。使用“刷新队列”可返回按优先级排序的列表。"
    : `已显示 ${shown}/${reviewListState.total} 个关系组。`;
}

function updateReviewSummary(summary, groupResponse) {
  const triage = summary.triage || {};
  const byTier = triage.by_tier || {};
  const machine = summary.machine_review || {};
  const tierText = ["high", "normal", "low"].map((tier) => (
    `${tierLabels[tier]} ${byTier[tier]?.groups ?? 0} 组`
  )).join(" · ");
  const machineText = `二审报告 ${machine.completed_candidate_count ?? 0}/${machine.candidate_count ?? triage.candidate_count ?? 0}；建议批准 ${machine.by_verdict?.recommend_approve ?? 0}；保留 ${machine.by_verdict?.abstain ?? 0}`;
  byId("review-summary").textContent = `任务 ${summary.jobs.total} · 待审核候选 ${triage.candidate_count ?? 0} · 合并后 ${triage.group_count ?? 0} 组 · ${tierText} · ${machineText} · 已写入图谱 ${summary.approved_edges}`;
  byId("review-filter-meta").textContent = `当前筛选：${groupResponse.total} 组。模型二审只提供证据建议；层级、风险和模型结论都不改变人工批准要求。`;
}

async function loadRelationReview() {
  byId("review-groups").textContent = "正在构建确定性审核分组…";
  try {
    const filters = reviewFilters();
    const [summary, response] = await Promise.all([
      api("/api/v1/review/relations/summary"),
      api(`/api/v1/review/relations/groups${queryString({ ...filters, limit: 12, offset: 0 })}`),
    ]);
    replaceSelectOptions("review-relation-type", summary.triage?.by_relation_type, "全部关系类型");
    replaceSelectOptions("review-source-type", summary.triage?.by_source_type, "全部来源类型");
    updateReviewSummary(summary, response);
    reviewListState = { filters, groups: response.groups, total: response.total, sampleMode: false };
    renderReviewGroups(reviewListState.groups, "没有符合当前筛选条件的待审核关系组。");
    updateReviewPagination();
  } catch (error) {
    byId("review-groups").textContent = `审核队列读取失败：${error.message}`;
  }
}

async function loadAuditSample() {
  byId("review-groups").textContent = "正在生成可复现的分层样本…";
  try {
    const filters = reviewFilters();
    const size = Number.parseInt(byId("review-sample-size").value, 10) || 12;
    const seed = byId("review-sample-seed").value.trim() || "project-snow-audit-v1";
    const sample = await api(`/api/v1/review/relations/audit-sample${queryString({ ...filters, size, seed })}`);
    const quotaText = Object.entries(sample.tier_quotas || {}).map(([tier, quota]) => `${tierLabels[tier] || tier} ${quota}`).join(" · ");
    byId("review-filter-meta").textContent = `分层样本：${sample.sample_size}/${sample.available_group_count} 组；${quotaText}；种子“${sample.seed}”。相同条件和种子会得到相同样本。`;
    reviewListState = { filters, groups: sample.groups, total: sample.sample_size, sampleMode: true };
    renderReviewGroups(reviewListState.groups, "当前筛选下没有可抽取的待审核关系组。");
    updateReviewPagination();
  } catch (error) {
    byId("review-groups").textContent = `分层抽样失败：${error.message}`;
  }
}

async function loadMoreRelationReview() {
  if (reviewListState.sampleMode || reviewListState.groups.length >= reviewListState.total) return;
  const button = byId("load-more-review");
  button.disabled = true;
  try {
    const response = await api(`/api/v1/review/relations/groups${queryString({ ...reviewListState.filters, limit: 12, offset: reviewListState.groups.length })}`);
    reviewListState.groups = reviewListState.groups.concat(response.groups);
    reviewListState.total = response.total;
    renderReviewGroups(reviewListState.groups, "没有符合当前筛选条件的待审核关系组。");
  } catch (error) {
    window.alert(`加载更多审核组失败：${error.message}`);
  } finally {
    updateReviewPagination();
  }
}

async function openReviewGroup(button) {
  const groupId = button.dataset.reviewOpen;
  const container = button.closest("[data-review-group]");
  if (!groupId || !container) return;
  button.disabled = true;
  try {
    const detail = await api(`/api/v1/review/relations/groups/${encodeURIComponent(groupId)}${queryString({ review_status: "pending_review", candidate_limit: 12, candidate_offset: 0 })}`);
    container.innerHTML = reviewGroupDetail(detail);
  } catch (error) {
    button.disabled = false;
    window.alert(`读取关系组详情失败：${error.message}`);
  }
}

async function loadMoreGroupCandidates(button) {
  const groupId = button.dataset.reviewMoreCandidates;
  const offset = Number.parseInt(button.dataset.candidateOffset, 10) || 0;
  const container = button.closest("[data-review-group]");
  const candidateList = container?.querySelector("[data-review-candidate-list]");
  if (!groupId || !candidateList) return;
  button.disabled = true;
  try {
    const detail = await api(`/api/v1/review/relations/groups/${encodeURIComponent(groupId)}${queryString({ review_status: "pending_review", candidate_limit: 12, candidate_offset: offset })}`);
    candidateList.insertAdjacentHTML("beforeend", (detail.candidates || []).map((candidate) => reviewCandidate(candidate, detail)).join(""));
    const nextOffset = (detail.candidate_offset || 0) + (detail.candidates || []).length;
    if (nextOffset >= detail.candidate_total) button.remove();
    else {
      button.dataset.candidateOffset = String(nextOffset);
      button.textContent = `继续加载该组候选（${nextOffset}/${detail.candidate_total}）`;
      button.disabled = false;
    }
  } catch (error) {
    button.disabled = false;
    window.alert(`加载该组候选失败：${error.message}`);
  }
}

async function collapseReviewGroup(button) {
  await loadRelationReview();
}

async function decideRelationCandidate(button) {
  const item = button.closest("[data-candidate]");
  if (!item) return;
  const decision = button.dataset.reviewDecision;
  const fromNodeId = item.querySelector('[data-field="from"]').value.trim() || null;
  const toNodeId = item.querySelector('[data-field="to"]').value.trim() || null;
  if (decision === "approved" && (!fromNodeId || !toNodeId)) {
    window.alert("批准前必须人工确认并填写主体、客体的现有图谱节点 ID。系统不会自动映射。\n");
    return;
  }
  const payload = {
    decision,
    reviewer_id: byId("reviewer").value.trim() || "local-reviewer",
    note: byId("review-note").value.trim(),
    from_node_id: fromNodeId,
    to_node_id: toNodeId,
  };
  button.disabled = true;
  try {
    await api(`/api/v1/review/relations/candidates/${encodeURIComponent(item.dataset.candidate)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadRelationReview();
    if (currentCharacter()) await loadGraph();
  } catch (error) {
    button.disabled = false;
    window.alert(`审核操作失败：${error.message}`);
  }
}

function entityEvidenceExamples(candidate) {
  const examples = candidate.evidence_examples || [];
  if (!examples.length) return '<p class="muted">没有可展示的关系端点示例，不能批准。</p>';
  return `<ul class="entity-examples">${examples.map((example) => (
    `<li><strong>${escapeHtml(example.subject)} <code>${escapeHtml(example.relation_type)}</code> ${escapeHtml(example.object)}</strong><br><span class="muted">${escapeHtml(example.source_type || "未知来源")}</span>${example.evidence_quote ? `<blockquote class="review-quote">${escapeHtml(example.evidence_quote)}</blockquote>` : ""}</li>`
  )).join("")}</ul>`;
}

function entityReviewCandidate(candidate) {
  const entityCandidateId = escapeHtml(candidate.entity_candidate_id);
  const evidence = candidate.evidence?.length
    ? `<details><summary>查看来源证据片段</summary><ul class="review-evidence">${candidate.evidence.map((item) => reviewEvidence(item, "")).join("")}</ul></details>`
    : '<p class="muted">来源证据片段已不存在，不能批准。</p>';
  const relationCount = (candidate.relation_candidate_ids || []).length;
  return `<article class="review-item entity-candidate" data-entity-candidate="${entityCandidateId}">
    <div><span class="chip">${escapeHtml(candidate.proposed_node_type)}</span><span class="chip">关联关系 ${escapeHtml(relationCount)} 条</span></div>
    <h4>${escapeHtml(candidate.entity_name)} <span class="muted">→</span> <code>${escapeHtml(candidate.proposed_node_id)}</code></h4>
    <p class="muted">来源类型：${escapeHtml((candidate.source_types || []).join("、") || "未知")}；证据页：${escapeHtml((candidate.evidence_page_ids || []).join("、") || "无")}</p>
    ${entityEvidenceExamples(candidate)}
    ${evidence}
    <p class="review-detail-policy">批准仅创建这个可追溯的 ${escapeHtml(candidate.proposed_node_type)} 节点；不会自动批准上方列出的任何关系。创建后请回到关系审核页面，重新核对具体关系。</p>
    <div class="review-actions"><button data-entity-review-decision="approved">批准并创建节点</button><button class="secondary" data-entity-review-decision="rejected">驳回节点候选</button></div>
  </article>`;
}

function updateEntityReviewPagination() {
  const button = byId("load-more-entity-review");
  const shown = entityReviewListState.candidates.length;
  button.hidden = shown >= entityReviewListState.total;
  button.disabled = shown >= entityReviewListState.total;
  byId("entity-review-pagination-meta").textContent = `已显示 ${shown}/${entityReviewListState.total} 个实体候选。`;
}

function updateEntityReviewSummary(summary) {
  const byType = Object.entries(summary.by_node_type || {}).map(([type, count]) => `${type} ${count}`).join(" · ") || "暂无待补节点";
  byId("entity-review-summary").textContent = `待审核节点 ${summary.candidates?.by_status?.pending_review ?? 0} · 覆盖待审核关系 ${summary.pending_relation_candidates_covered ?? 0} 条 · ${byType} · 已人工创建节点 ${summary.approved_nodes ?? 0}`;
}

async function loadEntityReview() {
  byId("entity-review-candidates").textContent = "正在构建缺失实体候选…";
  try {
    const [summary, response] = await Promise.all([
      api("/api/v1/review/entities/summary"),
      api("/api/v1/review/entities/candidates?review_status=pending_review&limit=20&offset=0"),
    ]);
    updateEntityReviewSummary(summary);
    entityReviewListState = { candidates: response.candidates || [], total: response.total || 0 };
    byId("entity-review-candidates").innerHTML = entityReviewListState.candidates.length
      ? entityReviewListState.candidates.map(entityReviewCandidate).join("")
      : '<p class="empty">当前没有待审批的缺失实体节点。</p>';
    updateEntityReviewPagination();
  } catch (error) {
    byId("entity-review-candidates").textContent = `实体节点队列读取失败：${error.message}`;
  }
}

async function loadMoreEntityReview() {
  if (entityReviewListState.candidates.length >= entityReviewListState.total) return;
  const button = byId("load-more-entity-review");
  button.disabled = true;
  try {
    const offset = entityReviewListState.candidates.length;
    const response = await api(`/api/v1/review/entities/candidates?review_status=pending_review&limit=20&offset=${offset}`);
    entityReviewListState.candidates = entityReviewListState.candidates.concat(response.candidates || []);
    entityReviewListState.total = response.total || entityReviewListState.total;
    byId("entity-review-candidates").innerHTML = entityReviewListState.candidates.length
      ? entityReviewListState.candidates.map(entityReviewCandidate).join("")
      : '<p class="empty">当前没有待审批的缺失实体节点。</p>';
  } catch (error) {
    window.alert(`加载实体节点候选失败：${error.message}`);
  } finally {
    updateEntityReviewPagination();
  }
}

async function decideEntityNodeCandidate(button) {
  const item = button.closest("[data-entity-candidate]");
  if (!item) return;
  button.disabled = true;
  try {
    await api(`/api/v1/review/entities/candidates/${encodeURIComponent(item.dataset.entityCandidate)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision: button.dataset.entityReviewDecision,
        reviewer_id: byId("reviewer").value.trim() || "local-reviewer",
        note: byId("review-note").value.trim(),
      }),
    });
    await Promise.all([loadEntityReview(), loadRelationReview()]);
    if (currentCharacter()) await loadGraph();
  } catch (error) {
    button.disabled = false;
    window.alert(`实体节点审核操作失败：${error.message}`);
  }
}

function resetReviewFilters() {
  byId("review-tier").value = "";
  byId("review-relation-type").value = "";
  byId("review-source-type").value = "";
  byId("review-risk-level").value = "";
  byId("review-machine-verdict").value = "";
  loadRelationReview();
}

let mvpState = {
  characterId: "",
  sessionId: "",
  worldSessionId: "",
  messageId: "",
  mode: "immersive",
  threads: {},
  feedbackOptions: [],
};

function resetMvpSession(clearAnswer = true) {
  mvpState = {
    ...mvpState,
    sessionId: "",
    messageId: "",
    mode: byId("mvp-mode")?.value || "immersive",
  };
  if (clearAnswer) {
    byId("mvp-answer").classList.add("empty");
    byId("mvp-answer").textContent = "模式或角色已切换；共享已确认背景，保留各自说话方式。";
    byId("mvp-feedback").hidden = true;
    renderMvpStyleContext({ status: "none" });
  }
}

function renderMvpStyleContext(style) {
  const target = byId("mvp-style-context");
  if (!target) return;
  const context = style || {};
  if (context.status === "active") {
    const activation = context.activation_source === "session" ? "会话延续" : "本轮识别";
    const label = context.kind === "costume"
      ? `时装：${context.costume_name || "未命名"} · 适配装甲：${context.armor_name || "未解析"}`
      : `装甲：${context.armor_name || "未命名"}`;
    target.innerHTML = `<span class="chip">${escapeHtml(activation)}</span> ${escapeHtml(label)}（装甲/时装属于同一角色上下文）`;
  } else if (context.status === "ambiguous") {
    const names = (context.candidates || []).map((item) => item.costume_name || item.armor_name).filter(Boolean).join("、");
    target.textContent = `检测到多个可能语境${names ? `：${names}` : ""}；本轮不会自动混入时装，请补充完整名称。`;
  } else if (context.status === "unresolved") {
    target.textContent = `已收到手动语境“${context.costume_name || context.raw || ""}”，但未在资料中精确匹配；不会解锁不相关的时装资料。`;
  } else if (context.status === "cleared") {
    target.textContent = "已切回角色本体；本轮不会使用时装语气。";
  } else {
    target.textContent = "当前未激活装甲或时装语境；角色本体设定优先。";
  }
}

function renderMvpFeedbackOptions(options) {
  mvpState.feedbackOptions = options || [];
  byId("mvp-feedback-options").innerHTML = mvpState.feedbackOptions.map((option) => (
    `<label class="feedback-option"><input type="checkbox" value="${escapeHtml(option.id)}" />${escapeHtml(option.label)}</label>`
  )).join("");
}

function renderMvpCoverage(coverage, registryVersion = "") {
  const target = byId("mvp-coverage");
  if (!target) return;
  const value = coverage || {};
  const level = value.level || "unknown";
  const label = value.label || "资料覆盖状态未知";
  const direct = Number(value.direct_document_count || 0);
  const linked = Number(value.linked_document_count || 0);
  const shared = Number(value.shared_context_document_count || 0);
  const address = Number(value.address_term_count || 0);
  const voice = Number(value.voice_evidence_count || 0);
  target.innerHTML = `<strong>资料覆盖：${escapeHtml(label)}</strong> <span class="chip coverage-${escapeHtml(level)}">${escapeHtml(level)}</span>`
    + `<span class="muted">直接资料 ${direct} 条 · 关联资料 ${linked} 条 · 共享背景 ${shared} 条 · 称呼证据 ${address} 条 · 语气证据 ${voice} 条${registryVersion ? ` · ${escapeHtml(registryVersion)}` : ""}</span>`;
}

async function loadMvpQuestions() {
  const characterId = byId("mvp-character").value;
  if (!characterId) return;
  mvpState.characterId = characterId;
  try {
    const result = await api(`/api/v1/mvp/questions${queryString({ character_id: characterId })}`);
    byId("mvp-questions").innerHTML = (result.questions || []).map((question) => (
      `<button type="button" class="mvp-question" data-mvp-question="${escapeHtml(question.text)}">${escapeHtml(question.category)}：${escapeHtml(question.text)}</button>`
    )).join("") || "当前角色没有预设问题。";
    renderMvpFeedbackOptions(result.feedback_options || []);
    renderMvpCoverage(result.coverage, result.registry_version);
  } catch (error) {
    byId("mvp-questions").textContent = `问题库读取失败：${error.message}`;
  }
}

async function loadMvpStatus() {
  try {
    const status = await api("/api/v1/mvp/status");
    const selectable = (status.selected_characters || []).filter((item) => item.selector_enabled !== false && item.view_available);
    byId("mvp-character").innerHTML = selectable.map((item) => (
      `<option value="${escapeHtml(item.character_id)}" title="${escapeHtml(item.coverage?.label || "资料覆盖状态未知")}">${escapeHtml(item.character_name)}${item.coverage?.level === "limited" ? "（资料覆盖有限）" : ""}</option>`
    )).join("") || '<option value="">视图尚未构建</option>';
    const state = status.enabled && status.provider_configured ? "模型已配置，可发送测试" : "检索工件已就绪；模型接口当前未开启（设置 MVP_CHAT_ENABLED=true）";
    byId("mvp-status").textContent = `${state} · ${status.selected_characters?.length || 0} 个角色 · ${status.question_count || 0} 条预设问题 · ${status.registry_version || ""}`;
    if (status.policy?.conversation_modes) {
      byId("mvp-status").textContent += ` · 默认${status.policy.conversation_modes.default === "immersive" ? "沉浸式陪伴" : status.policy.conversation_modes.default}`;
    }
    await loadMvpQuestions();
    resetMvpSession();
  } catch (error) {
    byId("mvp-status").textContent = `MVP 服务未连接：${error.message}`;
  }
}

async function submitMvpFeedback() {
  const selectedOptions = [...byId("mvp-feedback-options").querySelectorAll("input:checked")].map((input) => input.value);
  const freeText = byId("mvp-feedback-text").value.trim();
  if (!mvpState.characterId || !mvpState.sessionId || (!selectedOptions.length && !freeText)) {
    window.alert("请先完成一次回答，并选择反馈或填写说明。");
    return;
  }
  try {
    await api("/api/v1/mvp/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: mvpState.characterId,
        session_id: mvpState.sessionId,
        message_id: mvpState.messageId || null,
        selected_options: selectedOptions,
        free_text: freeText,
        mode: currentMvpThread()?.mode || mvpState.mode || "immersive",
        communication_channel: currentMvpThread()?.latestResult?.communication_channel || currentMvpThread()?.channel || "in_person",
        registry_version: currentMvpThread()?.latestResult?.registry_version || null,
        message_excerpt: currentMvpThread()?.messages?.filter((entry) => entry.role === "user").slice(-1)[0]?.text || "",
        answer_excerpt: currentMvpThread()?.latestResult?.answer || "",
      }),
    });
    byId("mvp-feedback-text").value = "";
    byId("mvp-feedback-options").querySelectorAll("input").forEach((input) => { input.checked = false; });
    window.alert("反馈已保存为待处理问题，不会自动修改正式资料。");
  } catch (error) {
    window.alert(`反馈提交失败：${error.message}`);
  }
}

// Communication-medium layer: persona mode and medium are orthogonal.  A
// character keeps one session ID and one visible timeline; the backend keeps
// the two mode histories separate while sharing explicit core memory.
const MVP_CHANNEL_LABELS = { in_person: "面对面", text: "文字通讯" };

function mvpThreadKey(characterId) {
  return String(characterId);
}

function readMvpChannelPreference(characterId) {
  try {
    const value = window.localStorage.getItem("project_snow:communication_channel:" + characterId);
    return value === "text" ? "text" : "in_person";
  } catch (_) {
    return "in_person";
  }
}

function writeMvpChannelPreference(characterId, channel) {
  try {
    window.localStorage.setItem("project_snow:communication_channel:" + characterId, channel);
  } catch (_) {
    // Private browsing or a disabled storage area should not break chat.
  }
}

function ensureMvpThreads() {
  if (!mvpState.threads) mvpState.threads = {};
  return mvpState.threads;
}

function getMvpThread(characterId, mode, create = true) {
  const threads = ensureMvpThreads();
  const key = mvpThreadKey(characterId);
  // Migrate an in-memory thread created by the pre-shared-session build.
  // Browser history is intentionally ephemeral, but this keeps a hot reload
  // from silently dropping one of the two mode timelines.
  if (!threads[key] && mode && threads[String(characterId) + "::" + String(mode)]) {
    const legacy = threads[String(characterId) + "::" + String(mode)];
    threads[key] = { ...legacy, key, mode, modeSwitches: [] };
  }
  if (!threads[key] && create) {
    threads[key] = {
      key,
      characterId,
      mode,
      modeSwitches: [],
      sessionId: "",
      channel: readMvpChannelPreference(characterId),
      messages: [],
      pending: null,
      conflict: null,
      error: "",
      latestResult: null,
    };
  }
  return threads[key];
}

function currentMvpThread() {
  if (!mvpState.characterId) return null;
  return getMvpThread(mvpState.characterId, mvpState.mode || "immersive");
}

function updateMvpChannelControl(channel) {
  document.querySelectorAll("#mvp-channel [data-channel]").forEach((button) => {
    const active = button.dataset.channel === channel;
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function renderMvpSceneState(scene, channel) {
  const target = byId("mvp-scene-state");
  if (!target) return;
  const state = scene || {};
  const locationVisible = state.location_visibility === "visible_for_current_turn";
  const analyst = state.analyst_location || "未定位";
  const character = state.character_location || "未定位";
  const place = locationVisible
    ? (state.co_located ? "双方同处：" + character : "分析员：" + analyst + " · 角色：" + character)
    : (state.co_located ? "已同处（地点未主动公开）" : "当前位置未公开");
  target.textContent = "当前媒介：" + (MVP_CHANNEL_LABELS[channel] || "面对面") + " · " + place;
}

function addMvpDivider(thread, text) {
  thread.messages.push({ role: "divider", text });
}

function addMvpDividerBefore(thread, entry, text) {
  const index = thread.messages.indexOf(entry);
  const divider = { role: "divider", text };
  if (index >= 0) {
    thread.messages.splice(index, 0, divider);
  } else {
    thread.messages.push(divider);
  }
}

function renderMvpTimeline(thread) {
  const target = byId("mvp-answer");
  if (!target || !thread) return;
  target.classList.remove("empty");
  const html = [];
  if (!thread.messages.length && !thread.pending && !thread.conflict) {
    target.classList.add("empty");
    target.textContent = "模型回答、引用和临时关系证据会显示在这里。";
    return;
  }
  html.push("<div class=\"conversation-timeline\">");
  thread.messages.forEach((entry) => {
    if (entry.role === "divider") {
      html.push("<div class=\"timeline-divider\"><span>" + escapeHtml(entry.text) + "</span></div>");
      return;
    }
    const channel = entry.channel || "in_person";
    const label = channel === "text" ? "文字通讯" : "面对面";
    if (entry.role === "user") {
      html.push("<div class=\"timeline-entry user " + channel + "\"><div class=\"timeline-label\">分析员 · " + label + (entry.sending && channel === "text" ? " · 发送中…" : "") + "</div><div class=\"timeline-bubble\">" + escapeHtml(entry.text) + "</div></div>");
      return;
    }
    const blocks = Array.isArray(entry.blocks) && entry.blocks.length
      ? entry.blocks
      : [{ type: channel === "text" ? "message" : "speech", text: entry.text || "" }];
    const blockHtml = blocks.map((block) => {
      const text = escapeHtml(block.text || "");
      return block.type === "action"
        ? "<div class=\"timeline-action\">" + text + "</div>"
        : "<div class=\"timeline-bubble\">" + text + "</div>";
    }).join("");
    const result = entry.result || {};
    const citations = (result.citations || []).map((citation) =>
      "<li><strong>" + escapeHtml(citation.source_type) + " · " + escapeHtml(citation.title) + "</strong><blockquote class=\"review-quote\">" + escapeHtml(citation.excerpt || "") + "</blockquote></li>"
    ).join("");
    const citationHtml = result.citations && result.citations.length
      ? "<details class=\"mvp-citations\"><summary>引用（" + result.citations.length + "）</summary><ul>" + citations + "</ul></details>"
      : "";
    const toolCalls = (result.tool_calls || []).map((call) => escapeHtml(call.name || "只读工具")).join("、");
    const toolHtml = toolCalls ? "<p class=\"muted timeline-tools\">已调用只读工具：" + toolCalls + "</p>" : "";
    html.push("<div class=\"timeline-entry assistant " + channel + "\"><div class=\"timeline-label\">" + escapeHtml(result.character_name || "角色") + " · " + label + "</div>" + blockHtml + toolHtml + citationHtml + "</div>");
  });
  const pendingChannel = thread.pending?.userEntry?.channel || thread.channel;
  if (thread.pending?.sending && pendingChannel === "text") {
    html.push("<div class=\"timeline-entry assistant text\"><div class=\"timeline-label\">" + escapeHtml(thread.characterName || "角色") + " · 文字通讯</div><div class=\"typing-indicator\">输入中…</div></div>");
  }
  if (thread.conflict) {
    const detail = thread.conflict;
    const options = (detail.options || []).map((option) =>
      "<button type=\"button\" data-presence-action=\"" + escapeHtml(option.action) + "\" data-presence-channel=\"" + escapeHtml(option.communication_channel || "text") + "\">" + escapeHtml(option.label) + "</button>"
    ).join("");
    html.push("<div class=\"presence-choice\"><span>" + escapeHtml(detail.message || "当前地点不支持面对面交谈。") + "</span>" + options + "</div>");
  }
  if (thread.error) html.push("<p class=\"muted\">" + escapeHtml(thread.error) + "</p>");
  html.push("</div>");
  target.innerHTML = html.join("");
}

function setMvpChannel(channel, announce = true) {
  if (channel !== "text" && channel !== "in_person") return;
  const thread = currentMvpThread();
  if (!thread || thread.channel === channel) {
    updateMvpChannelControl(channel);
    return;
  }
  const previous = thread.channel;
  thread.channel = channel;
  writeMvpChannelPreference(thread.characterId, channel);
  if (announce && thread.messages.length) {
    addMvpDivider(thread, "交流媒介切换为" + MVP_CHANNEL_LABELS[channel] + "（" + MVP_CHANNEL_LABELS[previous] + "已结束）");
  }
  updateMvpChannelControl(channel);
  renderMvpSceneState(thread.latestResult?.scene_state, channel);
  renderMvpTimeline(thread);
}

function syncMvpThreadView() {
  const thread = currentMvpThread();
  if (!thread) return;
  mvpState.sessionId = thread.sessionId;
  mvpState.messageId = thread.latestResult?.message_id || "";
  mvpState.channel = thread.channel;
  thread.characterName = byId("mvp-character")?.selectedOptions[0]?.textContent || thread.characterId;
  updateMvpChannelControl(thread.channel);
  renderMvpSceneState(thread.latestResult?.scene_state, thread.channel);
  renderMvpTimeline(thread);
  byId("mvp-feedback").hidden = !thread.latestResult;
}

function resetMvpSession() {
  const characterId = byId("mvp-character")?.value || "";
  const nextMode = byId("mvp-mode")?.value || "immersive";
  const previousMode = mvpState.mode || "immersive";
  mvpState.characterId = characterId;
  mvpState.mode = nextMode;
  if (!mvpState.characterId) return;
  const thread = getMvpThread(characterId, nextMode);
  if (thread.mode && thread.mode !== nextMode && thread.messages.length) {
    addMvpDivider(thread, "对话类型切换为" + (nextMode === "assistant" ? "角色助手" : "沉浸式陪伴") + "（共享已确认背景，保留各自说话方式）");
  }
  thread.mode = nextMode;
  if (previousMode !== nextMode) thread.modeSwitches.push({ from: previousMode, to: nextMode });
  syncMvpThreadView();
}

function renderMvpAnswer(result) {
  const thread = currentMvpThread();
  if (!thread) return;
  thread.latestResult = result;
  thread.sessionId = result.session_id || thread.sessionId;
  thread.characterName = result.character_name || thread.characterName;
  renderMvpCoverage(result.coverage, result.registry_version);
  thread.messages.push({
    role: "assistant",
    channel: result.communication_channel || thread.channel,
    blocks: result.content_blocks || [],
    text: result.answer || "模型没有返回回答。",
    result,
  });
  renderMvpStyleContext(result.style_context || {});
  renderMvpSceneState(result.scene_state, result.communication_channel || thread.channel);
  renderMvpTimeline(thread);
  byId("mvp-feedback").hidden = false;
}

async function sendMvpRequest(options = {}) {
  const thread = currentMvpThread();
  if (!thread || !thread.pending) return;
  const pending = thread.pending;
  thread.pending = { ...pending, sending: true };
  thread.error = "";
  thread.conflict = null;
  pending.userEntry.sending = true;
  renderMvpTimeline(thread);
  try {
    const result = await api("/api/v1/mvp/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character_id: thread.characterId,
        message: pending.message,
        session_id: thread.sessionId || null,
        world_session_id: mvpState.worldSessionId || null,
        mode: thread.mode,
        communication_channel: options.channel || thread.channel,
        presence_action: options.presenceAction || null,
        costume_context: byId("mvp-costume").value.trim() || null,
      }),
    });
    pending.userEntry.sending = false;
    if (result.communication_channel && result.communication_channel !== pending.userEntry.channel) {
      pending.userEntry.channel = result.communication_channel;
    }
    thread.pending = null;
    thread.latestResult = result;
    thread.sessionId = result.session_id || thread.sessionId;
    mvpState.sessionId = thread.sessionId;
    mvpState.worldSessionId = result.world_session_id || mvpState.worldSessionId;
    mvpState.messageId = result.message_id || "";
    if (result.scene_state?.presence_transition?.status === "joined_character") {
      addMvpDivider(thread, "你已来到她身边，现在可以面对面交谈了");
    }
    thread.messages.push({
      role: "assistant",
      channel: result.communication_channel || thread.channel,
      blocks: result.content_blocks || [],
      text: result.answer || "模型没有返回回答。",
      result,
    });
    const transition = result.channel_transition || {};
    const nextChannel = transition.to || result.communication_channel || thread.channel;
    if (transition.status === "applied_immediately" && transition.trigger === "dialogue") {
      addMvpDividerBefore(
        thread,
        pending.userEntry,
        "已按本条消息切换为" + (MVP_CHANNEL_LABELS[nextChannel] || nextChannel)
      );
    } else if (transition.status === "applied_after_reply") {
      addMvpDivider(thread, "本轮回复结束，下一轮改用" + (MVP_CHANNEL_LABELS[nextChannel] || nextChannel));
    }
    thread.channel = nextChannel;
    writeMvpChannelPreference(thread.characterId, thread.channel);
    renderMvpStyleContext(result.style_context || {});
    renderMvpSceneState(result.scene_state, thread.channel);
    updateMvpChannelControl(thread.channel);
    renderMvpTimeline(thread);
    byId("mvp-feedback").hidden = false;
  } catch (error) {
    pending.userEntry.sending = false;
    if (error.status === 409 && error.detail?.code === "communication_context_conflict") {
      // Keep the original request so either presence choice can resend it.
      // Clearing this object made the "去找她" button a visual dead end.
      thread.pending = { ...pending, sending: false };
      thread.conflict = error.detail;
      // The API raises this conflict before calling the model. Render the
      // character's invitation as a normal reply first, then place the two
      // presence choices below it. The original analyst message stays in the
      // timeline and remains attached to the pending retry.
      const invitation = error.detail.character_reply || "我现在不在你身边。要过来找我，还是先用通讯器聊？";
      const hasInvitation = thread.messages.some(
        (entry) => entry.role === "assistant" && entry.result?.presence_invitation
      );
      if (!hasInvitation) {
        thread.messages.push({
          role: "assistant",
          channel: "in_person",
          blocks: error.detail.content_blocks || [{ type: "speech", text: invitation }],
          text: invitation,
          result: {
            character_name: error.detail.character_name || thread.characterName || "角色",
            communication_channel: "in_person",
            citations: [],
            presence_invitation: true,
          },
        });
      }
      renderMvpSceneState(error.detail.scene_state, thread.channel);
    } else {
      thread.pending = null;
      thread.error = "MVP 对话失败：" + error.message;
      byId("mvp-feedback").hidden = true;
    }
    renderMvpTimeline(thread);
  }
}

async function submitMvpChat(event) {
  event.preventDefault();
  const characterId = byId("mvp-character").value;
  const message = byId("mvp-message").value.trim();
  const mode = byId("mvp-mode").value || "immersive";
  if (!characterId || !message) {
    byId("mvp-answer").textContent = "请选择角色并输入问题。";
    return;
  }
  mvpState.characterId = characterId;
  mvpState.mode = mode;
  const thread = getMvpThread(characterId, mode);
  if (thread.mode && thread.mode !== mode && thread.messages.length) {
    addMvpDivider(thread, "对话类型切换为" + (mode === "assistant" ? "角色助手" : "沉浸式陪伴") + "（共享已确认背景，保留各自说话方式）");
  }
  thread.mode = mode;
  if (thread.pending) return;
  const userEntry = { role: "user", channel: thread.channel, text: message, sending: true };
  thread.messages.push(userEntry);
  thread.pending = { message, userEntry };
  byId("mvp-message").value = "";
  renderMvpTimeline(thread);
  await sendMvpRequest({ channel: thread.channel });
}

const FEEDBACK_STATUS_LABELS = {
  pending_triage: "待处理",
  planned: "计划修复",
  resolved: "已解决",
  ignored: "忽略",
};
const FEEDBACK_RESOLUTION_LABELS = {
  fixed_verified: "已验证修复",
  needs_verification: "待验证",
  regression_candidate: "修复后再现",
  not_reproduced: "未能复现",
  duplicate: "重复反馈",
  open: "开放问题",
};

async function loadFeedbackInbox() {
  const category = byId("feedback-category-filter")?.value || "";
  const feedbackStatus = byId("feedback-status-filter")?.value || "";
  const characterId = byId("feedback-character-filter")?.value || "";
  const resolutionStatus = byId("feedback-resolution-filter")?.value || "";
  const target = byId("feedback-inbox-list");
  if (!target) return;
  target.textContent = "正在读取反馈…";
  try {
    const result = await api(`/api/v1/mvp/feedback${queryString({
      limit: 200,
      category,
      feedback_status: feedbackStatus,
      character_id: characterId,
      resolution_status: resolutionStatus,
    })}`);
    const categories = result.categories || [];
    const categoryLabels = Object.fromEntries(categories.map((item) => [item.id, item.label]));
    const categoryFilter = byId("feedback-category-filter");
    if (categoryFilter && categoryFilter.options.length <= 1) {
      categoryFilter.innerHTML = '<option value="">全部类别</option>' + categories.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`
      )).join("");
      categoryFilter.value = category;
    }
    const items = result.feedback || [];
    const characterFilter = byId("feedback-character-filter");
    if (characterFilter && characterFilter.options.length <= 1) {
      const characters = [...new Map(items.map((item) => [item.character_id, item.character_name])).entries()];
      characterFilter.innerHTML = '<option value="">全部角色</option>' + characters.map(([id, name]) => (
        `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`
      )).join("");
      characterFilter.value = characterId;
    }
    byId("feedback-inbox-summary").textContent = `当前筛选 ${result.total || 0} 条反馈；处理状态以追加审计记录保存。`;
    target.innerHTML = items.map((item) => {
      const legacy = (item.selected_options || []).join("、");
      const categoryLabel = categoryLabels[item.category] || item.category || legacy || "未分类";
      const occurrenceLabel = item.issue_occurrence === "duplicate"
        ? `重复反馈${item.duplicate_of ? " · 同问题族" : ""}`
        : "首次反馈";
      const issueKey = item.issue_key || "other";
      return `<article class="feedback-inbox-item">
        <header><strong>${escapeHtml(item.character_name || "未知角色")} · ${escapeHtml(categoryLabel)}</strong><span class="chip">${escapeHtml(occurrenceLabel)}</span><span class="chip">${escapeHtml(FEEDBACK_STATUS_LABELS[item.status] || item.status || "待处理")}</span><span class="chip">${escapeHtml(FEEDBACK_RESOLUTION_LABELS[item.resolution_status] || item.resolution_status || "待验证")}</span></header>
        <p class="muted feedback-issue-key">问题族：${escapeHtml(issueKey)}</p>
        <p>${escapeHtml(item.free_text || "（未填写说明）")}</p>
        <details><summary>查看对话上下文</summary><p><strong>分析员：</strong>${escapeHtml(item.message_excerpt || "")}</p><p><strong>角色：</strong>${escapeHtml(item.answer_excerpt || "")}</p><p class="muted">${escapeHtml(item.mode || "")} · ${escapeHtml(item.communication_channel || "")} · ${escapeHtml(item.created_at || "")}</p></details>
        <div class="feedback-triage-actions">
          <button type="button" class="secondary" data-feedback-id="${escapeHtml(item.feedback_id)}" data-feedback-status="planned">计划修复</button>
          <button type="button" class="secondary" data-feedback-id="${escapeHtml(item.feedback_id)}" data-feedback-status="resolved">已解决</button>
          <button type="button" class="secondary" data-feedback-id="${escapeHtml(item.feedback_id)}" data-feedback-status="ignored">忽略</button>
          <button type="button" class="secondary" data-feedback-id="${escapeHtml(item.feedback_id)}" data-feedback-status="pending_triage">恢复待处理</button>
        </div>
      </article>`;
    }).join("") || "当前筛选下没有反馈。";
  } catch (error) {
    target.textContent = `反馈读取失败：${error.message}`;
  }
}

async function triageFeedback(button) {
  const feedbackId = button.dataset.feedbackId;
  const feedbackStatus = button.dataset.feedbackStatus;
  if (!feedbackId || !feedbackStatus) return;
  const note = window.prompt("处理备注（可留空）", "") ?? "";
  button.disabled = true;
  try {
    await api(`/api/v1/mvp/feedback/${encodeURIComponent(feedbackId)}/triage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: feedbackStatus, note }),
    });
    await loadFeedbackInbox();
  } catch (error) {
    window.alert(`反馈状态更新失败：${error.message}`);
    button.disabled = false;
  }
}

byId("mvp-channel")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-channel]");
  if (button) setMvpChannel(button.dataset.channel);
});
byId("mvp-answer")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-presence-action]");
  if (!button) return;
  const thread = currentMvpThread();
  if (!thread?.pending || !thread.conflict) return;
  const channel = button.dataset.presenceChannel || "text";
  setMvpChannel(channel);
  if (button.dataset.presenceAction === "join_character") {
    addMvpDivider(thread, "你正去找她…");
    renderMvpTimeline(thread);
  }
  sendMvpRequest({
    channel,
    presenceAction: button.dataset.presenceAction === "join_character" ? "join_character" : null,
  });
});

byId("search").addEventListener("click", search);
byId("load-persona").addEventListener("click", loadPersona);
byId("load-graph").addEventListener("click", loadGraph);
byId("refresh-review").addEventListener("click", loadRelationReview);
byId("refresh-entity-review").addEventListener("click", loadEntityReview);
byId("load-audit-sample").addEventListener("click", loadAuditSample);
byId("load-more-review").addEventListener("click", loadMoreRelationReview);
byId("load-more-entity-review").addEventListener("click", loadMoreEntityReview);
byId("reset-review-filters").addEventListener("click", resetReviewFilters);
["review-tier", "review-relation-type", "review-source-type", "review-risk-level", "review-machine-verdict"].forEach((id) => {
  byId(id).addEventListener("change", loadRelationReview);
});
byId("review-groups").addEventListener("click", (event) => {
  const decisionButton = event.target.closest("button[data-review-decision]");
  const openButton = event.target.closest("button[data-review-open]");
  const collapseButton = event.target.closest("button[data-review-collapse]");
  const moreCandidatesButton = event.target.closest("button[data-review-more-candidates]");
  if (decisionButton) decideRelationCandidate(decisionButton);
  else if (openButton) openReviewGroup(openButton);
  else if (collapseButton) collapseReviewGroup(collapseButton);
  else if (moreCandidatesButton) loadMoreGroupCandidates(moreCandidatesButton);
});
byId("entity-review-candidates").addEventListener("click", (event) => {
  const decisionButton = event.target.closest("button[data-entity-review-decision]");
  if (decisionButton) decideEntityNodeCandidate(decisionButton);
});
byId("character").addEventListener("change", () => { loadPersona(); loadGraph(); });
byId("mvp-character").addEventListener("change", () => { resetMvpSession(); loadMvpQuestions(); });
const mvpModeControl = byId("mvp-mode");
if (mvpModeControl) mvpModeControl.addEventListener("change", () => resetMvpSession());
byId("mvp-questions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mvp-question]");
  if (button) byId("mvp-message").value = button.dataset.mvpQuestion;
});
byId("mvp-chat-form").addEventListener("submit", submitMvpChat);
byId("mvp-submit-feedback").addEventListener("click", submitMvpFeedback);
byId("refresh-feedback-inbox")?.addEventListener("click", loadFeedbackInbox);
["feedback-category-filter", "feedback-status-filter", "feedback-character-filter", "feedback-resolution-filter"].forEach((id) => {
  byId(id)?.addEventListener("change", loadFeedbackInbox);
});
byId("feedback-inbox-list")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-feedback-id]");
  if (button) triageFeedback(button);
});

loadHealth();
loadCharacters();
loadMvpStatus();
loadRelationReview();
loadEntityReview();
loadFeedbackInbox();
