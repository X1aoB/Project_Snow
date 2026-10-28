// The only paid feature. Consent storage contains no raw credentials; durable
// requests never contain credentials. The optional storage hooks are supplied by
// the host so tab retention can use sessionStorage and other retention can use
// localStorage without coupling this module to either global.
const clone = value => structuredClone(value);
const PREFIX = "proactive_request_v1:", NOTICE = "proactive-cost-2";
const AUTH_KEY = "project-snow-public:proactive-consent:v1", MAX_CONFIRMATION_MS = 7 * 24 * 60 * 60 * 1000;
const DURATION_VALUES = new Set(["tab", "2h", "24h", "3d", "7d"]);
const RETENTION_VALUES = new Set(["tab", "2h", "24h", "3d", "7d"]);
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
  let storage = null, storageKind = "", storageLoaded = false, storedAuth = null, expiryTimer = 0;
  let authRetention = "", invalidatedGeneration = -1, contextQueue = Promise.resolve(), storageQueue = Promise.resolve();
  const snapshot = () => adapter.snapshot();
  const token = () => ({ generation, epoch: snapshot().historyEpoch });
  const current = captured => !disposed && captured.generation === generation && captured.epoch === snapshot().historyEpoch
    && snapshot().writerOwned !== false;
  const visible = () => adapter.visible?.() !== false;
  const savedEnabled = () => adapter.preferences()?.proactivePreferences?.enabled === true;
  const preferences = () => ({ enabled: savedEnabled() && Boolean(consent), requiresConfirmation: !consent,
    serverEnabled, noticeVersion: NOTICE, busy: operation !== null,
    duration: consent?.duration ?? null, expiresAt: consent?.expires_at ?? null });
  const emit = () => adapter.emit?.("proactive-changed", preferences());

  function retentionPolicy(value) {
    const configured = typeof adapter.retention === "function" ? adapter.retention(value) : adapter.retention;
    const candidate = configured === undefined || configured === null ? value?.retention : configured;
    return RETENTION_VALUES.has(candidate) ? candidate : "tab";
  }
  function storageKindFor(retention) {
    return retention === "tab" ? "tab" : "local";
  }
  async function storageFor(kind) {
    if (kind === storageKind && storageLoaded) return storage;
    storageKind = kind; storageLoaded = true; storedAuth = null;
    storage = null;
    try {
      if (typeof adapter.storageForRetention === "function") storage = await adapter.storageForRetention(kind);
      else if (typeof adapter.getCredentialStorage === "function") storage = await adapter.getCredentialStorage(kind);
      else if (adapter.storageForRetention && typeof adapter.storageForRetention === "object") {
        storage = adapter.storageForRetention[kind] || null;
      } else if (adapter.getCredentialStorage && typeof adapter.getCredentialStorage === "object") {
        storage = adapter.getCredentialStorage[kind] || null;
      }
    } catch { storage = null; }
    return storage;
  }
  function storageTask(work) {
    const pending = storageQueue.then(work, work);
    storageQueue = pending.catch(() => {});
    return pending;
  }
  async function storageRead(source) {
    if (!source) return null;
    try {
      return await storageTask(async () => {
        if (typeof source.getItem === "function") return source.getItem(AUTH_KEY);
        if (typeof source.load === "function") return source.load(AUTH_KEY);
        if (typeof source.get === "function") return source.get(AUTH_KEY);
        if (typeof source === "object" && typeof source.has === "function") return source.has(AUTH_KEY) ? source.get(AUTH_KEY) : null;
        return null;
      });
    } catch {}
    return null;
  }
  async function storageWrite(source, value) {
    if (!source) return;
    try {
      await storageTask(async () => {
        if (typeof source.setItem === "function") { source.setItem(AUTH_KEY, JSON.stringify(value)); return; }
        if (typeof source.save === "function") { await source.save(AUTH_KEY, clone(value)); return; }
        if (typeof source.set === "function") { await source.set(AUTH_KEY, clone(value)); }
      });
    } catch {}
  }
  async function storageClear(source = storage) {
    if (!source) return;
    try {
      await storageTask(async () => {
        if (typeof source.removeItem === "function") { source.removeItem(AUTH_KEY); return; }
        if (typeof source.delete === "function") { await source.delete(AUTH_KEY); return; }
        // A zero-argument clear() cannot target one record safely.
        if (typeof source.clear === "function" && source.clear.length > 0) await source.clear(AUTH_KEY);
      });
    } catch {}
  }
  function normalizedAuth(value) {
    if (typeof value === "string") {
      try { value = JSON.parse(value); } catch { return null; }
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const session = typeof value.model_session_id === "string" ? value.model_session_id : "";
    const identity = typeof value.model_identity === "string" ? value.model_identity : "";
    const confirmedAt = Number(value.confirmed_at);
    const candidate = value.consent && typeof value.consent === "object" ? value.consent : value;
    const token = typeof candidate.consent_token === "string" ? candidate.consent_token : "";
    const noticeVersion = candidate.notice_version;
    const expiresAt = Date.parse(candidate.expires_at);
    if (!session || !identity || !Number.isFinite(confirmedAt) || confirmedAt <= 0) return null;
    if (!token && value.consent !== null && value.consent !== undefined) return null;
    if (!token) return { model_identity: identity, model_session_id: session, confirmed_at: confirmedAt, consent: null };
    if (noticeVersion !== NOTICE || candidate.model_session_id !== session || !Number.isFinite(expiresAt)) return null;
    const boundedExpiry = Math.min(expiresAt, confirmedAt + MAX_CONFIRMATION_MS);
    if (boundedExpiry <= Date.now()) return null;
    const duration = DURATION_VALUES.has(candidate.duration) ? candidate.duration : "tab";
    return { model_identity: identity, model_session_id: session, confirmed_at: confirmedAt,
      consent: { consent_token: token, notice_version: NOTICE, model_session_id: session, duration,
        expires_at: new Date(boundedExpiry).toISOString() } };
  }
  async function loadAuth(kind) {
    const source = await storageFor(kind);
    if (!source) return null;
    const raw = await storageRead(source), value = normalizedAuth(raw);
    if (!value) {
      if (raw !== null && raw !== undefined) await storageClear(source);
      return null;
    }
    storedAuth = value;
    return value;
  }
  async function clearAuthStorage(source, kind) {
    storedAuth = null;
    const targetKind = kind || storageKind || storageKindFor(retentionPolicy());
    const target = source === undefined ? (storage || await storageFor(targetKind)) : source;
    await storageClear(target);
  }
  function clearExpiryTimer() {
    if (expiryTimer) { clearTimeout(expiryTimer); expiryTimer = 0; }
  }
  function armExpiry(expiresAt) {
    clearExpiryTimer();
    const delay = Date.parse(expiresAt) - Date.now();
    if (!Number.isFinite(delay) || delay <= 0) return;
    expiryTimer = setTimeout(() => {
      expiryTimer = 0;
      if (!consent || Date.parse(consent.expires_at) > Date.now()) return;
      consent = null; modelSessionId = ""; void clearAuthStorage(); emit();
    }, Math.min(delay, 2147483647));
  }
  async function restoreAuth(kind, digest, expectedGeneration) {
    const saved = await loadAuth(kind);
    if (expectedGeneration !== generation || disposed) return null;
    if (saved?.model_identity !== digest) return null;
    modelSessionId = saved.model_session_id;
    consent = saved.consent ? { ...clone(saved.consent), confirmed_at: saved.confirmed_at } : null;
    if (consent) armExpiry(consent.expires_at);
    return saved;
  }
  function persistedConsent() {
    if (!consent || typeof consent.consent_token !== "string" || !consent.consent_token) return null;
    return { consent_token: consent.consent_token, notice_version: NOTICE, model_session_id: modelSessionId,
      duration: DURATION_VALUES.has(consent.duration) ? consent.duration : "tab", expires_at: consent.expires_at };
  }
  async function saveAuth(expectedGeneration = generation) {
    if (expectedGeneration !== generation || !storage || !modelSessionId || !modelIdentity) return;
    const value = { version: 1, model_identity: modelIdentity, model_session_id: modelSessionId,
      confirmed_at: consent?.confirmed_at || storedAuth?.confirmed_at || Date.now(), consent: persistedConsent() };
    await storageWrite(storage, value);
    if (expectedGeneration === generation && !disposed) storedAuth = normalizedAuth(value) || (consent ? value : null);
  }
  async function contextImpl(characterId) {
    const startedGeneration = generation;
    const value = await adapter.context(characterId);
    if (startedGeneration !== generation || disposed) return { ...value, model_session_id: modelSessionId };
    const bytes = new TextEncoder().encode(JSON.stringify([value.provider, value.model, value.reasoning_effort || null, value.credential]));
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(byte => byte.toString(16).padStart(2, "0")).join("");
    const retention = retentionPolicy(value), kind = storageKindFor(retention), identityChanged = digest !== modelIdentity;
    const retentionChanged = Boolean(authRetention) && retention !== authRetention;
    const forceFresh = invalidatedGeneration === startedGeneration;
    if (identityChanged || retentionChanged || !modelSessionId || forceFresh) {
      const previousIdentity = modelIdentity, previousKind = authRetention, previousStorage = storage;
      modelIdentity = digest; modelSessionId = ""; consent = null; storedAuth = null; clearExpiryTimer(); authRetention = retention;
      const restored = forceFresh || retentionChanged ? null : await restoreAuth(kind, digest, startedGeneration);
      if (startedGeneration !== generation || disposed) return { ...value, model_session_id: modelSessionId };
      if (!restored) {
        const currentStorage = await storageFor(kind);
        await storageClear(currentStorage);
        if (previousStorage && previousStorage !== currentStorage && (previousIdentity || previousKind)) {
          await storageClear(previousStorage);
        }
        if (startedGeneration !== generation || disposed) return { ...value, model_session_id: modelSessionId };
        modelSessionId = crypto.randomUUID(); await saveAuth(startedGeneration);
      } else if (retentionChanged && previousStorage && previousStorage !== storage) {
        await storageClear(previousStorage);
      }
      if (forceFresh && invalidatedGeneration === startedGeneration) invalidatedGeneration = -1;
    }
    serverEnabled = value.enabled === true && value.notice_version === NOTICE;
    if (consent && Date.parse(consent.expires_at) <= Date.now()) {
      consent = null; modelSessionId = ""; clearExpiryTimer(); await clearAuthStorage();
      if (startedGeneration !== generation || disposed) return { ...value, model_session_id: modelSessionId };
      modelSessionId = crypto.randomUUID(); await saveAuth(startedGeneration);
    }
    return { ...value, model_session_id: modelSessionId };
  }
  function context(characterId) {
    const pending = contextQueue.then(() => contextImpl(characterId), () => contextImpl(characterId));
    contextQueue = pending.catch(() => {});
    return pending;
  }
  function exclusive(work) {
    if (operation) return Promise.resolve(fail("busy"));
    const pending = Promise.resolve().then(work); operation = pending;
    return pending.finally(() => { if (operation === pending) operation = null; emit(); });
  }
  function invalidate() {
    generation += 1; invalidatedGeneration = generation; consent = null; modelIdentity = ""; modelSessionId = "";
    authRetention = ""; clearExpiryTimer();
    const pending = clearAuthStorage(); emit(); return pending;
  }
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
      const duration = DURATION_VALUES.has(options.duration) ? options.duration : "tab";
      const response = await adapter.request("/rendezvous/consent", { method: "POST", body: JSON.stringify({
        provider: value.provider, model: value.model, reasoning_effort: value.reasoning_effort || null,
        credential: value.credential, model_session_id: value.model_session_id,
        notice_version: NOTICE, accepted_cost_notice: true, duration }) });
      if (!current(captured)) return fail("inactive");
      const latest = await context(snapshot().selected);
      if (!current(captured) || latest.model_session_id !== value.model_session_id) return fail("model_changed");
      if (!response?.consent_token || response.notice_version !== NOTICE || response.model_session_id !== value.model_session_id
          || !Number.isFinite(Date.parse(response.expires_at)) || Date.parse(response.expires_at) <= Date.now()) return fail("invalid_response");
      await adapter.savePreferences({ proactivePreferences: { enabled: true } });
      if (!current(captured)) return fail("inactive");
      consent = { ...clone(response), confirmed_at: Date.now() };
      consent.duration = DURATION_VALUES.has(response.duration) ? response.duration : duration;
      const maxExpiry = new Date(consent.confirmed_at + MAX_CONFIRMATION_MS).toISOString();
      if (Date.parse(consent.expires_at) > Date.parse(maxExpiry)) consent.expires_at = maxExpiry;
      armExpiry(consent.expires_at); await saveAuth(); return { ok: true, ...preferences() };
    });
  }
  async function disable() {
    await invalidate();
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
  return { preferences, enable, disable, refresh, send, requests, recover, invalidate, clear: invalidate,
    setPreferences: patch => patch?.enabled === true ? enable(patch) : disable(),
    dispose() { disposed = true; clearExpiryTimer(); void invalidate(); } };
}
