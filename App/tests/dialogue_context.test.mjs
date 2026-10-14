import test from "node:test";
import assert from "node:assert/strict";
import { relevantDialogueReceipts } from "../public_frontend/dialogue-context.js";

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
