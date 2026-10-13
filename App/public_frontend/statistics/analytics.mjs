/** Optional v1 adapter. Vendored into each product; never import a sibling checkout. */
const JUMP = /^#snow_jump=([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i;
const namespace = app => `snow.statistics.v1.${app}.`;
const clock = env => env.Date?.now() ?? Date.now();

export function privacyBlocked(env = globalThis) {
  try { return Boolean(env.navigator?.globalPrivacyControl || env.navigator?.doNotTrack === "1" || env.doNotTrack === "1"); }
  catch { return true; }
}

export function consentExpiry(app, env = globalThis) {
  try {
    const value = Number(env.localStorage.getItem(namespace(app) + "consent"));
    return Number.isFinite(value) ? value : 0;
  }
  catch { return 0; }
}

export function clearIdentifiers(app, env = globalThis) {
  for (const name of ["anonymous", "session"]) {
    try { env.localStorage.removeItem(namespace(app) + name); } catch {}
  }
}

/** Discard only our exact fragment; never inspect or rewrite business anchors. */
export function discardEntry(env = globalThis) {
  try {
    const match = JUMP.exec(env.location.hash);
    if (match) env.history.replaceState(env.history.state, "", env.location.pathname + env.location.search);
    return match?.[1] || null;
  } catch { return null; }
}

function stripJump(url, env) {
  try {
    const target = new URL(url, env.location.href);
    if (target.origin === "https://snow.xiaob.dev" && JUMP.test(target.hash)) {
      target.hash = ""; return target.href;
    }
  } catch {}
  return url;
}

export function createAnalytics(config, env = globalThis) {
  const inert = Object.freeze({ active: false, track() {}, stop() {}, jump(url) { return url; } });
  if (!config?.enabled || !config?.consent || !["mywebsite", "project_snow"].includes(config.app)) return inert;
  if (typeof config.endpoint !== "string" || !config.endpoint.trim()) return inert;
  try {
    if (privacyBlocked(env) || consentExpiry(config.app, env) <= clock(env)) return inert;
    const endpoint = new URL(config.endpoint, env.location.href);
    if (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["localhost", "127.0.0.1"].includes(endpoint.hostname))) return inert;
    const ns = namespace(config.app), lifecycle = new AbortController();
    const uuid = () => env.crypto.randomUUID();
    let anonymous = null, session = null, stopped = false, queue = [], inFlight = false;
    let timer = null, expiryTimer = null, currentAbort = null;
    const paths = new Set(config.paths || ["/"]), characters = new Set(config.characters || []);
    const read = key => { try { return JSON.parse(env.localStorage.getItem(ns + key)); } catch { return null; } };
    const save = (key, value) => env.localStorage.setItem(ns + key, JSON.stringify(value));
    function stop(reason = "withdrawn") {
      if (stopped) return;
      stopped = true; queue = []; anonymous = null; session = null;
      if (timer) env.clearTimeout(timer);
      if (expiryTimer) env.clearTimeout(expiryTimer);
      currentAbort?.abort(); lifecycle.abort();
      clearIdentifiers(config.app, env);
      // A negative value is a separate refusal preference, never an identifier.
      if (consentExpiry(config.app, env) > 0) {
        try { env.localStorage.removeItem(ns + "consent"); } catch {}
      }
      try { config.onStop?.(reason); } catch {}
    }
    function allowed() {
      if (stopped) return false;
      if (privacyBlocked(env) || consentExpiry(config.app, env) <= clock(env)) {
        stop("permission-ended"); return false;
      }
      return true;
    }
    function watchExpiry() {
      if (expiryTimer) env.clearTimeout(expiryTimer);
      if (!allowed()) return;
      expiryTimer = env.setTimeout(watchExpiry, Math.min(consentExpiry(config.app, env) - clock(env), 2147483647));
    }
    const identify = () => {
      const at = clock(env), old = read("anonymous");
      anonymous ||= old && old.expires > at ? old : { id: uuid(), expires: at + 30 * 86400000 };
      if (anonymous.expires <= at) anonymous = { id: uuid(), expires: at + 30 * 86400000 };
      save("anonymous", anonymous);
      const previous = read("session");
      session = previous && at - previous.last < 1800000 ? previous : { id: uuid(), last: at };
      session.last = at; save("session", session);
      return { anonymous_id: anonymous.id, session_id: session.id };
    };
    const schedule = () => { if (!timer && !stopped) timer = env.setTimeout(() => { timer = null; void flush(); }, 1000); };
    async function flush() {
      if (!allowed() || inFlight || !queue.length) return;
      inFlight = true;
      const batch = queue.splice(0, 10), controller = new AbortController(); currentAbort = controller;
      const timeout = env.setTimeout(() => controller.abort(), 1500);
      try {
        const response = await env.fetch(endpoint.href, { method: "POST", credentials: "omit", mode: "cors",
          referrerPolicy: "no-referrer", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ events: batch.map(x => x.event) }), signal: controller.signal, keepalive: true });
        if (!response.ok && (response.status >= 500 || response.status === 429)) throw new Error("retry");
      } catch {
        if (allowed()) queue = [...batch.filter(x => x.attempt++ < 1), ...queue].slice(0, 20);
      } finally {
        env.clearTimeout(timeout); currentAbort = null; inFlight = false;
        if (queue.length) schedule();
      }
    }
    function track(kind, fields = {}) {
      if (!allowed()) return false;
      try {
        if (queue.length >= 20) return false;
        const safe = {};
        if (kind === "page_view") {
          const path = new URL(fields.path || env.location.href, env.location.href).pathname;
          if (!paths.has(path)) return false;
          safe.path = path;
        } else if (kind === "character_select" && config.app === "project_snow") {
          if (!characters.has(fields.character_id)) return false;
          safe.character_id = fields.character_id;
        } else if (kind === "request_observed" && config.app === "project_snow") {
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(fields.request_id || "")) return false;
          safe.request_id = fields.request_id;
        } else if (kind === "entry_click" && config.app === "mywebsite") {
          if (!JUMP.test(`#snow_jump=${fields.jump_id}`) || !/^[A-Za-z0-9_-]{1,64}$/.test(fields.channel || "")) return false;
          safe.jump_id = fields.jump_id; safe.channel = fields.channel;
        } else if (kind === "entry_arrival" && config.app === "project_snow") {
          if (!JUMP.test(`#snow_jump=${fields.jump_id}`)) return false;
          safe.jump_id = fields.jump_id;
        } else return false;
        queue.push({ event: { schema_version: 1, event_id: uuid(), app: config.app, event_type: kind,
          occurred_at: new Date(clock(env)).toISOString(), ...identify(), ...safe }, attempt: 0 });
        schedule(); return true;
      } catch { stop("storage-unavailable"); return false; }
    }
    try {
      env.addEventListener("storage", event => {
        if (event.key === null || event.key === ns + "consent") watchExpiry();
      }, { signal: lifecycle.signal });
      env.addEventListener("pageshow", watchExpiry, { signal: lifecycle.signal });
      env.addEventListener("snow:analytics:revoke", () => stop(), { signal: lifecycle.signal });
      env.document?.addEventListener("visibilitychange", watchExpiry, { signal: lifecycle.signal });
    } catch {}
    watchExpiry();
    return Object.freeze({ get active() { return allowed(); }, track, stop, jump(url) {
      try {
        if (!allowed()) return stripJump(url, env);
        if (config.app !== "mywebsite") return url;
        const target = new URL(url, env.location.href);
        if (target.origin !== "https://snow.xiaob.dev" || (target.hash && !JUMP.test(target.hash))) return url;
        const jump = uuid();
        if (!track("entry_click", { jump_id: jump, channel: "portfolio" })) return stripJump(url, env);
        target.hash = `snow_jump=${jump}`; return target.href;
      } catch { return url; }
    } });
  } catch { return inert; }
}

export function mountAnalytics(config, env = globalThis) {
  if (!config?.enabled) return createAnalytics(config, env);
  const listeners = new AbortController(), modifiedLinks = new Set();
  let requestHook;
  const cleanup = reason => {
    listeners.abort();
    if (requestHook && env.snowStatisticsRequest === requestHook) delete env.snowStatisticsRequest;
    for (const link of modifiedLinks) {
      try { link.href = stripJump(link.href, env); } catch {}
    }
    modifiedLinks.clear();
    try { discardEntry(env); config.onStop?.(reason); } catch {}
  };
  const adapter = createAnalytics({ ...config, onStop: cleanup }, env);
  const arrival = config.app === "project_snow" ? discardEntry(env) : null;
  if (!adapter.active) return adapter;
  try {
    adapter.track("page_view");
    if (arrival) adapter.track("entry_arrival", { jump_id: arrival });
    if (!adapter.active) return adapter;
    env.document.addEventListener("click", event => {
      try {
        const button = event.target.closest?.("[data-character]");
        if (button) adapter.track("character_select", { character_id: button.dataset.character });
        const link = event.target.closest?.("a[href]");
        if (link && config.app === "mywebsite") {
          const next = adapter.jump(link.href);
          if (next !== link.href) { modifiedLinks.add(link); link.href = next; }
        }
      } catch {}
    }, { passive: true, signal: listeners.signal });
    if (config.app === "project_snow") {
      requestHook = requestId => adapter.track("request_observed", { request_id: requestId });
      env.snowStatisticsRequest = requestHook;
    }
  } catch {}
  return adapter;
}
