/** Browser primitives independent of the conversation and its wire format. */
export interface SafeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function createSafeStorage(resolve: () => Storage): SafeStorage {
  // Tombstones matter: a failed remove must never expose the old credential.
  const memory = new Map<string, string | null>();
  return {
    getItem(key) {
      if (memory.has(key)) return memory.get(key) ?? null;
      try { return resolve().getItem(key); } catch { return null; }
    },
    setItem(key, value) {
      try { resolve().setItem(key, String(value)); memory.delete(key); }
      catch { memory.set(key, String(value)); }
    },
    removeItem(key) {
      try { resolve().removeItem(key); memory.delete(key); }
      catch { memory.set(key, null); }
    },
  };
}

export interface MessageCursor { createdAt: number; id: string }
export function compareMessageCursor(a: MessageCursor, b: MessageCursor): number {
  return Number(a.createdAt) - Number(b.createdAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

export function beforeMessageCursor(message: MessageCursor, before: number | MessageCursor): boolean {
  return typeof before === "number" ? message.createdAt < before : compareMessageCursor(message, before) < 0;
}

export interface BuildInfo {
  app_version?: string;
  revision?: string | null;
  api_schema?: string;
  state_schema?: string;
}
export function buildIdentity(info: BuildInfo): string {
  return [info.app_version || "", info.revision || ""].join(":");
}

/** Strip credentials at every depth, including pending request snapshots. */
export function portableCopy(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(portableCopy);
  if (!value || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (/^(credential|api_?key|authorization|cookie|state_?package|legacyStatePackage)$/i.test(key)) continue;
    if (["__proto__", "constructor", "prototype"].includes(key)) continue;
    out[key] = portableCopy(item);
  }
  return out;
}

export interface HistoryBackup {
  schema: "project-snow-history-1";
  databaseVersion: 4;
  exportedAt: string;
  threads: Record<string, unknown>[];
  messages: Record<string, unknown>[];
  appState: Record<string, unknown>[];
}
export function validateHistoryBackup(value: unknown): HistoryBackup {
  if (!value || typeof value !== "object") throw new Error("backup_invalid");
  const data = value as Partial<HistoryBackup>;
  if (data.schema !== "project-snow-history-1" || data.databaseVersion !== 4
    || !Array.isArray(data.threads) || !Array.isArray(data.messages) || !Array.isArray(data.appState)) throw new Error("backup_invalid");
  if (data.messages.length > 100000 || data.threads.length > 1000 || data.appState.length > 20) throw new Error("backup_too_large");
  const characters = new Set<string>();
  for (const thread of data.threads) {
    if (!thread || typeof thread.characterId !== "string" || !thread.characterId || characters.has(thread.characterId)) throw new Error("backup_invalid");
    characters.add(thread.characterId);
  }
  const ids = new Set<string>();
  for (const message of data.messages) {
    if (!message || typeof message.id !== "string" || !message.id || ids.has(message.id)
      || typeof message.characterId !== "string" || !characters.has(message.characterId)
      || !Number.isFinite(message.createdAt) || Number(message.createdAt) <= 0
      || !["assistant", "user"].includes(String(message.role))) throw new Error("backup_invalid");
    ids.add(message.id);
  }
  if (data.appState.some((item) => !item || !["drafts", "preferences", "ui_preferences"].includes(String(item.key)))) throw new Error("backup_invalid");
  return portableCopy(data) as HistoryBackup;
}
