import test from "node:test";
import assert from "node:assert/strict";
import { loadStageRelease, validateStageRelease, verifiedStageImage, historyWindow, createHttpClient, createSafeStorage, compareMessageCursor, beforeMessageCursor, portableCopy, validateHistoryBackup } from "../../public_frontend/modules/runtime.js";

test("history view stays bounded and stable when a newer reply arrives", () => {
  const records = Array.from({length:10000}, (_, i) => ({id:String(i)}));
  assert.equal(historyWindow(records,"",10000).messages.length,200);
  const before = historyWindow(records,"1000",200);
  const after = historyWindow([...records,{id:"new-reply"}],"1000",200);
  assert.deepEqual(after.messages,before.messages);
  assert.equal(after.hasNewer,true);
  assert.equal(historyWindow(records,"deleted-anchor",60).messages.at(-1).id,"9999");
});

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

test("HTTP deadlines and user cancellation remain distinguishable", async () => {
  const client = createHttpClient("/public/v1", (_url, {signal}) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {once:true});
  }));
  await assert.rejects(client.request("/chat/summarize", {timeoutMs:5}), /request_timeout/);
  const controller = new AbortController();
  const request = client.request("/chat/summarize", {signal:controller.signal,timeoutMs:1000});
  controller.abort();
  await assert.rejects(request, error => error.name === "AbortError");
  const malformed = createHttpClient("/public/v1", async () => new Response("upstream HTML", {status:200}));
  await assert.rejects(malformed.request("/config"), /invalid_response/);
});

test("stage release stays off until all 22 approvals and hashes are supplied", async () => {
  const fetchBefore = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = async () => { fetches += 1; return new Response("tampered"); };
  try {
    assert.equal(await loadStageRelease({enabled:false},[]),null);
    assert.equal(fetches,0);
    const asset = {url:"/assets/stage/neutral.png",sha256:"0".repeat(64),media_type:"image/png",source:{kind:"fixture",reference:"synthetic"},approval:{status:"approved",approved_by:"fixture",approved_at:"2026-09-07T00:00:00Z",evidence:"synthetic; not release approval"}};
    const characters = Array.from({length:22},(_,i)=>({character_id:i.toString(16).padStart(12,"0"),states:{neutral:asset},motions:["none"]}));
    const release = {schema:"project-snow-stage-1",version:"fixture",characters};
    assert.equal(validateStageRelease(release).characters.length,22);
    assert.throws(()=>validateStageRelease({...release,characters:characters.slice(1)}),/invalid/);
    assert.throws(()=>validateStageRelease({...release,characters:characters.map((character,index)=>index===0 ? {...character,states:{neutral:{...asset,approval:{...asset.approval,status:"pending"}}}} : character)}),/invalid/);
    await assert.rejects(verifiedStageImage(asset),/hash_mismatch/);
  } finally { globalThis.fetch = fetchBefore; }
});
