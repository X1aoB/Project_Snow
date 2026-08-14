"use strict";

const { app, BrowserWindow, session } = require("electron");

const WEB_URL = "http://127.0.0.1:8080/";
const API_HEALTH_URL = "http://127.0.0.1:8000/health";
const REQUEST_TIMEOUT_MS = 4000;

let mainWindow;

function isLocalApplicationUrl(value) {
  try { return new URL(value).origin === new URL(WEB_URL).origin; } catch (_) { return false; }
}

async function endpointAvailable(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    return response.ok;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

function offlinePage() {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Project Snow v0.5.0</title>
<style>:root{color-scheme:light}*{box-sizing:border-box}body{display:grid;place-items:center;min-height:100vh;margin:0;padding:32px;font-family:"Microsoft YaHei",system-ui,sans-serif;background:#eaf4ff;color:#08234a;line-height:1.7}main{width:min(720px,100%);border:1px solid #9dbef1;border-radius:18px;padding:32px;background:#fff;box-shadow:0 18px 58px rgba(6,43,115,.14)}.mark{display:grid;place-items:center;width:48px;height:48px;border-radius:13px;background:#0a5cff;color:#fff;font-size:1.25rem;font-weight:800}h1{margin:18px 0 4px;color:#062b73}.version{color:#0a5cff;font-weight:700}code{color:#0759d2;background:#edf5ff;padding:2px 5px;border-radius:4px}button{border:1px solid #0a5cff;border-radius:9px;background:#0a5cff;color:white;padding:11px 18px;font:inherit;font-weight:700;cursor:pointer}button:focus-visible{outline:3px solid #24c7ff;outline-offset:3px}</style>
</head><body><main><div class="mark">S</div><h1>Project Snow 尚未连接</h1><p class="version">v0.5.0 · 本地测试版</p>
<p>桌面客户端连接的是现有本地服务。请在两个 PowerShell 窗口中启动：</p>
<p><code>cd App</code><br><code>python -m backend.snow_app.main</code></p>
<p><code>cd App</code><br><code>python scripts/dev_server.py</code></p>
<p>确认 API 在 <code>127.0.0.1:8000</code>、Web 在 <code>127.0.0.1:8080</code> 后点击重试。</p>
<button onclick="location.href='${WEB_URL}'">重新连接</button></main></body></html>`;
}

async function openApplication() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const ready = await Promise.all([
    endpointAvailable(API_HEALTH_URL),
    endpointAvailable(WEB_URL),
  ]);
  if (ready.every(Boolean)) {
    await mainWindow.loadURL(WEB_URL);
  } else {
    await mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(offlinePage())}`);
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 960,
    minWidth: 360,
    minHeight: 560,
    show: false,
    backgroundColor: "#eaf4ff",
    webPreferences: {
      preload: require("path").join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isLocalApplicationUrl(url)) event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("did-fail-load", () => {
    if (!mainWindow.webContents.getURL().startsWith("data:")) {
      mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(offlinePage())}`);
    }
  });
  openApplication();
}

app.whenReady().then(() => {
  session.defaultSession.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    return permission === "media"
      && isLocalApplicationUrl(requestingOrigin)
      && isLocalApplicationUrl(webContents?.getURL());
  });
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    callback(permission === "media" && isLocalApplicationUrl(webContents?.getURL()));
  });
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
