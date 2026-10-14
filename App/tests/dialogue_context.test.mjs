import test from "node:test";
import assert from "node:assert/strict";
import { relevantDialogueReceipts, fitDialogueContext } from "../public_frontend/dialogue-context.js";

const receipt = (id, kind, value, location = "") => ({
  memory: { memory_id: id, kind, value, location_id: location }, proof: `synthetic-proof-${id}`,
});

test("location recall retains a confirmed visit after a long unrelated conversation", () => {
  const visit = receipt("verified-visit", "shared_scene_event", "在公园会合", "park");
  const reports = Array.from({ length: 80 }, (_, i) => receipt(`statement-${i}`, "user_statement", "我们今天喝了茶"));
  const selected = relevantDialogueReceipts([visit, ...reports], "我们上次去哪里？");
  assert.equal(selected.length, 12);
  assert.equal(selected[0].memory.memory_id, "verified-visit");
  assert.equal(reports.length, 80);
  assert.notEqual(selected[0], visit);
});

test("an unfulfilled promise cannot outrank a confirmed shared event in shared recall", () => {
  const completed = receipt("completed", "shared_scene_event", "当轮互动：递茶；接过茶杯");
  const others = Array.from({ length: 30 }, (_, i) => receipt(`promise-${i}`, "assistant_commitment", "约定在公园会合", "park"));
  assert.equal(relevantDialogueReceipts([completed, ...others], "还记得我们上次一起做了什么吗？")[0].memory.memory_id, "completed");
});

test("a specific user preference remains retrievable and neither selection nor mutation trims the local index", () => {
  const preference = receipt("preference", "user_statement", "我喜欢热咖啡，不加糖");
  const records = [preference, ...Array.from({ length: 20 }, (_, i) => receipt(`other-${i}`, "user_statement", "喜欢散步"))];
  const selected = relevantDialogueReceipts(records, "我喜欢什么咖啡？");
  assert.equal(selected[0].memory.memory_id, "preference");
  selected[0].memory.value = "changed fixture";
  assert.equal(preference.memory.value, "我喜欢热咖啡，不加糖");
  assert.equal(records.length, 21);
});

const segment = "893a634f-b97f-4eb6-b54d-c333e896c798";
const transferBytes = value => Buffer.byteLength(JSON.stringify(value), "utf8");
const completeReceipt = (i, value = "合成经历") => ({
  memory: { memory_id: `memory-${i}`, kind: "user_statement", value, source_request_id: segment, segment_id: segment,
    occurred_at: "2026-10-15T10:00:00Z", location_id: "", scene_epoch: 0, source_message_ids: [`user:${segment}`] },
  proof: "s".repeat(1450),
});

test("the complete UTF-8 context fits 8 KiB by dropping the least relevant receipts without trimming local history", () => {
  const memories = Array.from({ length: 20 }, (_, i) => completeReceipt(i));
  const original = structuredClone(memories);
  const input = { segment_id: segment, session_context_package: "opaque.unchanged", memory_candidates: memories };
  const fitted = fitDialogueContext(input);
  assert.ok(transferBytes(fitted) <= 8192);
  assert.ok(fitted.memory_candidates.length > 0 && fitted.memory_candidates.length < 12);
  assert.deepEqual(fitted.memory_candidates, memories.slice(0, fitted.memory_candidates.length));
  assert.ok(transferBytes({ ...fitted, memory_candidates: memories.slice(0, fitted.memory_candidates.length + 1) }) > 8192);
  assert.equal(fitted.session_context_package, input.session_context_package);
  fitted.memory_candidates[0].memory.value = "changed copy";
  assert.deepEqual(memories, original);
});

test("Chinese memory text uses UTF-8 bytes rather than JavaScript string length", () => {
  const make = value => ({ segment_id: segment, session_context_package: "opaque", memory_candidates:
    Array.from({ length: 12 }, (_, i) => completeReceipt(i, value)) });
  const chinese = fitDialogueContext(make("雪".repeat(160)));
  const ascii = fitDialogueContext(make("s".repeat(160)));
  assert.ok(chinese.memory_candidates.length < ascii.memory_candidates.length);
  assert.ok(transferBytes(chinese) > JSON.stringify(chinese).length);
  assert.ok(transferBytes(chinese) <= 8192);
});

test("context selection counts optional memory defaults and caps even tiny receipts at twelve", () => {
  const memories = Array.from({ length: 15 }, (_, i) => ({ memory: { memory_id: `memory-${i}`, kind: "user_statement",
    value: "合成", source_request_id: segment, segment_id: segment }, proof: "proof" }));
  const fitted = fitDialogueContext({ segment_id: segment.toUpperCase(), memory_candidates: memories });
  assert.equal(fitted.segment_id, segment);
  assert.equal(fitted.memory_candidates.length, 12);
  assert.deepEqual(fitted.memory_candidates[0].memory.source_message_ids, []);
  assert.equal(fitted.memory_candidates[0].memory.occurred_at, "");
  assert.equal(memories[0].memory.occurred_at, undefined);
  assert.ok(transferBytes(fitted) <= 8192);
});

test("an opaque token that cannot fit alone is refused without rewriting it or the local index", () => {
  const input = { segment_id: segment, session_context_package: "s".repeat(8192), memory_candidates: [completeReceipt(1)] };
  const original = structuredClone(input);
  assert.throws(() => fitDialogueContext(input), { message: "request_too_large" });
  assert.deepEqual(input, original);
});
