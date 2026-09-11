/** Optional v1 adapter. Zero storage or network access until enabled + consented.
 * Vendored into each product; never import from a sibling checkout at runtime.
 */
export function createAnalytics(config, env = globalThis) {
  const inert = Object.freeze({ active: false, track() {}, stop() {}, jump(url) { return url; } });
  if (!config?.enabled || !config?.consent || !["mywebsite", "project_snow"].includes(config.app)) return inert;
  if (typeof config.endpoint !== "string" || !config.endpoint.trim()) return inert;
  try {
    if (env.navigator?.globalPrivacyControl || env.navigator?.doNotTrack === "1") return inert;
    const endpoint = new URL(config.endpoint, env.location.href);
    if (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["localhost", "127.0.0.1"].includes(endpoint.hostname))) return inert;
    const ns = `snow.statistics.v1.${config.app}.`;
    const uuid = () => env.crypto.randomUUID();
    let anonymous = null, session = null, stopped = false, queue = [], inFlight = false;
    let timer = null, currentAbort = null;
    const now = () => Date.now();
    const paths = new Set(config.paths || ["/"]);
    const characters = new Set(config.characters || []);
    const read = key => { try { return JSON.parse(env.localStorage.getItem(ns + key)); } catch { return null; } };
    const save = (key, value) => { try { env.localStorage.setItem(ns + key, JSON.stringify(value)); } catch {} };
    // Scope and expire pseudonymous IDs; do not touch business storage.
    const identify = () => {
      const old = read("anonymous");
      anonymous ||= old && old.expires > now() ? old : { id: uuid(), expires: now() + 30 * 86400000 };
      if (anonymous.expires <= now()) anonymous = { id: uuid(), expires: now() + 30 * 86400000 };
      save("anonymous", anonymous);
      const previous = read("session");
      session = previous && now() - previous.last < 1800000 ? previous : { id: uuid(), last: now() };
      session.last = now(); save("session", session);
      return { anonymous_id: anonymous.id, session_id: session.id };
    };
    const schedule = () => { if (!timer && !stopped) timer = env.setTimeout(() => { timer = null; void flush(); }, 1000); };
    async function flush() {
      if (stopped || inFlight || !queue.length) return;
      inFlight = true;
      const batch = queue.splice(0, 10);
      const controller = new AbortController(); currentAbort = controller;
      const timeout = env.setTimeout(() => controller.abort(), 1500);
      try {
        const response = await env.fetch(endpoint.href, { method: "POST", credentials: "omit", mode: "cors",
          referrerPolicy: "no-referrer", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ events: batch.map(x => x.event) }), signal: controller.signal, keepalive: true });
        if (!response.ok && (response.status >= 500 || response.status === 429)) throw new Error("retry");
      } catch {
        if (!stopped) queue = [...batch.filter(x => x.attempt++ < 1), ...queue].slice(0, 20);
      } finally {
        env.clearTimeout(timeout); currentAbort = null; inFlight = false;
        if (queue.length) schedule();
      }
    }
    function track(kind, fields = {}) {
      if (stopped) return;
      try {
        const safe = {};
        if (kind === "page_view") {
          const path = new URL(fields.path || env.location.href, env.location.href).pathname;
          if (!paths.has(path)) return;
          safe.path = path;
        } else if (kind === "character_select" && config.app === "project_snow") {
          if (!characters.has(fields.character_id)) return;
          safe.character_id = fields.character_id;
        } else if (kind === "request_observed" && config.app === "project_snow") {
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(fields.request_id || "")) return;
          safe.request_id = fields.request_id;
        } else if (kind === "entry_click" && config.app === "mywebsite") {
          if (!/^[0-9a-f-]{36}$/i.test(fields.jump_id || "") || !/^[A-Za-z0-9_-]{1,64}$/.test(fields.channel || "")) return;
          safe.jump_id = fields.jump_id; safe.channel = fields.channel;
        } else if (kind === "entry_arrival" && config.app === "project_snow") {
          if (!/^[0-9a-f-]{36}$/i.test(fields.jump_id || "")) return;
          safe.jump_id = fields.jump_id;
        } else return;
        const event = { schema_version: 1, event_id: uuid(), app: config.app, event_type: kind,
          occurred_at: new Date().toISOString(), ...identify(), ...safe };
        if (queue.length < 20) queue.push({ event, attempt: 0 });
        schedule();
      } catch { /* Business interactions must survive all adapter failures. */ }
    }
    function stop() {
      stopped = true; queue = [];
      if (timer) env.clearTimeout(timer);
      currentAbort?.abort();
      for (const name of ["anonymous", "session", "consent"]) {
        try { env.localStorage.removeItem(ns + name); } catch {}
      }
    }
    return Object.freeze({ get active() { return !stopped; }, track, stop, jump(url) {
      try {
        if (stopped || config.app !== "mywebsite") return url;
        const target = new URL(url, env.location.href);
        if (target.origin !== "https://snow.xiaob.dev" || target.hash) return url;
        const jump = uuid();
        track("entry_click", { jump_id: jump, channel: "portfolio" });
        // Fragment avoids logging an attribution ID in the destination request URL.
        target.hash = `snow_jump=${jump}`;
        return target.href;
      } catch { return url; }
    } });
  } catch { return inert; }
}

export function mountAnalytics(config, env = globalThis) {
  const adapter = createAnalytics(config, env);
  if (!adapter.active) return adapter;
  const listeners = new AbortController();
  let requestHook;
  try {
    adapter.track("page_view");
    if (config.app === "project_snow") {
      const match = /^#snow_jump=([0-9a-f-]{36})$/i.exec(env.location.hash);
      if (match) {
        adapter.track("entry_arrival", { jump_id: match[1] });
        env.history.replaceState(env.history.state, "", env.location.pathname + env.location.search);
      }
    }
    env.document.addEventListener("click", event => {
      try {
        const button = event.target.closest?.("[data-character]");
        if (button) adapter.track("character_select", { character_id: button.dataset.character });
        const link = event.target.closest?.("a[href]");
        if (link && config.app === "mywebsite") link.href = adapter.jump(link.href);
      } catch {}
    }, { passive: true, signal: listeners.signal });
    if (config.app === "project_snow") {
      requestHook = requestId => adapter.track("request_observed", { request_id: requestId });
      env.snowStatisticsRequest = requestHook;
    }
  } catch {}
  const mounted = Object.freeze({ track: adapter.track, jump: adapter.jump,
    get active() { return adapter.active; }, stop() {
      adapter.stop(); listeners.abort();
      if (requestHook && env.snowStatisticsRequest === requestHook) delete env.snowStatisticsRequest;
    } });
  try { env.addEventListener("snow:analytics:revoke", () => mounted.stop(), { signal: listeners.signal }); } catch {}
  return mounted;
}
