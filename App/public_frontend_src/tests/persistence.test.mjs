import test from "node:test";
import assert from "node:assert/strict";
import { mergeHistoryDatabase, HISTORY_DATABASE_VERSION } from "../../public_frontend/modules/runtime.js";

// This fixture models queued IDB requests and transaction commit/rollback;
// browser integration tests exercise the same import with real IndexedDB.
function databaseFixture(seed = {}) {
  const keys = { threads: "characterId", messages: "id", app_state: "key" };
  const stores = Object.fromEntries(Object.entries(keys).map(([name, key]) =>
    [name, new Map((seed[name] || []).map(value => [value[key], structuredClone(value)]))]));
  const db = {
    transaction(names) {
      const selected = Array.isArray(names) ? names : [names];
      const copies = Object.fromEntries(selected.map(name => [name, structuredClone(stores[name])]));
      const tx = new EventTarget();
      let pending = 0, active = true;
      tx.abort = () => {
        if (!active) throw new DOMException("inactive", "InvalidStateError");
        active = false;
        setImmediate(() => tx.dispatchEvent(new Event("abort")));
      };
      const request = action => {
        const result = { result: undefined, onsuccess: null };
        pending += 1;
        setImmediate(() => {
          if (!active) return;
          try { result.result = action(); result.onsuccess?.(); }
          catch (error) { tx.error = error; tx.abort(); }
          pending -= 1;
          if (!pending && active) setImmediate(() => {
            if (!active || pending) return;
            for (const name of selected) stores[name] = copies[name];
            active = false;
            tx.dispatchEvent(new Event("complete"));
          });
        });
        return result;
      };
      tx.objectStore = name => ({
        get: key => request(() => structuredClone(copies[name].get(key))),
        put: value => {
          const copy = structuredClone(value);
          return request(() => copies[name].set(copy[keys[name]], copy));
        },
      });
      return tx;
    },
  };
  return { db, records: name => [...stores[name].values()].map(value => structuredClone(value)) };
}

const record = (id, extra = {}) => ({ id, characterId: "fixture-character", status: "sent", contentBlocks: [{ type: "message", text: "fixture text" }], ...extra });
const emptyBackup = () => ({ threads: [], messages: [], appState: [] });
const normalize = value => structuredClone(value);

test("three-argument history import retains existing records and safe v4 behavior", async () => {
  const existing = record("same", { source: "local", extraUsage: [{ requestId: "local-summary" }] });
  const oldPreferences = { key: "ui_preferences", historyRetentionDays: 30, deletedMemoryIds: ["local"] };
  const fixture = databaseFixture({
    threads: [{ characterId: "existing", summary: "local summary" }],
    messages: [existing], app_state: [oldPreferences, { key: "drafts", values: { local: "kept", shared: "local" } }],
  });
  await mergeHistoryDatabase(fixture.db, {
    threads: [{ characterId: "existing", summary: "backup summary" }, { characterId: "new", summaryRequestId: "old", summaryCheckpointMessageId: "old" }],
    messages: [record("same", { source: "imported" }), record("pending", { status: "pending", requestSnapshot: { requestId: "foreign" } })],
    appState: [{ key: "ui_preferences", historyRetentionDays: 90 }, { key: "drafts", values: { imported: "new", shared: "backup" } }],
  }, normalize);
  assert.equal(HISTORY_DATABASE_VERSION, 4);
  assert.deepEqual(fixture.records("messages").find(item => item.id === "same"), existing);
  assert.deepEqual(fixture.records("app_state").find(item => item.key === "ui_preferences"), oldPreferences);
  assert.deepEqual(fixture.records("app_state").find(item => item.key === "drafts"), { key: "drafts", values: { imported: "new", shared: "local", local: "kept" } });
  assert.deepEqual(fixture.records("threads").find(item => item.characterId === "existing"), { characterId: "existing", summary: "local summary" });
  assert.deepEqual(fixture.records("threads").find(item => item.characterId === "new"), { characterId: "new", summaryRequestId: "", summaryCheckpointMessageId: "" });
  assert.equal(fixture.records("messages").find(item => item.id === "pending").requestSnapshot, null);
  assert.equal(fixture.records("messages").find(item => item.id === "pending").status, "failed");
  assert.equal(fixture.records("messages").find(item => item.id === "pending").errorCode, "stream_disconnected");
});

test("optional merge callbacks preserve local message content and merge only requested fields", async () => {
  const original = record("same", { source: "local-source", usage: { provider_calls: 1 }, extraUsage: [{ requestId: "summary-1" }] });
  const preferences = { key: "ui_preferences", historyRetentionDays: 30, favoriteLines: [{ id: "a" }], deletedMemoryIds: ["x"] };
  const fixture = databaseFixture({ messages: [original], app_state: [preferences] });
  const backup = { ...emptyBackup(), messages: [record("same", { source: "imported-source", extraUsage: [{ requestId: "summary-1" }, { requestId: "summary-2" }] })],
    appState: [{ key: "ui_preferences", historyRetentionDays: 90, favoriteLines: [{ id: "b" }], deletedMemoryIds: ["y"] }] };
  const before = structuredClone(backup);
  const options = {
    mergeMessage(current, incoming) {
      return { ...current, extraUsage: [...new Map([...current.extraUsage, ...incoming.extraUsage].map(item => [item.requestId, item])).values()] };
    },
    mergeUiPreferences(current, incoming) {
      return { ...current, favoriteLines: [...current.favoriteLines, ...incoming.favoriteLines], deletedMemoryIds: [...new Set([...current.deletedMemoryIds, ...incoming.deletedMemoryIds])] };
    },
  };
  await mergeHistoryDatabase(fixture.db, backup, normalize, options);
  const merged = fixture.records("messages")[0];
  assert.deepEqual({ ...merged, extraUsage: original.extraUsage }, original);
  assert.deepEqual(merged.extraUsage, [{ requestId: "summary-1" }, { requestId: "summary-2" }]);
  assert.deepEqual(fixture.records("app_state")[0], { ...preferences, favoriteLines: [{ id: "a" }, { id: "b" }], deletedMemoryIds: ["x", "y"] });
  assert.deepEqual(backup, before);
});

test("merge callback receives detached copies and cannot change record identity", async () => {
  const backup = { ...emptyBackup(), messages: [record("same", { extraUsage: [] })], appState: [{ key: "ui_preferences", deletedMemoryIds: [] }] };
  const before = structuredClone(backup);
  const fixture = databaseFixture({ messages: [record("same")], app_state: [{ key: "ui_preferences" }] });
  await mergeHistoryDatabase(fixture.db, backup, normalize, {
    mergeMessage(current, incoming) { incoming.contentBlocks[0].text = "mutated copy"; return { ...current, id: "different" }; },
    mergeUiPreferences(current, incoming) { incoming.deletedMemoryIds.push("copy-only"); return { ...current, key: "different" }; },
  });
  assert.deepEqual(backup, before);
  assert.equal(fixture.records("messages")[0].id, "same");
  assert.equal(fixture.records("app_state")[0].key, "ui_preferences");
});

test("first import preserves supplied preference values without invoking conflict callbacks", async () => {
  const fixture = databaseFixture();
  const preferences = { key: "ui_preferences", usagePreferences: { showUsage: false }, deletedMemoryIds: ["x"] };
  let called = 0;
  await mergeHistoryDatabase(fixture.db, { ...emptyBackup(), messages: [record("new")], appState: [preferences] }, normalize, {
    mergeMessage() { called += 1; throw new Error("unexpected"); },
    mergeUiPreferences() { called += 1; throw new Error("unexpected"); },
  });
  assert.equal(called, 0);
  assert.deepEqual(fixture.records("app_state"), [preferences]);
});

test("a merge callback failure aborts every store and rejects with the original error", async () => {
  for (const callback of ["mergeMessage", "mergeUiPreferences"]) {
    const seed = { threads: [{ characterId: "local" }], messages: [record("same")], app_state: [{ key: "ui_preferences" }] };
    const fixture = databaseFixture(seed), failure = new Error("fixture_merge_failure");
    const backup = { threads: [{ characterId: "import-only" }], messages: [record("new"), record("same")], appState: [{ key: "drafts", values: { imported: "new" } }, { key: "ui_preferences" }] };
    await assert.rejects(mergeHistoryDatabase(fixture.db, backup, normalize, { [callback]: () => { throw failure; } }), error => error === failure);
    for (const [name, values] of Object.entries(seed)) assert.deepEqual(fixture.records(name), values);
  }
});

test("asynchronous or invalid merge results cannot silently overwrite local history", async () => {
  for (const result of [null, [], Promise.resolve({})]) {
    const fixture = databaseFixture({ messages: [record("same")] });
    await assert.rejects(mergeHistoryDatabase(fixture.db, { ...emptyBackup(), messages: [record("same")] }, normalize, { mergeMessage: () => result }), /backup_merge_invalid/);
    assert.deepEqual(fixture.records("messages"), [record("same")]);
  }
});
