// The only paid feature. Consent stays in memory; durable requests never contain credentials.
const clone = value => structuredClone(value);
const PREFIX = "proactive_request_v1:", NOTICE = "proactive-cost-1";
const fail = reason => ({ ok: false, reason });
const publicRecord = record => record ? { key: record.key, requestId: record.envelope.request_id,
  characterId: record.envelope.character_id, rendezvousId: record.envelope.rendezvous_id,
  status: record.status, createdAt: record.createdAt, usage: clone(record.usage || null), modelCalled: record.modelCalled ?? null } : null;
function safeUsage(value) {
  return Object.fromEntries(["provider_calls", "prompt_tokens", "completion_tokens", "total_tokens"].map(key =>
    [key, Number.isSafeInteger(value?.[key]) && value[key] >= 0 ? value[key] : null]));
}
function receiptFor(record) {
  return { key: `proactive_usage_v1:${record.envelope.request_id}`, requestId: record.envelope.request_id,
    characterId: record.envelope.character_id, createdAt: record.createdAt, source: "proactive_reminder",
    model: record.envelope.model, usage: safeUsage(record.usage), modelCalled: record.modelCalled ?? null, status: record.status };
}

export function createProactiveClient(adapter) {
  let consent = null, modelIdentity = "", modelSessionId = "", generation = 0, disposed = false;
  let operation = null, serverEnabled = false;
  const snapshot = () => adapter.snapshot();
  const token = () => ({ generation, epoch: snapshot().historyEpoch });
  const current = captured => !disposed && captured.generation === generation && captured.epoch === snapshot().historyEpoch
    && snapshot().writerOwned !== false;
  const visible = () => adapter.visible?.() !== false;
  const savedEnabled = () => adapter.preferences()?.proactivePreferences?.enabled === true;
  const preferences = () => ({ enabled: savedEnabled() && Boolean(consent), requiresConfirmation: !consent,
    serverEnabled, noticeVersion: NOTICE, busy: operation !== null });
  const emit = () => adapter.emit?.("proactive-changed", preferences());
  async function context(characterId) {
    const value = await adapter.context(characterId);
    const bytes = new TextEncoder().encode(JSON.stringify([value.provider, value.model, value.reasoning_effort || null, value.credential]));
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(byte => byte.toString(16).padStart(2, "0")).join("");
    if (digest !== modelIdentity) { modelIdentity = digest; modelSessionId = crypto.randomUUID(); consent = null; }
    serverEnabled = value.enabled === true && value.notice_version === NOTICE;
    if (consent && Date.parse(consent.expires_at) <= Date.now()) consent = null;
    return { ...value, model_session_id: modelSessionId };
  }
  function exclusive(work) {
    if (operation) return Promise.resolve(fail("busy"));
    const pending = Promise.resolve().then(work); operation = pending;
    return pending.finally(() => { if (operation === pending) operation = null; emit(); });
  }
  function invalidate() { generation += 1; consent = null; modelIdentity = ""; modelSessionId = ""; emit(); }
  async function refresh() {
    if (disposed) return preferences();
    const captured = token();
    await context(snapshot().selected);
    if (!current(captured)) consent = null;
    emit(); return preferences();
  }
  function enable(options = {}) {
    return exclusive(async () => {
      if (options.acceptedCostNotice !== true) return fail("cost_confirmation_required");
      const captured = token(), value = await context(snapshot().selected);
      if (!current(captured) || !visible()) return fail("inactive");
      if (!serverEnabled) return fail("server_disabled");
      if (!value.ready || !value.credential) return fail("model_required");
      const response = await adapter.request("/rendezvous/consent", { method: "POST", body: JSON.stringify({
        provider: value.provider, model: value.model, reasoning_effort: value.reasoning_effort || null,
        credential: value.credential, model_session_id: value.model_session_id,
        notice_version: NOTICE, accepted_cost_notice: true }) });
      if (!current(captured)) return fail("inactive");
      const latest = await context(snapshot().selected);
      if (!current(captured) || latest.model_session_id !== value.model_session_id) return fail("model_changed");
      if (!response?.consent_token || response.notice_version !== NOTICE || response.model_session_id !== value.model_session_id
          || !Number.isFinite(Date.parse(response.expires_at)) || Date.parse(response.expires_at) <= Date.now()) return fail("invalid_response");
      await adapter.savePreferences({ proactivePreferences: { enabled: true } });
      if (!current(captured)) return fail("inactive");
      consent = clone(response); return { ok: true, ...preferences() };
    });
  }
  async function disable() {
    invalidate();
    if (!disposed && snapshot().writerOwned !== false) await adapter.savePreferences({ proactivePreferences: { enabled: false } });
    return preferences();
  }
  const keyFor = reminder => `${PREFIX}${reminder.dayKey}:${reminder.characterId}:${reminder.rendezvousId}`;
  function validReminder(reminder) {
    const state = snapshot(), world = state.rendezvous;
    if (!reminder?.rendezvousId || reminder.status !== "fired" || reminder.dayKey !== state.localDayKey
        || !world?.authoritative || world.dayKey !== state.localDayKey) return false;
    const active = adapter.activeReminders().find(item => item.id === reminder.id && item.status === "fired"
      && item.rendezvousId === reminder.rendezvousId && item.characterId === reminder.characterId);
    return Boolean(active && world.items.some(item => item.character_id === reminder.characterId
      && item.rendezvous_id === reminder.rendezvousId && item.location_id === reminder.locationId
      && item.schedule_date === state.localDayKey));
  }
  async function retainUnknown(record, captured) {
    if (!current(captured)) return fail("inactive");
    const updated = { ...record, status: "unknown" };
    updated.usageReceipt = receiptFor(updated);
    await adapter.saveState(updated.usageReceipt.key, updated.usageReceipt);
    if (!current(captured)) return fail("inactive");
    await adapter.saveState(record.key, updated);
    adapter.emit?.("usage-saved", clone(updated.usageReceipt));
    return { ok: false, reason: "result_unknown", ...publicRecord(updated) };
  }
  async function accept(response, record, captured) {
    if (!current(captured)) return fail("inactive");
    if (response?.request_id !== record.envelope.request_id || response.character_id !== record.envelope.character_id
        || response.source !== "proactive_reminder" || !["completed", "failed", "unknown"].includes(response.status)) {
      return retainUnknown(record, captured);
    }
    const updated = { ...record, status: response.status, usage: safeUsage(response.usage),
      modelCalled: typeof response.model_called === "boolean" ? response.model_called : null };
    updated.usageReceipt = receiptFor(updated);
    if (response.status === "completed") {
      if (!response.message_id || !Array.isArray(response.content_blocks) || !response.content_blocks.length
          || response.content_blocks.some(block => block?.type !== "message" || typeof block.text !== "string" || !block.text.trim())) {
        return retainUnknown(record, captured);
      }
      await adapter.commit(clone(response), clone(updated));
    } else {
      await adapter.saveState(updated.usageReceipt.key, updated.usageReceipt);
      if (!current(captured)) return fail("inactive");
      await adapter.saveState(record.key, updated);
    }
    adapter.emit?.("usage-saved", clone(updated.usageReceipt));
    return { ok: response.status === "completed", ...publicRecord(updated) };
  }
  async function dispatch(record, captured, authorization, recovery) {
    let result;
    try {
      result = await adapter.request("/rendezvous/message", { method: "POST", timeoutMs: 180000,
        ...(recovery ? { headers: { "X-Snow-Recovery-Only": "1" } } : {}),
        body: JSON.stringify({ ...record.envelope, credential: authorization?.credential || "", consent_token: authorization?.consent_token || "" }) });
    } catch {
      // Never generate a second request after an uncertain provider outcome.
      return retainUnknown(record, captured);
    }
    return accept(result, record, captured);
  }
  function send(reminder) {
    return exclusive(async () => {
      const captured = token();
      if (!current(captured) || !visible() || !validReminder(reminder)) return fail("inactive");
      if (!savedEnabled() || !consent) return fail("cost_confirmation_required");
      return adapter.withRequest(reminder.characterId, async () => {
        const value = await context(reminder.characterId);
        if (!current(captured) || !visible() || !validReminder(reminder)) return fail("inactive");
        if (!serverEnabled) return fail("server_disabled");
        if (!value.ready || !consent || !savedEnabled()) return fail("cost_confirmation_required");
        const key = keyFor(reminder), existing = await adapter.loadState(key);
        if (!current(captured)) return fail("inactive");
        if (existing) return { ok: existing.status === "completed", reason: "recovery_required", ...publicRecord(existing) };
        const record = { key, status: "prepared", dispatched: false, createdAt: Date.now(), envelope: {
          request_id: crypto.randomUUID(), character_id: reminder.characterId, rendezvous_id: reminder.rendezvousId,
          segment_id: value.segment_id, state_package: value.state_package, session_context_package: value.session_context_package,
          provider: value.provider, model: value.model, reasoning_effort: value.reasoning_effort || null,
          model_session_id: value.model_session_id, notice_version: NOTICE } };
        await adapter.saveState(key, clone(record));
        const latest = await context(reminder.characterId);
        if (!current(captured) || !visible() || !validReminder(reminder) || !consent || !savedEnabled()
            || latest.model_session_id !== value.model_session_id || !serverEnabled) return fail("inactive");
        record.dispatched = true; record.status = "pending";
        await adapter.saveState(key, clone(record));
        const dispatchContext = await context(reminder.characterId);
        if (!current(captured) || !visible() || !consent || !savedEnabled() || !validReminder(reminder)
            || !serverEnabled || !dispatchContext.ready || dispatchContext.model_session_id !== value.model_session_id
            || adapter.contextCurrent?.(dispatchContext) === false) return fail("inactive");
        return dispatch(record, captured, { credential: dispatchContext.credential, consent_token: consent.consent_token }, false);
      });
    });
  }
  async function requests() {
    const captured = token(), records = await adapter.readRequests();
    return current(captured) ? records.filter(record => record?.key?.startsWith(PREFIX) && record.envelope).map(publicRecord) : [];
  }
  function recover(requestId) {
    return exclusive(async () => {
      const captured = token();
      if (!current(captured) || !visible()) return fail("inactive");
      const records = await adapter.readRequests(), record = records.find(item => item.envelope?.request_id === requestId);
      if (!current(captured) || !record?.key?.startsWith(PREFIX)) return fail("missing");
      if (record.status === "completed") return { ok: true, ...publicRecord(record) };
      if (!record.dispatched) return { ...fail("not_dispatched"), ...publicRecord(record) };
      return adapter.withRequest(record.envelope.character_id, () => current(captured) && visible()
        ? dispatch(record, captured, null, true) : fail("inactive"));
    });
  }
  return { preferences, enable, disable, refresh, send, requests, recover, invalidate,
    setPreferences: patch => patch?.enabled === true ? enable(patch) : disable(),
    dispose() { disposed = true; invalidate(); } };
}
