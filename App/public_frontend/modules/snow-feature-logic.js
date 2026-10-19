// Local feature data only. The adapter owns persistence and request admission.
import { createReadingLogic, normalizeReadingPreferences, importReadingPreferences } from "/modules/snow-reading-logic.js";
import { createReminderLogic, normalizeRendezvousReminders, mergeRendezvousReminders } from "/modules/snow-reminder-logic.js";
export { importReadingPreferences };
const copy = value => structuredClone(value);
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const string = value => typeof value === "string" ? value : "";
const list = value => Array.isArray(value) ? value : [];
const stamp = value => Number.isFinite(Number(value)) ? Number(value) : Date.parse(value) || 0;
const dayFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit",
});
function dayKey(timestamp) {
  const parts = Object.fromEntries(dayFormatter.formatToParts(new Date(timestamp)).map(item => [item.type, item.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function identifiers(values, limit) {
  return [...new Set(list(values).filter(value => typeof value === "string" && value && value.length <= 256))].slice(-limit);
}
function favoriteId(ref) {
  if (!ref || !string(ref.messageId) || ![ref.blockIndex, ref.start, ref.end].every(Number.isInteger)
      || ref.blockIndex < 0 || ref.start < 0 || ref.end <= ref.start) return "";
  return `${ref.messageId}:${ref.blockIndex}:${ref.start}-${ref.end}`;
}
function favoriteLines(values) {
  const entries = new Map();
  for (const value of list(values)) {
    const id = favoriteId(value);
    if (!id || !string(value.characterId) || !string(value.text) || Array.from(value.text).length > 240) continue;
    entries.set(id, { ...copy(value), id });
  }
  return [...entries.values()].sort((a, b) => stamp(b.createdAt) - stamp(a.createdAt) || a.id.localeCompare(b.id)).slice(0, 200);
}
function mergeUsageReceipt(previous, incoming) {
  const result = { ...object(previous), ...copy(object(incoming)) };
  for (const [key, aliases, valid] of [
    ["provider_calls", ["provider_calls", "model_calls", "call_count"], value => count(value) !== null],
    ["total_tokens", ["total_tokens", "totalTokens"], value => count(value) !== null && value > 0],
  ]) {
    const observed = [...aliases.map(alias => incoming?.[alias]), ...aliases.map(alias => previous?.[alias])].filter(valid);
    if (observed.length) result[key] = Math.max(...observed);
  }
  if (!string(result.model) && string(previous?.model)) result.model = previous.model;
  return result;
}
export function normalizeExtraUsage(values) {
  const entries = new Map();
  for (const value of list(values)) {
    const item = object(value), requestId = string(item.requestId || item.request_id);
    if (!requestId || requestId.length > 256 || item.kind !== "summary") continue;
    entries.set(requestId, { ...mergeUsageReceipt(entries.get(requestId), item), kind: "summary", requestId });
  }
  return [...entries.values()];
}
export function mergeExtraUsage(previous, incoming) {
  return normalizeExtraUsage([...list(previous), ...list(incoming)]);
}
export function normalizeProactiveUsage(values) {
  const entries = new Map();
  for (const raw of list(values)) {
    const item = object(raw), requestId = string(item.requestId || item.request_id), characterId = messageCharacter(item);
    const createdAt = stamp(item.createdAt ?? item.created_at);
    if (!requestId || requestId.length > 256 || !characterId || characterId.length > 256 || !createdAt || !Number.isFinite(new Date(createdAt).getTime())) continue;
    const previous = entries.get(requestId), merged = mergeUsageReceipt(previous?.usage, item.usage), usage = {};
    for (const key of ["provider_calls", "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"]) {
      const observed = [merged[key], previous?.usage?.[key]].filter(value => Number.isSafeInteger(value) && value >= 0);
      usage[key] = observed.length ? Math.max(...observed) : null;
    }
    const modelCalled = item.modelCalled === true || previous?.modelCalled === true ? true : item.modelCalled === false ? false : null;
    entries.set(requestId, { requestId, characterId, createdAt: previous ? Math.min(previous.createdAt, createdAt) : createdAt,
      source: "proactive_reminder", model: string(item.model).slice(0, 256) || previous?.model || "", usage, modelCalled,
      status: previous?.status === "completed" || item.status === "completed" ? "completed"
        : ["pending", "failed", "unknown"].includes(item.status) ? item.status : "unknown" });
  }
  return [...entries.values()];
}
export function portableFeatureState(appStates, localPreferences) {
  const values = list(appStates), result = values.filter(item => ["drafts", "preferences", "ui_preferences"].includes(item?.key)).map(copy);
  let current = result.find(item => item.key === "ui_preferences");
  const proactiveUsage = normalizeProactiveUsage([
    ...list(current?.proactiveUsage), ...values.filter(item => string(item?.key).startsWith("proactive_usage_v1:"))]);
  let readingPreferences = current && Object.hasOwn(current, "readingPreferences") ? normalizeReadingPreferences(current.readingPreferences) : null;
  if (localPreferences) try { readingPreferences = normalizeReadingPreferences(JSON.parse(localPreferences.getItem("project-snow-public:reading:v1") || "{}")); } catch {}
  if (proactiveUsage.length || current?.proactiveUsage) {
    if (current) current.proactiveUsage = proactiveUsage;
    else { current = { key: "ui_preferences", proactiveUsage }; result.push(current); }
  }
  if (readingPreferences) {
    if (current) current.readingPreferences = readingPreferences;
    else result.push({ key: "ui_preferences", readingPreferences });
  }
  return result;
}
export function normalizeFeaturePreferences(value = {}) {
  const preferences = object(value), usage = object(preferences.usagePreferences);
  return { ...copy(preferences), favoriteLines: favoriteLines(preferences.favoriteLines),
    deletedMemoryIds: identifiers(preferences.deletedMemoryIds, 500),
    usagePreferences: { ...copy(usage), showUsage: usage.showUsage !== false },
    rendezvousReminders: normalizeRendezvousReminders(preferences.rendezvousReminders),
    proactiveUsage: normalizeProactiveUsage(preferences.proactiveUsage),
    ...(Object.hasOwn(preferences, "readingPreferences") ? { readingPreferences: normalizeReadingPreferences(preferences.readingPreferences) } : {}),
    reminderPreferences: { ...copy(object(preferences.reminderPreferences)), enabled: preferences.reminderPreferences?.enabled !== false } };
}
export function mergeFeaturePreferences(current = {}, incoming = {}, options = {}) {
  const existing = normalizeFeaturePreferences(current), imported = normalizeFeaturePreferences(incoming);
  return normalizeFeaturePreferences({ ...imported, ...existing,
    favoriteLines: [...imported.favoriteLines, ...existing.favoriteLines],
    deletedMemoryIds: [...imported.deletedMemoryIds, ...existing.deletedMemoryIds],
    usagePreferences: { ...object(incoming.usagePreferences), ...object(current.usagePreferences) },
    rendezvousReminders: mergeRendezvousReminders(existing.rendezvousReminders, imported.rendezvousReminders, options),
    proactiveUsage: normalizeProactiveUsage([...imported.proactiveUsage, ...existing.proactiveUsage]),
    ...(Object.hasOwn(incoming, "readingPreferences") ? { readingPreferences: normalizeReadingPreferences(incoming.readingPreferences) } : {}),
    reminderPreferences: { ...object(incoming.reminderPreferences), ...object(current.reminderPreferences) } });
}
export function filterDeletedMemories(receipts, deletedIds = []) {
  const deleted = new Set(identifiers(deletedIds, 500));
  return list(receipts).filter(item => !deleted.has(item?.memory?.memory_id)).map(copy);
}
function fold(text) {
  let value = "", index = 0;
  const starts = [], ends = [];
  for (const character of text) {
    const code = character.codePointAt(0);
    const folded = (code >= 0xff01 && code <= 0xff5e ? String.fromCodePoint(code - 0xfee0) : character).toLowerCase();
    value += folded;
    for (let unit = 0; unit < folded.length; unit += 1) { starts.push(index); ends.push(index + character.length); }
    index += character.length;
  }
  return { value, starts, ends };
}
function textRanges(text, query) {
  const source = fold(text), needle = fold(query).value, ranges = [];
  for (let from = 0, index; needle && (index = source.value.indexOf(needle, from)) >= 0; from = index + needle.length) {
    const range = [source.starts[index], source.ends[index + needle.length - 1]];
    if (!ranges.length || ranges.at(-1)[0] !== range[0] || ranges.at(-1)[1] !== range[1]) ranges.push(range);
  }
  return ranges;
}
function wholeBoundary(text, index) {
  return !(index > 0 && index < text.length && /[\uD800-\uDBFF]/.test(text[index - 1]) && /[\uDC00-\uDFFF]/.test(text[index]));
}
function count(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
function usagePart(value, kind, fallbackModel = "") {
  const usage = object(value);
  const calls = count(usage.provider_calls ?? usage.model_calls ?? usage.call_count);
  const raw = count(usage.total_tokens ?? usage.totalTokens);
  return { kind, calls, tokens: calls === 0 ? 0 : raw !== null && raw > 0 ? raw : null,
    model: string(usage.model) || fallbackModel };
}
function sumParts(parts) {
  const paid = parts.filter(part => part.calls !== 0);
  const knownCalls = paid.filter(part => part.calls !== null), knownTokens = paid.filter(part => part.tokens !== null);
  return { calls: !paid.length ? 0 : !knownCalls.length ? null : knownCalls.reduce((sum, part) => sum + part.calls, 0),
    tokens: !paid.length ? 0 : !knownTokens.length ? null : knownTokens.reduce((sum, part) => sum + part.tokens, 0),
    partial: paid.some(part => part.calls === null) || (knownTokens.length > 0 && knownTokens.length < paid.length) };
}
function messageCharacter(message) { return string(message.characterId || message.character_id); }
function requestKey(message) {
  return `${messageCharacter(message)}:${string(message.requestId || message.request_id) || `message:${message.id}`}`;
}
function roundDay(group) {
  const dated = group.map(message => ({ message, created: stamp(message.createdAt ?? message.created_at) }))
    .filter(item => item.created > 0 && Number.isFinite(new Date(item.created).getTime()));
  const users = dated.filter(item => item.message.role === "user");
  // A turn belongs to its send date. Arrival turns have no user message.
  const started = (users.length ? users : dated).reduce((first, item) => Math.min(first, item.created), Infinity);
  return Number.isFinite(started) ? dayKey(started) : "";
}

export function createFeatureLogic(adapter) {
  for (const name of ["snapshot", "preferences", "savePreferences", "loadDialogue", "saveDialogue", "getMessage", "messages", "allMessages", "on"]) {
    if (typeof adapter?.[name] !== "function") throw new TypeError(`feature_adapter_${name}_required`);
  }
  const undo = new Map(), messages = new Map(), groups = new Map(), activeReads = new Map(), receipts = new Map(), activeUsageReads = new Map();
  let generation = 0, undoVersion = 0;
  let pending = Promise.resolve(), disposed = false;
  const serial = operation => {
    const result = pending.then(operation);
    pending = result.catch(() => {});
    return result;
  };
  const preferences = () => normalizeFeaturePreferences(adapter.preferences());
  const historyToken = () => ({ generation, epoch: adapter.snapshot().historyEpoch });
  const currentHistory = token => !disposed && token.generation === generation && token.epoch === adapter.snapshot().historyEpoch;
  const writable = () => !disposed && adapter.snapshot().writerOwned !== false;
  const busy = characterId => {
    const state = adapter.snapshot();
    return disposed || state.writerOwned === false || Boolean(typeof state.busyFor === "function" ? state.busyFor(characterId) : state.busy);
  };
  const guardMemory = (characterId, operation) => serial(() => typeof adapter.withMemoryWrite === "function"
    ? adapter.withMemoryWrite(characterId, operation) : operation());
  const savePreferences = async (patch, token) => {
    if (!currentHistory(token)) return false;
    await adapter.savePreferences(copy(patch));
    return currentHistory(token);
  };
  const cache = message => {
    if (!message?.id) return;
    // Cost aggregation keeps no dialogue text, snapshots, or proof material.
    const value = {};
    for (const key of ["id", "characterId", "character_id", "role", "status", "requestId", "request_id", "createdAt", "created_at",
      "source", "usage", "extraUsage", "extra_usage", "model"]) {
      if (Object.hasOwn(message, key)) value[key] = copy(message[key]);
    }
    const previous = messages.get(message.id);
    if (previous) {
      const old = groups.get(requestKey(previous));
      old?.delete(message.id);
      if (!old?.size) groups.delete(requestKey(previous));
    }
    messages.delete(message.id);
    messages.set(message.id, value);
    const key = requestKey(value);
    if (!groups.has(key)) groups.set(key, new Map());
    groups.get(key).set(message.id, value);
  };
  const unsubscribeSaved = adapter.on("message-saved", message => {
    if (disposed) return;
    if (message?.id) for (const changed of activeReads.values()) changed.add(message.id);
    cache(message);
  });
  const cacheUsage = value => {
    const receipt = normalizeProactiveUsage([value])[0];
    if (!receipt) return;
    const key = requestKey(receipt), previous = receipts.get(key);
    receipts.set(key, normalizeProactiveUsage([previous, receipt])[0]);
  };
  const unsubscribeUsage = adapter.on("usage-saved", receipt => {
    if (disposed) return;
    const key = requestKey(object(receipt));
    for (const changed of activeUsageReads.values()) changed.add(key);
    cacheUsage(receipt);
  });
  function refresh() {
    if (disposed) return Promise.resolve();
    const current = ++generation, epoch = adapter.snapshot().historyEpoch, changed = new Set(), changedUsage = new Set();
    undoVersion += 1;
    undo.clear();
    messages.clear();
    groups.clear();
    receipts.clear();
    activeReads.set(current, changed);
    activeUsageReads.set(current, changedUsage);
    return Promise.all([Promise.resolve().then(() => adapter.allMessages()), Promise.resolve().then(() => adapter.usageReceipts?.() || [])]).then(([values, usage]) => {
      if (disposed || current !== generation || epoch !== adapter.snapshot().historyEpoch) return;
      const saved = [...messages.values()].filter(message => changed.has(message.id));
      messages.clear();
      groups.clear();
      for (const message of list(values)) if (!changed.has(message.id)) cache(message);
      for (const message of saved) cache(message);
      const savedUsage = [...receipts].filter(([key]) => changedUsage.has(key)).map(([, value]) => value);
      receipts.clear();
      for (const value of normalizeProactiveUsage([...preferences().proactiveUsage, ...list(usage)])) if (!changedUsage.has(requestKey(value))) cacheUsage(value);
      for (const value of savedUsage) cacheUsage(value);
    }).finally(() => { activeReads.delete(current); activeUsageReads.delete(current); });
  }
  const unsubscribeHistory = adapter.on("history-changed", () => { void refresh().catch(() => {}); });
  const reading = createReadingLogic(adapter), reminders = createReminderLogic(adapter);
  const ready = Promise.all([refresh(), reading.ready, reminders.ready]);

  async function memoryList(characterId) {
    const token = historyToken(), record = object(await adapter.loadDialogue(characterId));
    if (!currentHistory(token)) return [];
    const state = adapter.snapshot();
    const catalog = list(state.movementCatalog);
    return filterDeletedMemories(record.memories, preferences().deletedMemoryIds).map(item => {
      const memory = object(item.memory), locationId = string(memory.location_id);
      const location = catalog.find(place => string(place.location_id || place.id) === locationId);
      return { id: string(memory.memory_id), kind: string(memory.kind), text: string(memory.value),
        occurredAt: string(memory.occurred_at), locationId,
        locationName: string(location?.display_name || location?.location_name || location?.name),
        sourceMessageIds: copy(list(memory.source_message_ids)) };
    }).filter(item => item.id).sort((a, b) => stamp(b.occurredAt) - stamp(a.occurredAt) || b.id.localeCompare(a.id));
  }
  async function mutateMemory(characterId, id, restoring) {
    if (!string(characterId) || !string(id)) return { ok: false };
    const version = undoVersion, token = historyToken();
    return guardMemory(characterId, async () => {
      if (!currentHistory(token)) return { ok: false };
      if (busy(characterId)) return { ok: false, reason: "busy" };
      if (restoring && version !== undoVersion) return { ok: false };
      const record = copy(object(await adapter.loadDialogue(characterId)));
      if (!currentHistory(token)) return { ok: false };
      if (busy(characterId)) return { ok: false, reason: "busy" };
      const key = `${characterId}\u0000${id}`, original = restoring ? undo.get(key)
        : list(record.memories).find(item => item?.memory?.memory_id === id);
      if (!original) return { ok: false };
      const previous = preferences().deletedMemoryIds;
      const deletedMemoryIds = restoring ? previous.filter(value => value !== id) : identifiers([...previous, id], 500);
      if (restoring) {
        // Keep the tombstone in place until the signed receipt is safely back on disk.
        record.memories = [...list(record.memories).filter(item => item?.memory?.memory_id !== id), copy(original)];
        if (busy(characterId)) return { ok: false, reason: "busy" };
        await adapter.saveDialogue(characterId, copy(record));
        if (!currentHistory(token)) return { ok: false };
        if (busy(characterId)) return { ok: false, reason: "busy" };
        if (!await savePreferences({ deletedMemoryIds }, token)) return { ok: false };
        undo.delete(key);
      } else {
        // A failed record write remains safely hidden by its already-persisted tombstone.
        if (!await savePreferences({ deletedMemoryIds }, token)) return { ok: false };
        if (busy(characterId)) {
          await savePreferences({ deletedMemoryIds: identifiers([...previous,
            ...preferences().deletedMemoryIds.filter(value => value !== id || previous.includes(id))], 500) }, token);
          return { ok: false, reason: "busy" };
        }
        if (version === undoVersion) undo.set(key, copy(original));
        record.memories = list(record.memories).filter(item => item?.memory?.memory_id !== id);
        await adapter.saveDialogue(characterId, copy(record));
        if (!currentHistory(token)) return { ok: false };
      }
      return { ok: true };
    });
  }
  function groupParts(group) {
    const replies = group.filter(item => item.role === "assistant" && (item.usage !== null && item.usage !== undefined || item.status === "sent"));
    // Replays may have different local ids; an empty replay cannot erase an observed receipt.
    // IndexedDB primary-key ordering puts arrival receipts before scene placeholders.
    const reply = replies.filter(item => item.source === "presence_arrival").at(-1) || replies.at(-1), parts = [];
    if (reply) parts.push(usagePart(replies.reduce((usage, item) => mergeUsageReceipt(usage, item.usage),
      reply.source === "proactive_reminder" ? receipts.get(requestKey(reply))?.usage || {} : {}),
      reply.source === "presence_arrival" ? "arrival" : reply.source === "proactive_reminder" ? "proactive" : "reply",
      string(reply.model) || (reply.source === "proactive_reminder" ? string(receipts.get(requestKey(reply))?.model) : "")));
    const extras = normalizeExtraUsage(group.flatMap(item => list(item.extraUsage || item.extra_usage)));
    for (const extra of extras) parts.push(usagePart(extra, "summary"));
    return parts;
  }
  function roundParts(messageId) {
    const message = messages.get(messageId);
    return message ? groupParts([...groups.get(requestKey(message)).values()]) : [];
  }
  return {
    ready, refresh, reading, reminders,
    // Read-only copy for the UI (favorite anchors need contentBlocks); null when missing.
    messages: { async get(messageId) {
      const token = historyToken();
      if (!string(messageId) || !currentHistory(token)) return null;
      const message = await adapter.getMessage(messageId);
      return currentHistory(token) ? copy(message) || null : null;
    } },
    memories: {
      list: memoryList,
      count: async characterId => (await memoryList(characterId)).length,
      remove: (characterId, id) => mutateMemory(characterId, id, false),
      restore: (characterId, id) => mutateMemory(characterId, id, true),
      clearUndo(characterId) {
        undoVersion += 1;
        for (const key of undo.keys()) if (!characterId || key.startsWith(`${characterId}\u0000`)) undo.delete(key);
      },
    },
    favorites: {
      list({ characterId } = {}) { return copy(preferences().favoriteLines.filter(item => !characterId || item.characterId === characterId)); },
      has(ref) { const id = favoriteId(ref); return Boolean(id && preferences().favoriteLines.some(item => item.id === id)); },
      toggle(ref) {
        const token = historyToken();
        return serial(async () => {
          const id = favoriteId(ref);
          if (!id) return false;
          let favorites = preferences().favoriteLines;
          if (!currentHistory(token) || !writable()) return favorites.some(item => item.id === id);
          if (favorites.some(item => item.id === id)) {
            await savePreferences({ favoriteLines: favorites.filter(item => item.id !== id) }, token);
            return preferences().favoriteLines.some(item => item.id === id);
          }
          const message = copy(await adapter.getMessage(ref.messageId));
          if (!currentHistory(token)) return preferences().favoriteLines.some(item => item.id === id);
          if (!message || message.status !== "sent" || !writable()) return false;
          const block = list(message.contentBlocks || message.content_blocks)[ref.blockIndex], text = string(block?.text);
          if (ref.end > text.length || !wholeBoundary(text, ref.start) || !wholeBoundary(text, ref.end)
              || Array.from(text.slice(ref.start, ref.end)).length > 240) return false;
          favorites = preferences().favoriteLines;
          if (favorites.length >= 200) return false;
          const location = object(message.movementStatus || message.movement_status);
          const favorite = { id, characterId: messageCharacter(message), messageId: message.id, blockIndex: ref.blockIndex,
            start: ref.start, end: ref.end, text: text.slice(ref.start, ref.end), createdAt: Date.now(),
            channel: (message.communicationChannel || message.communication_channel) === "in_person" ? "in_person" : "text",
            locationName: string(message.locationName || location.location_name || location.character_location) };
          if (!favorite.characterId) return false;
          await savePreferences({ favoriteLines: [...favorites, favorite] }, token);
          return preferences().favoriteLines.some(item => item.id === id);
        });
      },
    },
    transcript: {
      async search(characterId, query, { limit = 200 } = {}) {
        const token = historyToken();
        const term = string(query).trim();
        if (!term || !string(characterId)) return [];
        const maximum = Number.isFinite(limit) ? Math.max(0, Math.min(200, Math.floor(limit))) : 200;
        if (!maximum) return [];
        const records = copy(list(await adapter.messages(characterId))).filter(item => messageCharacter(item) === characterId)
          .sort((a, b) => stamp(b.createdAt ?? b.created_at) - stamp(a.createdAt ?? a.created_at) || string(b.id).localeCompare(string(a.id)));
        if (!currentHistory(token)) return [];
        const results = [];
        for (const message of records) {
          for (const [blockIndex, block] of list(message.contentBlocks || message.content_blocks).entries()) {
            const text = string(block?.text), ranges = textRanges(text, term);
            if (!ranges.length) continue;
            results.push({ messageId: message.id, blockIndex, role: message.role,
              channel: (message.communicationChannel || message.communication_channel) === "in_person" ? "in_person" : "text",
              createdAt: message.createdAt ?? message.created_at, text, ranges });
            if (results.length === maximum) return results;
          }
        }
        return results;
      },
    },
    usage: {
      round(messageId) { const parts = roundParts(messageId); return { ...sumParts(parts), parts: copy(parts) }; },
      today() {
        const today = string(adapter.snapshot().localDayKey) || dayKey(Date.now()), extras = [], parts = [], observed = new Set();
        for (const values of groups.values()) {
          const group = [...values.values()];
          const proactive = group.find(message => message.source === "proactive_reminder"), receipt = proactive && receipts.get(requestKey(proactive));
          if ((receipt ? dayKey(receipt.createdAt) : roundDay(group)) !== today) continue;
          const round = groupParts(group);
          if (round.some(part => part.kind === "proactive")) observed.add(requestKey(group[0]));
          parts.push(...round.filter(part => part.kind !== "summary"));
          extras.push(...group.flatMap(item => list(item.extraUsage || item.extra_usage)));
        }
        for (const [key, receipt] of receipts) {
          const created = stamp(receipt.createdAt ?? receipt.created_at);
          if (observed.has(key) || !created || !Number.isFinite(new Date(created).getTime()) || dayKey(created) !== today) continue;
          parts.push(usagePart(receipt.usage, "proactive", string(receipt.model)));
        }
        parts.push(...normalizeExtraUsage(extras).map(extra => usagePart(extra, "summary")));
        return sumParts(parts);
      },
      preferences: () => copy(preferences().usagePreferences),
      setPreferences(patch) {
        const token = historyToken();
        return serial(async () => {
          if (currentHistory(token) && writable()) await savePreferences({ usagePreferences: { ...preferences().usagePreferences,
            ...(typeof patch?.showUsage === "boolean" ? { showUsage: patch.showUsage } : {}) } }, token);
          return copy(preferences().usagePreferences);
        });
      },
    },
    dispose() {
      disposed = true;
      reading.dispose(); reminders.dispose();
      undoVersion += 1;
      undo.clear(); messages.clear(); groups.clear(); receipts.clear(); activeReads.clear(); activeUsageReads.clear();
      for (const unsubscribe of [unsubscribeSaved, unsubscribeHistory, unsubscribeUsage]) if (typeof unsubscribe === "function") unsubscribe();
    },
  };
}
