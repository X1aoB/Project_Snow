"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow, nativeImage } = require("electron");
process.env.PROJECT_SNOW_DESKTOP_TEST = "1";
const { offlinePage, isLocalApplicationUrl, ICON_PATH } = require("./main");
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "xiaoji-brand-smoke-"));
app.setPath("userData", profile);
app.whenReady().then(async () => {
  assert.equal(nativeImage.createFromPath(ICON_PATH).isEmpty(), false);
  assert.equal(isLocalApplicationUrl("http://127.0.0.1:8080/immersive/"), true);
  assert.equal(isLocalApplicationUrl("https://attacker.example/"), false);
  assert.equal(isLocalApplicationUrl("http://127.0.0.1:8080@attacker.example/"), false);
  const window = new BrowserWindow({show:false,width:900,height:740,icon:ICON_PATH,
    webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(offlinePage())}`);
  const identity = await window.webContents.executeJavaScript(`({title:document.title,
    logoLoaded:document.querySelector('img.mark').complete && document.querySelector('img.mark').naturalWidth===1024,
    noNode:typeof require==='undefined', text:document.querySelector('h1').textContent})`);
  assert.match(identity.title, /小吉终端/);
  assert.match(identity.text, /小吉终端/);
  assert.equal(identity.logoLoaded, true);
  assert.equal(identity.noNode, true);
  if (process.env.PROJECT_SNOW_SMOKE_SCREENSHOT) {
    await window.webContents.executeJavaScript('document.fonts.ready');
    await new Promise(resolve => setTimeout(resolve, 500));
    const screenshot = await window.webContents.capturePage();
    fs.writeFileSync(process.env.PROJECT_SNOW_SMOKE_SCREENSHOT,screenshot.toPNG());
  }
  process.stdout.write(JSON.stringify({status:"ok",electron:process.versions.electron,...identity})+"\n");
  window.destroy();
  app.exit(0);
}).catch(error => { process.stderr.write(String(error.stack)+"\n"); app.exit(1); });
