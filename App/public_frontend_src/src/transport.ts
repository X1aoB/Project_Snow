export type PublicRequestOptions = RequestInit & { timeoutMs?: number };

type TransportError = Error & {
  requestId?: string;
  errorStage?: string;
  retryable?: boolean | null;
  retryAfterSeconds?: number;
  httpStatus?: number;
};

/** One deadline covers both the response headers and JSON body. */
export function createHttpClient(root: string, fetcher: typeof fetch = fetch) {
  return {
    async request(path: string, options: PublicRequestOptions = {}): Promise<Record<string, unknown>> {
      const { timeoutMs = 20000, signal: callerSignal, ...fetchOptions } = options;
      const headers = new Headers(fetchOptions.headers);
      if (fetchOptions.method && fetchOptions.method !== "GET" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const controller = new AbortController();
      const relayAbort = () => controller.abort();
      if (callerSignal?.aborted) controller.abort();
      else callerSignal?.addEventListener("abort", relayAbort, { once: true });
      const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        const response = await fetcher(`${root}${path}`, {
          credentials: "same-origin", ...fetchOptions, headers, signal: controller.signal,
        });
        let payload: Record<string, unknown> = {};
        if (response.status !== 204) {
          try { payload = await response.json(); }
          catch (error) {
            if (controller.signal.aborted) throw error;
            const failure = new Error(response.ok ? "invalid_response" : "request_failed") as TransportError;
            failure.errorStage = "transport";
            failure.httpStatus = response.status;
            throw failure;
          }
        }
        if (!response.ok) {
          const detail = payload?.detail as {
            code?: string;
            request_id?: string;
            stage?: string;
            retryable?: boolean;
            retry_after_seconds?: number;
          } | undefined;
          const failure = new Error(typeof detail?.code === "string" ? detail.code : "request_failed") as TransportError;
          failure.requestId = typeof detail?.request_id === "string" ? detail.request_id : "";
          failure.errorStage = typeof detail?.stage === "string" ? detail.stage : "";
          failure.retryable = typeof detail?.retryable === "boolean" ? detail.retryable : null;
          const retryAfter = Number(detail?.retry_after_seconds);
          failure.retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter > 0
            ? Math.min(300, Math.floor(retryAfter)) : 0;
          failure.httpStatus = response.status;
          throw failure;
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("invalid_response");
        return payload;
      } catch (error) {
        if (controller.signal.aborted && !callerSignal?.aborted && timeoutMs > 0) {
          const failure = new Error("request_timeout") as TransportError;
          failure.errorStage = "transport";
          throw failure;
        }
        throw error;
      } finally {
        if (timeout !== null) clearTimeout(timeout);
        callerSignal?.removeEventListener("abort", relayAbort);
      }
    },
  };
}
