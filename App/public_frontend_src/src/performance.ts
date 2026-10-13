/** Anonymous durations only: never export arbitrary provider diagnostics. */
export const SERVER_TIMING_KEYS = Object.freeze([
  "queue_ms", "context_build_ms", "retrieval_ms", "prompt_build_ms", "hybrid_total", "initial_model_ms",
  "provider_headers_ms", "provider_first_content_ms", "provider_first_event_ms", "guard_rewrite_ms",
  "empty_recovery_ms", "validation_ms", "validation_local_ms", "validation_total_ms",
  "first_verified_block_ms", "durable_commit_ms", "first_committed_block_ms", "total",
] as const);

type ServerTimingKey = typeof SERVER_TIMING_KEYS[number];

export function anonymousServerTimings(value: unknown): Partial<Record<ServerTimingKey, number>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const source = value as Record<string, unknown>;
  const result: Partial<Record<ServerTimingKey, number>> = {};
  for (const key of SERVER_TIMING_KEYS) {
    const item = source[key];
    if (typeof item === "number" && Number.isFinite(item) && item >= 0 && item <= 3_600_000) {
      result[key] = Math.trunc(item);
    }
  }
  return result;
}
