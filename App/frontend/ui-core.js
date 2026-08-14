export const byId = (id) => document.getElementById(id);

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function renderIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
}

export async function api(path, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const raw = response.status === 204 ? "" : await response.text();
    let payload = null;
    try { payload = raw ? JSON.parse(raw) : null; } catch (_) { payload = raw; }
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === "string" ? detail : detail?.message || `请求失败（${response.status}）`;
      const error = new Error(message);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("等待回复超时，可以重试；幂等标识会避免重复写入。");
      timeoutError.name = "TimeoutError";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export function storageGet(key, fallback = "") {
  try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
}

export function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* local-only preference */ }
}

export function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function textPortrait(character, size = "") {
  const name = String(character?.character_name || "?").trim();
  const monogram = name.slice(0, 1) || "?";
  const generated = String(character?.generated_portrait || "").trim();
  if (generated) {
    return `<span class="portrait ${size}"><img src="${escapeHtml(generated)}" alt="${escapeHtml(name)}" /></span>`;
  }
  return `<span class="portrait portrait-text ${size}" aria-label="${escapeHtml(name)}">${escapeHtml(monogram)}</span>`;
}
