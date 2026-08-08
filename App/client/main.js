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
<html lang="zh-CN"><head><meta charset="utf-8"><title>Project Snow Preview</title>
<style>body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#111313;color:#f2f3f1;padding:48px;line-height:1.7}main{max-width:720px;margin:auto;border:1px solid #474e49;border-radius:7px;padding:28px;background:#1e2120}h1{margin-top:0}code{color:#9ee0d5}button{border:1px solid #4e9388;border-radius:7px;background:#286f67;color:white;padding:10px 16px;font:inherit;cursor:pointer}</style>
</head><body><main><h1>Project Snow Preview 尚未连接</h1>
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
    backgroundColor: "#111313",
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
