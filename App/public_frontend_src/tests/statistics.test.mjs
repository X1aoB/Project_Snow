import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import config from "../../public_frontend/statistics/config.mjs";
import { createAnalytics, mountAnalytics } from "../../public_frontend/statistics/analytics.mjs";

test("default-off statistics neither registers request hook nor touches product storage", () => {
  assert.equal(config.enabled, false);
  const environment = new Proxy({}, { get() { throw new Error("unexpected product side effect"); } });
  assert.equal(createAnalytics(config, environment).active, false);
  assert.doesNotThrow(() => mountAnalytics(config, environment));
});

test("statistics allowlist matches the public selector roster and keeps the endpoint separate", () => {
  const registry = JSON.parse(readFileSync(new URL("../../backend/snow_app/mvp_character_registry.json", import.meta.url), "utf8"));
  const expected = registry.characters.filter(row => row.selector_enabled !== false).map(row => row.character_id).sort();
  assert.equal(expected.length, 22);
  assert.deepEqual([...config.characters].sort(), expected);
  assert.equal(new URL(config.endpoint).origin, "https://stats.xiaob.dev");
});
