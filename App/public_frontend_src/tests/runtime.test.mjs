import test from "node:test";
import assert from "node:assert/strict";
import { createSafeStorage, compareMessageCursor, beforeMessageCursor, portableCopy, validateHistoryBackup } from "../../public_frontend/modules/runtime.js";

test("blocked storage preserves a usable session and credential deletion tombstone", () => {
  const underlying = new Map([["credential", "old-envelope"]]);
  let blocked = false;
  const storage = createSafeStorage(() => {
    if (blocked) throw new Error("SecurityError");
    return {
      getItem: key => underlying.get(key) ?? null,
      setItem: (key, value) => underlying.set(key, value),
      removeItem: key => underlying.delete(key),
    };
  });
  assert.equal(storage.getItem("credential"), "old-envelope");
  blocked = true;
  storage.setItem("notice", "accepted");
  storage.removeItem("credential");
  assert.equal(storage.getItem("notice"), "accepted");
  blocked = false;
  assert.equal(storage.getItem("credential"), null);
  storage.setItem("credential", "new-envelope");
  assert.equal(storage.getItem("credential"), "new-envelope");
});

test("compound pagination retrieves every equal-timestamp record in stable order", () => {
  const source = Array.from({ length: 10001 }, (_, n) => ({ id: String(n).padStart(5, "0"), createdAt: 1000 }));
  const result = [];
  let cursor = Number.MAX_SAFE_INTEGER;
  for (;;) {
    const page = source.filter(item => beforeMessageCursor(item, cursor)).sort((a, b) => compareMessageCursor(b, a)).slice(0, 60);
    if (!page.length) break;
    result.push(...page);
    cursor = page.at(-1);
  }
  assert.equal(result.length, 10001);
  assert.equal(new Set(result.map(item => item.id)).size, 10001);
});

test("portable backups exclude nested credentials and subject-bound state", () => {
  const copy = portableCopy({ credential: "secret", messages: [{ requestSnapshot: { api_key: "secret", state_package: "signed", request_id: "id" } }], statePackage: "signed" });
  assert.deepEqual(copy, { messages: [{ requestSnapshot: { request_id: "id" } }] });
  const backup = { schema: "project-snow-history-1", databaseVersion: 4, exportedAt: "2026-09-07", threads: [{ characterId: "mia" }], messages: [{ id: "one", characterId: "mia", createdAt: 1, role: "user" }], appState: [{ key: "drafts", values: {} }] };
  assert.equal(validateHistoryBackup(backup).databaseVersion, 4);
  assert.throws(() => validateHistoryBackup({ ...backup, databaseVersion: 5 }), /backup_invalid/);
  assert.throws(() => validateHistoryBackup({ ...backup, messages: [...backup.messages, ...backup.messages] }), /backup_invalid/);
  assert.throws(() => validateHistoryBackup({ ...backup, appState: [{ key: "world" }] }), /backup_invalid/);
});
