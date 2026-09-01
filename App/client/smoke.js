"use strict";

const { app, BrowserWindow, session } = require("electron");
const fs = require("fs");
const path = require("path");

const LANDING_URL = "http://127.0.0.1:8080/";
const IMMERSIVE_URL = "http://127.0.0.1:8080/immersive/";
const WORKSPACE_URL = "http://127.0.0.1:8080/workspace/";
const SCREENSHOT_PATH = path.resolve(__dirname, "..", "runtime", "screenshots", "electron-mobile.png");
const FACE_SCREENSHOT_PATH = path.resolve(__dirname, "..", "runtime", "screenshots", "electron-face-mobile.png");
const DESKTOP_SCREENSHOT_PATH = path.resolve(__dirname, "..", "runtime", "screenshots", "electron-face-desktop.png");

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

async function waitForLanding(window) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const ready = await window.webContents.executeJavaScript(
      "document.querySelectorAll('.experience-card').length === 1 && document.body.innerText.includes('v0.5.0')",
      true,
    );
    if (ready) return;
    await delay(100);
  }
  throw new Error("Landing bootstrap did not finish.");
}

async function waitForChatReady(window) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const ready = await window.webContents.executeJavaScript(
      "!document.getElementById('timeline').innerText.includes('正在读取本地会话')",
      true,
    );
    if (ready) return;
    await delay(100);
  }
  throw new Error("Conversation history did not finish loading.");
}

async function waitForChannel(window, channel) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const current = await window.webContents.executeJavaScript(
      "document.getElementById('chat-app').dataset.channel",
      true,
    );
    if (current === channel) return;
    await delay(100);
  }
  throw new Error(`Immersive surface did not switch to ${channel}.`);
}

async function run() {
  const consoleErrors = [];
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const window = new BrowserWindow({
    width: 390,
    height: 844,
    useContentSize: true,
    show: false,
    backgroundColor: "#eaf4ff",
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

  await window.loadURL(LANDING_URL);
  await waitForLanding(window);
  const landing = await window.webContents.executeJavaScript(`(() => ({
    surface: document.body.dataset.surface,
    entries: document.querySelectorAll('.experience-card').length,
    versionVisible: document.body.innerText.includes('v0.5.0 · 本地测试版'),
    rosterRemoved: !document.body.innerText.includes('她们都在这里')
  }))()`, true);

  await window.loadURL(IMMERSIVE_URL);
  await waitForCharacters(window);
  await waitForChatReady(window);
  await window.webContents.insertCSS("*,*::before,*::after{animation:none!important;transition:none!important}");
  const initialChannel = await window.webContents.executeJavaScript(
    "document.getElementById('chat-app').dataset.channel",
    true,
  );
  if (initialChannel !== "text") {
    await window.webContents.executeJavaScript("document.getElementById('open-communicator').click()", true);
    await waitForChannel(window, "text");
  }
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
      )?.id || "",
      surface: document.body.dataset.surface,
      channel: document.getElementById('chat-app').dataset.channel,
      textVisible: !document.getElementById('text-surface').hidden,
      stageHidden: document.getElementById('in-person-surface').hidden,
      actionHidden: document.getElementById('analyst-action-field').hidden,
      oldChannelControlRemoved: !document.getElementById('channel-control'),
      agentControlsHidden: getComputedStyle(document.getElementById('assistant-tool-toggle')).display === 'none',
      technicalModelHidden: getComputedStyle(document.getElementById('active-model')).display === 'none'
    };
  })()`, true);
  const inputX = Math.round((metrics.inputRect.left + metrics.inputRect.right) / 2);
  const inputY = Math.round((metrics.inputRect.top + metrics.inputRect.bottom) / 2);
  window.webContents.sendInputEvent({ type: "mouseDown", x: inputX, y: inputY, button: "left", clickCount: 1 });
  window.webContents.sendInputEvent({ type: "mouseUp", x: inputX, y: inputY, button: "left", clickCount: 1 });
  await delay(50);
  metrics.activeElement = await window.webContents.executeJavaScript("document.activeElement?.id || ''", true);

  metrics.providerSettings = await window.webContents.executeJavaScript(`(() => {
    document.getElementById("open-settings").click();
    document.querySelector('[data-provider-choice="deepseek"]').click();
    const model = document.getElementById("provider-model");
    model.value = "deepseek-v4-flash";
    model.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector(".windows-env-guide").open = true;
    const guide = document.getElementById("windows-env-code").textContent;
    const result = {
      vendorButtons: document.querySelectorAll("[data-provider-choice]").length,
      selectedVendor: document.getElementById("provider-kind").value,
      guideHasEndpoint: guide.includes("https://api.deepseek.com/v1"),
      guideHasModel: guide.includes("deepseek-v4-flash"),
      guideHasSecretPlaceholder: guide.includes("<粘贴你的 API Key>"),
    };
    document.getElementById("settings-dialog").close();
    return result;
  })()`, true);

  await window.webContents.insertCSS(".toast{display:none!important}");

  fs.mkdirSync(path.dirname(SCREENSHOT_PATH), { recursive: true });
  fs.writeFileSync(SCREENSHOT_PATH, (await window.webContents.capturePage()).toPNG());

  await window.webContents.executeJavaScript("document.getElementById('go-in-person').click()", true);
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const open = await window.webContents.executeJavaScript("document.getElementById('presence-dialog').open", true);
    if (open) break;
    await delay(100);
  }
  await window.webContents.executeJavaScript("document.getElementById('confirm-presence-transition').click()", true);
  await waitForChannel(window, "in_person");
  await delay(350);
  const faceMetrics = await window.webContents.executeJavaScript(`(() => ({
    channel: document.getElementById('chat-app').dataset.channel,
    stageVisible: !document.getElementById('in-person-surface').hidden,
    textHidden: document.getElementById('text-surface').hidden,
    noBubbleTimelineInStage: !document.getElementById('in-person-surface').querySelector('.message'),
    largeStagePortraitRemoved: !document.getElementById('stage-character-visual'),
    backdropLoaded: document.getElementById('scene-backdrop').complete && document.getElementById('scene-backdrop').naturalWidth > 0,
    onlySceneBackdropImage: [...document.querySelectorAll('#scene-stage img')].every((item) => item.id === 'scene-backdrop'),
    stagePortraitVisible: document.getElementById('stage-portrait').getBoundingClientRect().width > 0,
    portraitKind: document.getElementById('stage-portrait').dataset.portraitKind,
    faceOverlayRemoved: !document.querySelector('.expression-overlay'),
    actionToggleVisible: getComputedStyle(document.getElementById('toggle-action')).display !== 'none',
    location: document.getElementById('stage-location').innerText,
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
  }))()`, true);
  faceMetrics.actionInputFocus = await window.webContents.executeJavaScript(`(() => {
    document.getElementById('toggle-action').click();
    document.getElementById('action-input').focus();
    return !document.getElementById('analyst-action-field').hidden && document.activeElement.id === 'action-input';
  })()`, true);
  faceMetrics.hiddenUi = await window.webContents.executeJavaScript(`(() => {
    document.getElementById('toggle-stage-ui').click();
    const hidden = document.getElementById('chat-app').classList.contains('stage-ui-hidden')
      && !document.getElementById('restore-stage-ui').hidden;
    document.getElementById('restore-stage-ui').click();
    return hidden && !document.getElementById('chat-app').classList.contains('stage-ui-hidden');
  })()`, true);
  faceMetrics.revealControl = await window.webContents.executeJavaScript(`(() => {
    const dialogue = document.getElementById('stage-dialogue');
    dialogue.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return !dialogue.classList.contains('is-revealing');
  })()`, true);
  fs.writeFileSync(FACE_SCREENSHOT_PATH, (await window.webContents.capturePage()).toPNG());
  await window.webContents.executeJavaScript("document.getElementById('open-transcript').click()", true);
  await delay(250);
  faceMetrics.transcriptOpen = await window.webContents.executeJavaScript(
    "document.getElementById('transcript-panel').classList.contains('open') && !document.getElementById('drawer-scrim').hidden",
    true,
  );
  await window.webContents.executeJavaScript("document.getElementById('close-transcript').click()", true);

  window.setContentSize(1365, 900);
  await delay(250);
  const desktopFace = await window.webContents.executeJavaScript(`(() => ({
    viewport: { width: window.innerWidth, height: window.innerHeight },
    stageVisible: !document.getElementById('in-person-surface').hidden,
    contactOffCanvas: getComputedStyle(document.getElementById('contact-panel')).transform !== 'none',
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
  }))()`, true);
  fs.writeFileSync(DESKTOP_SCREENSHOT_PATH, (await window.webContents.capturePage()).toPNG());
  window.setContentSize(390, 844);
  await delay(150);
  await window.webContents.executeJavaScript("document.getElementById('open-communicator').click()", true);
  await waitForChannel(window, "text");

  await window.webContents.executeJavaScript(
    "document.getElementById('open-contacts').click()",
    true,
  );
  await delay(250);
  metrics.contactDrawer = await window.webContents.executeJavaScript(`(() => ({
    open: document.getElementById("contact-panel").classList.contains("open"),
    transform: getComputedStyle(document.getElementById("contact-panel")).transform,
    scrimVisible: !document.getElementById("drawer-scrim").hidden
  }))()`, true);
  window.webContents.sendInputEvent({ type: "mouseDown", x: 370, y: 420, button: "left", clickCount: 1 });
  window.webContents.sendInputEvent({ type: "mouseUp", x: 370, y: 420, button: "left", clickCount: 1 });
  await delay(250);

  await window.loadURL(WORKSPACE_URL);
  const workspaceReady = await window.webContents.executeJavaScript(
    "Boolean(document.getElementById('feedback-inbox') && document.getElementById('mvp-message') && document.getElementById('workspace-detail-drawer'))",
    true,
  );

  const result = { landing, immersive: metrics, faceMetrics, desktopFace, workspaceReady, consoleErrors, screenshots: [SCREENSHOT_PATH, FACE_SCREENSHOT_PATH, DESKTOP_SCREENSHOT_PATH] };
  const passed = landing.surface === "landing"
    && landing.entries === 1
    && landing.versionVisible
    && landing.rosterRemoved
    && metrics.mobileMedia
    && metrics.characterCount === 22
    && metrics.surface === "immersive"
    && metrics.channel === "text"
    && metrics.textVisible
    && metrics.stageHidden
    && metrics.actionHidden
    && metrics.oldChannelControlRemoved
    && metrics.agentControlsHidden
    && metrics.technicalModelHidden
    && !metrics.horizontalOverflow
    && metrics.contactTransform !== "none"
    && metrics.contactDrawer.open
    && metrics.contactDrawer.scrimVisible
    && metrics.inputPointTarget === "message-input"
    && metrics.activeElement === "message-input"
    && metrics.providerSettings.vendorButtons === 6
    && metrics.providerSettings.selectedVendor === "deepseek"
    && metrics.providerSettings.guideHasEndpoint
    && metrics.providerSettings.guideHasModel
    && metrics.providerSettings.guideHasSecretPlaceholder
    && faceMetrics.channel === "in_person"
    && faceMetrics.stageVisible
    && faceMetrics.textHidden
    && faceMetrics.noBubbleTimelineInStage
    && faceMetrics.largeStagePortraitRemoved
    && faceMetrics.backdropLoaded
    && faceMetrics.onlySceneBackdropImage
    && faceMetrics.stagePortraitVisible
    && ["headshot", "full_body"].includes(faceMetrics.portraitKind)
    && faceMetrics.faceOverlayRemoved
    && faceMetrics.actionToggleVisible
    && faceMetrics.actionInputFocus
    && faceMetrics.hiddenUi
    && faceMetrics.revealControl
    && Boolean(faceMetrics.location)
    && faceMetrics.transcriptOpen
    && !faceMetrics.horizontalOverflow
    && desktopFace.viewport.width >= 1360
    && desktopFace.stageVisible
    && desktopFace.contactOffCanvas
    && !desktopFace.horizontalOverflow
    && workspaceReady
    && consoleErrors.length === 0;
  process.stdout.write(`${JSON.stringify({ ...result, passed }, null, 2)}\n`);
  window.destroy();
  app.exit(passed ? 0 : 1);
}

app.whenReady().then(run).catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  app.exit(1);
});
