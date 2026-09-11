import { mountAnalytics } from "./analytics.mjs";

/** Optional consent control. Safe to delete together with its single script tag. */
export function installAnalytics(config, env = globalThis) {
  if (!config?.enabled) return;
  try {
    const key = `snow.statistics.v1.${config.app}.consent`;
    const control = env.document.createElement("button");
    control.type = "button";
    control.className = "snow-statistics-consent";
    control.title = "可选匿名统计：访问页面、角色选择与请求编号；不包含聊天或工具输入。可随时关闭。";
    let adapter = null;
    const enabled = () => {
      try { return Number(env.localStorage.getItem(key)) > Date.now(); } catch { return false; }
    };
    const render = () => { control.textContent = adapter ? "关闭匿名访问统计" : "允许匿名访问统计"; };
    const start = () => {
      const mounted = mountAnalytics({ ...config, consent: true }, env);
      adapter = mounted.active ? mounted : null;
      render();
    };
    control.addEventListener("click", () => {
      if (adapter) { adapter.stop(); adapter = null; render(); }
      else {
        try { env.localStorage.setItem(key, String(Date.now() + 30 * 86400000)); } catch {}
        start();
      }
    });
    if (enabled()) start();
    render();
    (env.document.querySelector("footer") || env.document.body).append(control);
  } catch { /* Missing DOM/storage must never break the host application. */ }
}
