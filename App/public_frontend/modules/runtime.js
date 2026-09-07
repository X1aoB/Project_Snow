// src/runtime.ts
function createSafeStorage(resolve) {
  const memory = /* @__PURE__ */ new Map();
  return {
    getItem(key) {
      if (memory.has(key)) return memory.get(key) ?? null;
      try {
        return resolve().getItem(key);
      } catch {
        return null;
      }
    },
    setItem(key, value) {
      try {
        resolve().setItem(key, String(value));
        memory.delete(key);
      } catch {
        memory.set(key, String(value));
      }
    },
    removeItem(key) {
      try {
        resolve().removeItem(key);
        memory.delete(key);
      } catch {
        memory.set(key, null);
      }
    }
  };
}
function compareMessageCursor(a, b) {
  return Number(a.createdAt) - Number(b.createdAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}
function beforeMessageCursor(message, before) {
  return typeof before === "number" ? message.createdAt < before : compareMessageCursor(message, before) < 0;
}
function buildIdentity(info) {
  return [info.app_version || "", info.revision || ""].join(":");
}
function portableCopy(value) {
  if (Array.isArray(value)) return value.map(portableCopy);
  if (!value || typeof value !== "object") return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (/^(credential|api_?key|authorization|cookie|state_?package|legacyStatePackage)$/i.test(key)) continue;
    if (["__proto__", "constructor", "prototype"].includes(key)) continue;
    out[key] = portableCopy(item);
  }
  return out;
}
function validateHistoryBackup(value) {
  if (!value || typeof value !== "object") throw new Error("backup_invalid");
  const data = value;
  if (data.schema !== "project-snow-history-1" || data.databaseVersion !== 4 || !Array.isArray(data.threads) || !Array.isArray(data.messages) || !Array.isArray(data.appState)) throw new Error("backup_invalid");
  if (data.messages.length > 1e5 || data.threads.length > 1e3 || data.appState.length > 20) throw new Error("backup_too_large");
  const characters = /* @__PURE__ */ new Set();
  for (const thread of data.threads) {
    if (!thread || typeof thread.characterId !== "string" || !thread.characterId || characters.has(thread.characterId)) throw new Error("backup_invalid");
    characters.add(thread.characterId);
  }
  const ids = /* @__PURE__ */ new Set();
  for (const message of data.messages) {
    if (!message || typeof message.id !== "string" || !message.id || ids.has(message.id) || typeof message.characterId !== "string" || !characters.has(message.characterId) || !Number.isFinite(message.createdAt) || Number(message.createdAt) <= 0 || !["assistant", "user"].includes(String(message.role))) throw new Error("backup_invalid");
    ids.add(message.id);
  }
  if (data.appState.some((item) => !item || !["drafts", "preferences", "ui_preferences"].includes(String(item.key)))) throw new Error("backup_invalid");
  return portableCopy(data);
}
export {
  beforeMessageCursor,
  buildIdentity,
  compareMessageCursor,
  createSafeStorage,
  portableCopy,
  validateHistoryBackup
};
