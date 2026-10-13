import test from "node:test";
import assert from "node:assert/strict";
import { anonymousServerTimings } from "../../public_frontend/modules/runtime.js";

test("timing export strips provider secrets and preserves unmeasured phases", () => {
  const input = {
    queue_ms: 0, retrieval_ms: 130.9, provider_first_content_ms: 2200,
    initial_model_ms: 8500, first_committed_block_ms: 2500,
    credential: "synthetic-secret", raw_prompt: "synthetic-body",
    provider_id: "synthetic-private-provider", custom_latency_ms: 900,
  };
  const result = anonymousServerTimings(input);
  assert.deepEqual(result, { queue_ms: 0, retrieval_ms: 130, initial_model_ms: 8500,
    provider_first_content_ms: 2200, first_committed_block_ms: 2500 });
  assert.ok(!("durable_commit_ms" in result));
  assert.ok(!JSON.stringify(result).includes("synthetic"));
  result.queue_ms = 10;
  assert.equal(input.queue_ms, 0);
});

test("invalid diagnostics cannot become measured zero-duration phases", () => {
  assert.deepEqual(anonymousServerTimings({ queue_ms: -1, retrieval_ms: true,
    provider_headers_ms: "30", total: Infinity, validation_ms: NaN,
    durable_commit_ms: 3_600_001, first_committed_block_ms: null }), {});
  for (const input of [null, undefined, false, 8, "synthetic-secret", []]) {
    assert.deepEqual(anonymousServerTimings(input), {});
  }
  assert.deepEqual(anonymousServerTimings({ total: 3_600_000 }), { total: 3_600_000 });
});
