"use strict";

const { app, BrowserWindow, session } = require("electron");
const fs = require("fs");
const path = require("path");

const CHAT_URL = "http://127.0.0.1:8080/";
const WORKSPACE_URL = "http://127.0.0.1:8080/workspace/";
const SCREENSHOT_PATH = path.resolve(__dirname, "..", "runtime", "screenshots", "electron-mobile.png");

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForCharacters(window) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const count = await window.webContents.executeJavaScript(
      "document.querySelectorAll('.character-item').length",
      true,
    );
    if (count === 22) return;
    await delay(100);
  }
  throw new Error("Character bootstrap did not finish.");
}

async function run() {
  const consoleErrors = [];
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const window = new BrowserWindow({
    width: 390,
    height: 844,
    useContentSize: true,
    show: false,
    backgroundColor: "#111313",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.on("console-message", (_event, level, message) => {
    if (level >= 2) consoleErrors.push(message);
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  await window.loadURL(CHAT_URL);
  await waitForCharacters(window);
  const metrics = await window.webContents.executeJavaScript(`(() => {
    const input = document.getElementById("message-input");
    const inputRect = input.getBoundingClientRect();
    const contacts = document.getElementById("contact-panel");
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      mobileMedia: window.matchMedia("(max-width: 820px)").matches,
      characterCount: document.querySelectorAll(".character-item").length,
      horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      contactTransform: getComputedStyle(contacts).transform,
      inputRect: { left: inputRect.left, right: inputRect.right, top: inputRect.top, bottom: inputRect.bottom },
      inputPointTarget: document.elementFromPoint(
        inputRect.left + inputRect.width / 2,
        inputRect.top + inputRect.height / 2
      )?.id || ""
    };
  })()`, true);
  const inputX = Math.round((metrics.inputRect.left + metrics.inputRect.right) / 2);
  const inputY = Math.round((metrics.inputRect.top + metrics.inputRect.bottom) / 2);
  window.webContents.sendInputEvent({ type: "mouseDown", x: inputX, y: inputY, button: "left", clickCount: 1 });
  window.webContents.sendInputEvent({ type: "mouseUp", x: inputX, y: inputY, button: "left", clickCount: 1 });
  await delay(50);
  metrics.activeElement = await window.webContents.executeJavaScript("document.activeElement?.id || ''", true);

  const openContactsRect = await window.webContents.executeJavaScript(`(() => {
    const rect = document.getElementById("open-contacts").getBoundingClientRect();
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  })()`, true);
  const contactsX = Math.round((openContactsRect.left + openContactsRect.right) / 2);
  const contactsY = Math.round((openContactsRect.top + openContactsRect.bottom) / 2);
  window.webContents.sendInputEvent({ type: "mouseDown", x: contactsX, y: contactsY, button: "left", clickCount: 1 });
  window.webContents.sendInputEvent({ type: "mouseUp", x: contactsX, y: contactsY, button: "left", clickCount: 1 });
  await delay(250);
  metrics.contactDrawer = await window.webContents.executeJavaScript(`(() => ({
    open: document.getElementById("contact-panel").classList.contains("open"),
    transform: getComputedStyle(document.getElementById("contact-panel")).transform,
    scrimVisible: !document.getElementById("drawer-scrim").hidden
  }))()`, true);
  window.webContents.sendInputEvent({ type: "mouseDown", x: 370, y: 420, button: "left", clickCount: 1 });
  window.webContents.sendInputEvent({ type: "mouseUp", x: 370, y: 420, button: "left", clickCount: 1 });
  await delay(250);

  fs.mkdirSync(path.dirname(SCREENSHOT_PATH), { recursive: true });
  fs.writeFileSync(SCREENSHOT_PATH, (await window.webContents.capturePage()).toPNG());

  await window.loadURL(WORKSPACE_URL);
  const workspaceReady = await window.webContents.executeJavaScript(
    "Boolean(document.getElementById('feedback-inbox') && document.getElementById('mvp-message'))",
    true,
  );

  const result = { ...metrics, workspaceReady, consoleErrors, screenshot: SCREENSHOT_PATH };
  const passed = metrics.mobileMedia
    && metrics.characterCount === 22
    && !metrics.horizontalOverflow
    && metrics.contactTransform !== "none"
    && metrics.contactDrawer.open
    && metrics.contactDrawer.scrimVisible
    && metrics.inputPointTarget === "message-input"
    && metrics.activeElement === "message-input"
    && workspaceReady
    && consoleErrors.length === 0;
  process.stdout.write(`${JSON.stringify({ ...result, passed }, null, 2)}\n`);
  window.destroy();
  app.quit();
  if (!passed) process.exitCode = 1;
}

app.whenReady().then(run).catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  app.quit();
  process.exitCode = 1;
});
