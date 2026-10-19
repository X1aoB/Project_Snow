// Local reminders only. The host owns authoritative world acceptance and paid requests.
const clone = value => structuredClone(value);
const array = value => Array.isArray(value) ? value : [];
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = value => typeof value === "string" ? value : "";
const activeStatuses = new Set(["scheduled", "snoozed", "fired"]);
const formatter = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit" });
function dayKey(timestamp) {
  const parts = Object.fromEntries(formatter.formatToParts(new Date(timestamp)).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function timestamp(value) { const parsed = typeof value === "number" ? value : Date.parse(value); return Number.isFinite(new Date(parsed).getTime()) ? parsed : null; }
export function normalizeRendezvousReminders(values) {
  const rows = new Map();
  for (const raw of array(values)) {
    const item = object(raw), at = timestamp(item.remindAt);
    if (!text(item.id) || !text(item.characterId) || !text(item.locationId) || at === null
        || !/^\d{4}-\d{2}-\d{2}$/.test(text(item.dayKey)) || dayKey(at) !== item.dayKey
        || !["scheduled", "fired", "snoozed", "done", "expired"].includes(item.status)) continue;
    rows.set(item.id, { ...clone(item), rendezvousId: text(item.rendezvousId) || null, pendingRequestId: text(item.pendingRequestId),
      locationName: text(item.locationName), remindAt: new Date(at).toISOString(), createdAt: timestamp(item.createdAt) ?? at });
  }
  return [...rows.values()].sort((a, b) => b.createdAt - a.createdAt || a.id.localeCompare(b.id)).slice(0, 50);
}
export function mergeRendezvousReminders(current, incoming, options = {}) {
  const today = text(options.dayKey) || dayKey(options.now ?? Date.now());
  return normalizeRendezvousReminders([...normalizeRendezvousReminders(incoming).filter(item => item.dayKey === today),
    ...normalizeRendezvousReminders(current)]);
}
function worldRows(value) {
  const items = value?.items;
  return Array.isArray(items) ? clone(items) : Object.entries(object(items)).map(([characterId, row]) => ({ character_id: characterId, ...clone(object(row)) }));
}
export function createReminderLogic(adapter) {
  const clock = adapter.clock || { now: () => Date.now(), setTimeout: (fn, delay) => setTimeout(fn, delay), clearTimeout: id => clearTimeout(id) };
  let running = false, disposed = false, timer = null, generation = 0, queue = Promise.resolve(), world = null;
  const snapshot = () => adapter.snapshot();
  const today = () => text(snapshot().localDayKey) || dayKey(clock.now());
  const token = () => ({ generation, epoch: snapshot().historyEpoch });
  const current = captured => !disposed && captured.generation === generation && captured.epoch === snapshot().historyEpoch;
  const writable = captured => current(captured) && snapshot().writerOwned !== false;
  const preferences = () => ({ ...clone(object(adapter.preferences()?.reminderPreferences)), enabled: adapter.preferences()?.reminderPreferences?.enabled !== false });
  const rows = () => normalizeRendezvousReminders(adapter.preferences()?.rendezvousReminders);
  const serial = operation => { const result = queue.then(operation); queue = result.catch(() => {}); return result; };
  function clearTimer() { if (timer !== null) clock.clearTimeout(timer); timer = null; }
  function schedule() {
    clearTimer(); if (!running || disposed || snapshot().writerOwned === false) return;
    const now = clock.now(), nextDay = Date.parse(`${today()}T00:00:00+08:00`) + 86400000;
    const next = preferences().enabled ? rows().filter(item => item.dayKey === today() && item.rendezvousId
      && ["scheduled", "snoozed"].includes(item.status) && matching(item)).reduce((first, item) => Math.min(first, timestamp(item.remindAt)), nextDay) : nextDay;
    timer = clock.setTimeout(() => { timer = null; void check().catch(() => { running = false; clearTimer(); }); }, Math.max(0, Math.min(2147483647, next - now)));
  }
  async function save(values, captured) {
    if (!writable(captured)) return false;
    await adapter.savePreferences({ rendezvousReminders: normalizeRendezvousReminders(values) });
    if (!writable(captured)) return false;
    try { adapter.emit?.("reminders-changed", { reminders: clone(normalizeRendezvousReminders(values)) }); } catch {}
    return true;
  }
  function validTime(value) { const at = timestamp(value); return at !== null && at > clock.now() && dayKey(at) === today() ? at : null; }
  function authoritative() { return world || snapshot().rendezvous; }
  function matching(item, value = authoritative()) {
    if (!value?.authoritative || value.dayKey !== today()) return undefined;
    return worldRows(value).find(row => text(row.character_id || row.characterId) === item.characterId
      && text(row.rendezvous_id || row.rendezvousId) === item.rendezvousId
      && text(row.schedule_date || row.dayKey || value.dayKey) === today()
      && text(row.location_id || row.locationId) === item.locationId) || null;
  }
  function settledRows(values) {
    return values.map(item => {
      if (!activeStatuses.has(item.status)) return item;
      if (item.dayKey !== today()) return { ...item, status: "expired" };
      if (item.rendezvousId && matching(item) === null) return { ...item, status: "done" };
      return clone(item);
    });
  }
  function check() {
    const captured = token();
    return serial(async () => {
      if (!writable(captured)) { schedule(); return []; }
      const original = rows(), updated = settledRows(original), due = [];
      if (preferences().enabled) for (const item of updated) {
        if (item.dayKey === today() && item.rendezvousId && matching(item) && ["scheduled", "snoozed"].includes(item.status)
            && timestamp(item.remindAt) <= clock.now()) { item.status = "fired"; due.push(clone(item)); }
      }
      if (JSON.stringify(original) !== JSON.stringify(updated) && !await save(updated, captured)) return [];
      if (!writable(captured)) return [];
      for (const reminder of due) {
        const character = array(snapshot().characters).find(item => text(item.character_id || item.id) === reminder.characterId) || null;
        try { adapter.emit?.("reminder-due", { reminder: clone(reminder), character: clone(character) }); } catch {}
      }
      schedule(); return clone(due);
    });
  }
  function mutate(operation) {
    const captured = token();
    return serial(async () => {
      if (!writable(captured)) return { ok: false, reason: "readonly" };
      const values = settledRows(rows()), result = operation(values);
      if (!result?.ok) return result;
      if (!await save(values, captured)) return { ok: false };
      schedule(); return clone(result);
    });
  }
  function insert(values, item) {
    const old = values.findIndex(row => row.id === item.id);
    if (old >= 0) { values[old] = item; return true; }
    if (values.length >= 50) {
      const ended = values.findLastIndex(row => !activeStatuses.has(row.status));
      if (ended < 0) return false;
      values.splice(ended, 1);
    }
    values.push(item); return true;
  }
  function plan({ characterId, locationId, locationName = "", remindAt, requestId } = {}) {
    return mutate(values => {
      const at = validTime(remindAt);
      if (!text(characterId) || !text(locationId) || !text(requestId) || at === null) return { ok: false, reason: "invalid" };
      const id = `plan:${characterId}:${requestId}`, previous = values.find(item => item.id === id);
      const item = { id, characterId, rendezvousId: null, pendingRequestId: requestId, locationId, locationName: text(locationName),
        remindAt: new Date(at).toISOString(), dayKey: today(), status: "scheduled", createdAt: previous?.createdAt ?? clock.now() };
      if (!insert(values, item)) return { ok: false, reason: "limit" };
      return { ok: true, id };
    });
  }
  function set({ characterId, rendezvousId, remindAt } = {}) {
    return mutate(values => {
      const at = validTime(remindAt), value = authoritative();
      const rendezvous = value?.authoritative && value.dayKey === today() ? worldRows(value).find(row =>
        text(row.character_id || row.characterId) === characterId && text(row.rendezvous_id || row.rendezvousId) === rendezvousId
        && text(row.schedule_date || row.dayKey || value.dayKey) === today()) : null;
      if (!rendezvous || at === null) return { ok: false, reason: "invalid" };
      const previous = values.find(row => row.characterId === characterId && row.rendezvousId === rendezvousId);
      const id = previous?.id || `rendezvous:${characterId}:${rendezvousId}`;
      const item = { id, characterId, rendezvousId, pendingRequestId: previous?.pendingRequestId || "",
        locationId: text(rendezvous.location_id || rendezvous.locationId), locationName: text(rendezvous.location_name || rendezvous.locationName),
        remindAt: new Date(at).toISOString(), dayKey: today(), status: "scheduled", createdAt: previous?.createdAt ?? clock.now() };
      if (!item.locationId || !insert(values, item)) return { ok: false, reason: item.locationId ? "limit" : "invalid" };
      return { ok: true, id };
    });
  }
  function settle({ requestId, status, rendezvous } = {}) {
    return mutate(values => {
      let changed = false;
      for (const item of values.filter(row => row.pendingRequestId === requestId && !row.rendezvousId && activeStatuses.has(row.status))) {
        if (status === "character_waiting") {
          if (!rendezvous) continue;
          const id = text(rendezvous.rendezvous_id || rendezvous.rendezvousId), date = text(rendezvous.schedule_date || rendezvous.dayKey);
          if (id && date === today() && text(rendezvous.location_id || rendezvous.locationId) === item.locationId
              && text(rendezvous.character_id || rendezvous.characterId || item.characterId) === item.characterId) item.rendezvousId = id;
          else item.status = "done";
        } else if (["pending", "unknown", "request_result_unknown", "subscription_result_unknown"].includes(status)) continue;
        else item.status = "done";
        changed = true;
      }
      return { ok: true, changed };
    }).then(async result => { if (result.ok) await check(); return result; });
  }
  function clear(id) { return mutate(values => { const item = values.find(row => row.id === id); if (!item) return { ok: false }; item.status = "done"; return { ok: true }; }); }
  function snooze(id, minutes = 10) {
    return mutate(values => {
      const item = values.find(row => row.id === id), at = clock.now() + Number(minutes) * 60000;
      if (!item || item.dayKey !== today() || !item.rendezvousId || !activeStatuses.has(item.status)
          || !Number.isFinite(minutes) || minutes <= 0 || validTime(at) === null) return { ok: false, reason: "invalid" };
      item.remindAt = new Date(at).toISOString(); item.status = "snoozed"; return { ok: true };
    });
  }
  const unsubscribeHistory = adapter.on?.("history-changed", () => {
    generation += 1; world = null; clearTimer(); if (running) void check().catch(() => { running = false; });
  });
  return { ready: Promise.resolve(), plan, set, settle, clear, snooze, check,
    active: () => clone(settledRows(rows()).filter(item => item.dayKey === today() && activeStatuses.has(item.status))),
    preferences,
    setPreferences(patch) {
      const captured = token();
      return serial(async () => {
        if (writable(captured) && typeof patch?.enabled === "boolean") {
          await adapter.savePreferences({ reminderPreferences: { ...preferences(), enabled: patch.enabled } });
          if (writable(captured)) try { adapter.emit?.("reminders-changed", { reminders: clone(rows()) }); } catch {}
        }
        if (current(captured)) schedule(); return preferences();
      });
    },
    start() { running = true; return check(); },
    stop() { running = false; clearTimer(); },
    environment() { return running ? check() : Promise.resolve([]); },
    reconcile(value = snapshot().rendezvous) { world = clone(value || null); return check(); },
    dispose() { disposed = true; running = false; generation += 1; clearTimer(); unsubscribeHistory?.(); },
  };
}
