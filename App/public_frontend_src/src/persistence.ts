export const HISTORY_DATABASE_VERSION = 4;
export type StoredRecord = Record<string, unknown>;

export async function writeDraftsDatabase(db: IDBDatabase, values: Record<string, string>, revision: string): Promise<void> {
  const tx = db.transaction("app_state", "readwrite");
  const done = transactionDone(tx);
  const store = tx.objectStore("app_state");
  store.put({ key: "drafts", values, revision });
  // Legacy v4 readers ignore this key. It survives their boot-time draft bug.
  store.put({ key: "drafts_recovery_v1", values, revision });
  await done;
}

export function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", () => resolve(), { once: true });
    transaction.addEventListener("error", () => reject(transaction.error || new Error("indexeddb_transaction_failed")), { once: true });
    transaction.addEventListener("abort", () => reject(transaction.error || new Error("indexeddb_transaction_aborted")), { once: true });
  });
}

export async function pruneHistoryDatabase(db: IDBDatabase, cutoff: number): Promise<Map<string, number>> {
  const counts = new Map<string, number>();
  const tx = db.transaction(["threads", "messages"], "readwrite");
  const done = transactionDone(tx);
  const cursorRequest = tx.objectStore("messages").openCursor();
  cursorRequest.onsuccess = () => {
    const cursor = cursorRequest.result;
    if (cursor) {
      if (Number(cursor.value.createdAt || 0) < cutoff) cursor.delete();
      else counts.set(cursor.value.characterId, (counts.get(cursor.value.characterId) || 0) + 1);
      cursor.continue();
    } else {
      const threads = tx.objectStore("threads").openCursor();
      threads.onsuccess = () => {
        const item = threads.result;
        if (!item) return;
        item.update({ ...item.value, messageCount: counts.get(item.value.characterId) || 0 });
        item.continue();
      };
    }
  };
  await done;
  return counts;
}

export interface ImportRecords {
  threads: StoredRecord[];
  messages: StoredRecord[];
  appState: StoredRecord[];
}

/** Merge all stores in one transaction; an abort leaves every old record intact. */
export async function mergeHistoryDatabase(
  db: IDBDatabase,
  backup: ImportRecords,
  normalizeMessage: (record: StoredRecord) => StoredRecord,
): Promise<void> {
  const tx = db.transaction(["threads", "messages", "app_state"], "readwrite");
  const done = transactionDone(tx);
  try {
    const threads = tx.objectStore("threads");
    const messages = tx.objectStore("messages");
    const appState = tx.objectStore("app_state");
    for (const raw of backup.threads) {
      const existing = threads.get(String(raw.characterId));
      existing.onsuccess = () => {
        if (!existing.result) threads.put({ ...raw, summaryRequestId: "", summaryCheckpointMessageId: "" });
      };
    }
    for (const raw of backup.messages) {
      const message = normalizeMessage(raw);
      // A backup can move between anonymous subjects. Preserve diagnostics,
      // but never replay an imported request with an altered signed state.
      message.requestSnapshot = null;
      if (message.status === "pending") { message.status = "failed"; message.errorCode = "stream_disconnected"; }
      const existing = messages.get(String(message.id));
      existing.onsuccess = () => { if (!existing.result) messages.put(message); };
    }
    for (const entry of backup.appState) {
      const existing = appState.get(String(entry.key));
      existing.onsuccess = () => {
        if (entry.key === "drafts") appState.put({ key: "drafts", values: { ...(entry.values as object || {}), ...(existing.result?.values || {}) } });
        else if (!existing.result) appState.put(entry);
      };
    }
  } catch (error) {
    tx.abort();
    await done.catch(() => undefined);
    throw error;
  }
  await done;
}

export async function clearHistoryDatabase(db: IDBDatabase, writerLease?: StoredRecord): Promise<void> {
  const tx = db.transaction(["threads", "messages", "app_state"], "readwrite");
  const done = transactionDone(tx);
  for (const name of ["threads", "messages", "app_state"]) tx.objectStore(name).clear();
  // The coordinator's lease is operational state, not conversation history.
  if (writerLease) tx.objectStore("app_state").put(writerLease);
  await done;
}
