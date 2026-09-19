import { clearIdentifiers, consentExpiry, discardEntry, mountAnalytics, privacyBlocked } from "./analytics.mjs";

const COPY = {
  zh: {
    title: "允许访问统计吗？", allow: "允许访问统计", decline: "暂不允许", withdraw: "关闭访问统计", close: "收起说明",
    settings: "访问统计设置", on: "访问统计已开启", off: "访问统计已关闭", detail: "查看隐私说明",
    web: "允许后，仅记录允许页面的访问和试玩入口点击，用于改进本站。不会读取工具输入、聊天内容或密钥。",
    snow: "记录页面访问、角色选择和请求编号，帮助改进体验。不收集聊天内容或密钥。",
    quality: "另行统计已记录生成请求的结果和耗时，不含聊天内容，不受此开关影响。",
    retention: "可随时在“设置 → 隐私”中关闭；拒绝不影响聊天。",
    blocked: "浏览器隐私信号已阻止访问统计。", unavailable: "浏览器无法保存统计偏好，访问统计保持关闭。",
  },
  en: {
    title: "Optional visit analytics — your choice", allow: "Allow visit analytics", decline: "Not now", withdraw: "Turn off visit analytics", close: "Close notice",
    settings: "Visit analytics settings", on: "Visit analytics is on", off: "Visit analytics is off", detail: "Full data and privacy notice",
    web: "If allowed, we record visits to approved pages and clicks to try Xiaoji Terminal to improve this site. Tool input, chat content and API keys are not read.",
    snow: "If allowed, we record page visits, public character selections and request IDs to understand usage and entry conversion. Chat content, API keys, feedback and the chat database are not read.",
    quality: "Separate service quality statistics: Xiaoji Terminal's existing filtered generation-completion logs provide recorded request counts, outcomes and duration, without visit analytics identifiers. This visit analytics choice does not control that processing.",
    retention: "Each site uses its own analytics identifiers and consent preference for up to 30 days. Withdrawal stops future visit analytics and removes this site's identifiers. Already accepted events follow a 7-day retention target, identifier support records up to 30 days and daily aggregates up to 90 days. Only delayed aggregates are public, with small-count protection.",
    blocked: "A browser privacy signal has blocked visit analytics.", unavailable: "This browser cannot save the preference. Visit analytics remains off.",
  },
};

/** A non-blocking visible notice and a persistent keyboard-accessible settings button. */
export function installAnalytics(config, env = globalThis) {
  if (!config?.enabled || !["mywebsite", "project_snow"].includes(config.app)) return;
  try {
    const key = `snow.statistics.v1.${config.app}.consent`, now = () => env.Date?.now() ?? Date.now();
    const document = env.document;
    if (document.getElementById?.("snow-statistics-settings")) return;
    const settingsHost = config.app === "project_snow" ? document.getElementById("snow-statistics-settings-host") : null;
    if (config.app === "project_snow" && !settingsHost) return;
    const make = (tag, parent) => { const node = document.createElement(tag); parent?.append(node); return node; };
    const panel = make("section"), settings = make("button");
    panel.id = "snow-statistics-notice"; panel.className = "snow-statistics-notice";
    panel.setAttribute("data-no-translate", ""); panel.setAttribute("aria-labelledby", "snow-statistics-title");
    settings.id = "snow-statistics-settings"; settings.type = "button"; settings.className = "snow-statistics-settings";
    settings.setAttribute("aria-controls", panel.id); settings.setAttribute("data-no-translate", "");
    const heading = make("h2", panel); heading.id = "snow-statistics-title";
    const copyBody = make("div", panel); copyBody.className = "snow-statistics-copy";
    const description = make("p", copyBody), quality = make("p", copyBody), retention = make("p", copyBody);
    const detail = make("a", copyBody); detail.href = "/privacy/";
    const status = make("p", copyBody); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const actions = make("div", panel); actions.className = "snow-statistics-actions";
    const allow = make("button", actions), decline = make("button", actions), close = make("button", actions);
    for (const button of [allow, decline, close]) button.type = "button";
    let adapter = null, failure = false;
    const language = () => config.app === "mywebsite" && /^en/i.test(document.documentElement.lang) ? "en" : "zh";
    const permitted = () => !privacyBlocked(env) && consentExpiry(config.app, env) > now();
    const render = () => {
      const copy = COPY[language()], active = Boolean(adapter?.active);
      heading.textContent = copy.title; description.textContent = copy[config.app === "mywebsite" ? "web" : "snow"];
      quality.textContent = copy.quality; retention.textContent = copy.retention; detail.textContent = copy.detail;
      allow.textContent = copy.allow; allow.disabled = active || privacyBlocked(env);
      decline.textContent = active ? copy.withdraw : copy.decline; close.textContent = copy.close;
      status.textContent = privacyBlocked(env) ? copy.blocked : failure ? copy.unavailable : active ? copy.on : copy.off;
      settings.textContent = `${copy.settings} · ${active ? copy.on : copy.off}`;
      settings.setAttribute("aria-expanded", String(!panel.hidden));
    };
    const start = () => {
      if (!permitted() || adapter?.active) return;
      const mounted = mountAnalytics({ ...config, consent: true, onStop() { adapter = null; render(); } }, env);
      adapter = mounted.active ? mounted : null;
    };
    const stop = () => { adapter?.stop(); adapter = null; clearIdentifiers(config.app, env); discardEntry(env); };
    const choose = accepted => {
      failure = false;
      if (accepted && privacyBlocked(env)) { stop(); render(); return; }
      if (!accepted) stop();
      try {
        const value = (accepted ? 1 : -1) * (now() + 30 * 86400000);
        env.localStorage.setItem(key, String(value));
        if (consentExpiry(config.app, env) !== value) throw Error("preference not persisted");
        if (accepted) start();
        panel.hidden = true;
      } catch { failure = true; stop(); }
      render();
    };
    allow.addEventListener("click", () => choose(true)); decline.addEventListener("click", () => choose(false));
    const closeNotice = () => {
      panel.hidden = true; render();
      const defaultTrigger = document.getElementById("contact-panel")?.getAttribute("aria-hidden") === "true" ? "open-contacts" : "open-settings";
      const returnTo = panel.classList.contains("snow-statistics-inline") ? settings : document.getElementById(defaultTrigger) || settings;
      returnTo.focus();
    };
    close.addEventListener("click", closeNotice);
    settings.addEventListener("click", () => {
      const inline = panel.classList.contains("snow-statistics-inline");
      if (settingsHost) { settingsHost.append(panel); panel.classList.add("snow-statistics-inline"); }
      panel.hidden = settingsHost && !inline ? false : !panel.hidden;
      render(); if (!panel.hidden) close.focus();
    });
    panel.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeNotice(); }
    });
    if (settingsHost) document.getElementById("settings-dialog")?.addEventListener("close", () => {
      if (!panel.classList.contains("snow-statistics-inline")) return;
      panel.hidden = true; panel.classList.remove("snow-statistics-inline"); document.body.append(panel); render();
    });
    // A grant in another tab applies on a future page load; withdrawal is immediate.
    env.addEventListener("storage", event => {
      if (event.key !== null && event.key !== key) return;
      if (!permitted()) stop();
      render();
    });
    env.addEventListener("snow:analytics:revoke", () => choose(false));
    env.addEventListener("pageshow", () => { if (!permitted()) stop(); render(); });
    if (env.MutationObserver) new env.MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["lang", "data-locale"] });
    const expiry = consentExpiry(config.app, env);
    panel.hidden = Math.abs(expiry) > now();
    if (permitted()) start();
    else {
      clearIdentifiers(config.app, env);
      if (config.app === "project_snow") discardEntry(env);
    }
    render();
    (settingsHost || document.querySelector("footer") || document.body).append(settings);
    if (settingsHost) document.getElementById("snow-statistics-disabled").hidden = true;
    document.body.append(panel);
    return Object.freeze({ stop: () => choose(false) });
  } catch { /* Missing DOM/storage must never break the host application. */ }
}
