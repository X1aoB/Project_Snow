import test from "node:test";
import assert from "node:assert/strict";
import config from "../../public_frontend/statistics/config.mjs";
import { createAnalytics, mountAnalytics } from "../../public_frontend/statistics/analytics.mjs";

test("default-off statistics neither registers request hook nor touches product storage", () => {
  assert.equal(config.enabled, false);
  const environment = new Proxy({}, { get() { throw new Error("unexpected product side effect"); } });
  assert.equal(createAnalytics(config, environment).active, false);
  assert.doesNotThrow(() => mountAnalytics(config, environment));
});
