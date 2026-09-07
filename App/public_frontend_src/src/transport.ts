export type PublicRequestOptions = RequestInit & { timeoutMs?: number };

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
            throw new Error(response.ok ? "invalid_response" : "request_failed");
          }
        }
        if (!response.ok) {
          const detail = payload?.detail as { code?: string } | undefined;
          throw new Error(typeof detail?.code === "string" ? detail.code : "request_failed");
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("invalid_response");
        return payload;
      } catch (error) {
        if (controller.signal.aborted && !callerSignal?.aborted && timeoutMs > 0) throw new Error("request_timeout");
        throw error;
      } finally {
        if (timeout !== null) clearTimeout(timeout);
        callerSignal?.removeEventListener("abort", relayAbort);
      }
    },
  };
}
