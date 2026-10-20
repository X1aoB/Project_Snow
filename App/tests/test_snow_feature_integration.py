"""First-release features against fingerprinted assets and loopback-only fixtures."""
from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from scripts.fingerprint_public_frontend import fingerprint
from tests import test_dialogue_v2_frontend as dialogue
from tests import test_snow_adv_features_ui as ui


APP_ROOT = Path(__file__).resolve().parents[1]
FEATURE_URLS = (
    "/modules/snow-feature-logic.js", "/modules/snow-feature-icons.js",
    "/modules/snow-features.css", "/modules/snow-feature-card.js",
    "/modules/snow-features.js", "/modules/snow-reading-logic.js",
    "/modules/snow-reminder-logic.js", "/modules/snow-feature-runtime.js",
    "/modules/snow-proactive-client.js",
)
READ_STORES = """async () => {
  const db=await new Promise((resolve,reject)=>{const request=indexedDB.open('project-snow-public');
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
  try {
    const read=name=>new Promise((resolve,reject)=>{const request=db.transaction(name).objectStore(name).getAll();
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
    return {messages:await read('messages'),appState:await read('app_state')};
  } finally {db.close();}
}"""


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "fingerprinted browser integration tier")
class SnowFeatureIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="snow-feature-integration-")
        cls.bundle = Path(cls.temporary.name) / "App"
        for relative in ("public_frontend", "frontend/shared", "frontend/assets/immersive"):
            shutil.copytree(APP_ROOT / relative, cls.bundle / relative)
        cls.asset_map = fingerprint(cls.bundle)["assets"]
        # The existing host builds its brand-image URL dynamically; code cannot fall back.
        cls.canonical_code = {url for url in cls.asset_map if Path(url).suffix in {".js", ".css"}}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dialogue.DialogueHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        ui.SnowAdvFeaturesUITests.setUp(self)
        self.blocked = []
        self.served = set()

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [])
            self.assertEqual(self.blocked, [])
        finally:
            ui.SnowAdvFeaturesUITests.tearDown(self)

    open = ui.SnowAdvFeaturesUITests.open
    send = ui.SnowAdvFeaturesUITests.send
    enter_stage = ui.SnowAdvFeaturesUITests.enter_stage
    open_transcript = ui.SnowAdvFeaturesUITests.open_transcript
    candidates = ui.SnowAdvFeaturesUITests.candidates

    def page(self, *, enabled=True):
        page = ui.SnowAdvFeaturesUITests.page(self, width=1440, height=900, enabled=enabled)

        def serve(route):
            request = route.request
            path = urlsplit(request.url).path
            if not request.url.startswith(self.base_url + "/"):
                self.blocked.append("external_request")
                route.abort()
                return
            if path.startswith("/public/"):
                allowed = {"/public/v1/byok/session", "/public/v1/byok/models",
                           "/public/v2/scene/action", "/public/v2/chat/stream"}
                if request.method != "GET" and (request.method != "POST" or path not in allowed):
                    self.blocked.append("unexpected_business_request")
                    route.abort()
                else:
                    route.fallback()
                return
            if path.startswith("/media/") or path == "/announcements.json":
                route.fallback()
                return
            if request.method != "GET" or path in self.canonical_code:
                self.blocked.append("canonical_asset_fallback:" + path)
                route.fulfill(status=404, body="canonical asset fallback forbidden")
                return
            if path.startswith("/shared/") or path.startswith("/assets/immersive/"):
                target = self.bundle / "frontend" / path.lstrip("/")
            else:
                target = self.bundle / "public_frontend" / path.lstrip("/")
                if path.endswith("/"):
                    target /= "index.html"
            if not target.is_file() or not target.resolve().is_relative_to(self.bundle.resolve()):
                self.blocked.append("missing_bundle_asset:" + path)
                route.fulfill(status=404, body="missing fingerprinted bundle asset")
                return
            payload = target.read_bytes()
            match = re.search(r"\.([0-9a-f]{16})\.[^/]+$", path)
            if match and path in self.asset_map.values():
                self.assertEqual(hashlib.sha256(payload).hexdigest()[:16], match.group(1))
            self.served.add(path)
            route.fulfill(content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=payload)

        page.route("**/*", serve)
        return page

    def test_fingerprinted_memory_favorite_search_card_and_usage_chain(self):
        page = self.open(self.page())
        self.assertIn(self.asset_map["/app.js"], self.served)
        self.assertNotIn(self.asset_map["/modules/snow-feature-card.js"], self.served)
        self.send(page, "今天有南瓜汤吗")
        self.send(page, "合成第二句")
        self.enter_stage(page)
        page.locator("#stage-menu > summary").click()
        page.locator("#stage-open-memory").click()
        page.locator(".snow-memory-item").first.wait_for()
        before = page.evaluate(READ_STORES)
        receipts = {item["memory"]["memory_id"]: item
                    for record in before["appState"] for item in record.get("memories", [])}
        page.locator(".snow-memory-item button.snow-round").first.click()
        page.locator(".snow-memory-confirm .danger-button").click()
        page.locator(".snow-memory-removed").wait_for()
        after = page.evaluate(READ_STORES)
        deleted_id = next(record for record in after["appState"] if record["key"] == "ui_preferences")["deletedMemoryIds"][0]
        self.assertIn(deleted_id, receipts)
        self.assertNotIn(deleted_id, {item["memory"]["memory_id"]
                                    for record in after["appState"] for item in record.get("memories", [])})
        page.locator(".snow-memory-removed button").click()
        page.locator(".snow-memory-removed").wait_for(state="detached")
        restored = page.evaluate(READ_STORES)
        self.assertEqual(next(item for record in restored["appState"] for item in record.get("memories", [])
                              if item["memory"]["memory_id"] == deleted_id), receipts[deleted_id])
        page.locator(".snow-memory-item button.snow-round").first.click()
        page.locator(".snow-memory-confirm .danger-button").click()
        page.locator(".snow-memory-removed").wait_for()
        page.keyboard.press("Escape")
        page.locator("#snow-memory-dialog").wait_for(state="hidden")
        self.send(page, "合成删除后的对话")
        deleted = next(record for record in page.evaluate(READ_STORES)["appState"]
                       if record["key"] == "ui_preferences")["deletedMemoryIds"]
        self.assertTrue(deleted)
        self.assertTrue(set(deleted).isdisjoint(self.candidates()))
        page.locator("#snow-stage-usage").wait_for(state="visible")
        self.assertEqual(page.locator("#snow-stage-usage .snow-usage-long").inner_text(), "本轮 1 次调用 · 10 tokens")
        page.locator("#snow-stage-usage").click()
        page.locator(".snow-pop").wait_for()
        self.assertIn("以厂商账单为准", page.locator(".snow-pop").inner_text())
        page.keyboard.press("Escape")
        self.open_transcript(page)
        star = page.locator("#transcript-content .snow-entry-fav").first
        star.wait_for()
        star.click()
        page.wait_for_function("document.querySelector('#transcript-content .snow-entry-fav').getAttribute('aria-pressed')==='true'")
        favorites = next(record for record in page.evaluate(READ_STORES)["appState"]
                         if record["key"] == "ui_preferences")["favoriteLines"]
        self.assertEqual(len(favorites), 1)
        self.assertEqual(favorites[0]["text"], "合成的有效回复。")
        page.locator("#snow-backlog-search").fill("南瓜汤")
        page.locator(".snow-search-hit").first.wait_for()
        self.assertEqual(page.locator(".snow-search-hit mark.snow-mark").all_inner_texts(), ["南瓜汤"])
        page.locator('[data-snow-backlog-tab="favorites"]').click()
        page.locator(".snow-fav-card button", has_text="生成图片卡").click()
        page.wait_for_function("document.querySelector('#snow-card-dialog img')?.src.startsWith('data:image/png')")
        self.assertEqual(page.locator("#snow-card-dialog img").evaluate("async img=>{await img.decode();return [img.naturalWidth,img.naturalHeight];}"), [1080, 1350])
        with page.expect_download() as download:
            page.locator("#snow-card-dialog .primary-button").click()
        self.assertEqual(Path(download.value.path()).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        for url in FEATURE_URLS:
            self.assertIn(self.asset_map[url], self.served, url)
        self.assertTrue(page.locator("#snow-stage-usage svg").count())
        self.assertEqual([path for path in self.requested if path in self.canonical_code], [])

    def test_flag_off_does_not_fetch_feature_assets_from_fingerprinted_app(self):
        self.open(self.page(enabled=False), enabled=False)
        self.assertIn(self.asset_map["/app.js"], self.served)
        self.assertTrue(set(self.asset_map[url] for url in FEATURE_URLS).isdisjoint(self.served))

    def test_hashed_message_reader_copies_and_discards_history_replacement(self):
        page = self.open(self.page())
        result = page.evaluate("""async url=>{
          const {createFeatureLogic}=await import(url);
          const state={historyEpoch:0,writerOwned:false},listeners=new Map();
          let original={id:'synthetic-reader',status:'sent',contentBlocks:[{type:'speech',text:'合成私有台词'}]};
          const emit=type=>{for(const listener of listeners.get(type)||[])listener();};
          const adapter={snapshot:()=>({...state}),preferences:()=>({}),savePreferences:async()=>{},
            loadDialogue:async()=>({}),saveDialogue:async()=>{},getMessage:async id=>id===original?.id?original:null,
            messages:async()=>[],allMessages:async()=>[],on(type,listener){
              if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(listener);
              return()=>listeners.get(type)?.delete(listener);}};
          const logic=createFeatureLogic(adapter);await logic.ready;
          const first=await logic.messages.get(original.id);first.contentBlocks[0].text='mutated';
          const copied=(await logic.messages.get(original.id)).contentBlocks[0].text;
          const missing=await logic.messages.get('missing'),invalid=await logic.messages.get(12);
          const cancelled=[];
          for(const mode of ['clear','import','generation','dispose']){
            let release,entered;const started=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>release=resolve);
            adapter.getMessage=async()=>{entered();await gate;return original;};
            const read=logic.messages.get(original.id);await started;
            if(mode==='dispose')logic.dispose();
            else {if(mode!=='generation')state.historyEpoch++;emit('history-changed');}
            release();cancelled.push(await read);
          }
          return {copied,missing,invalid,cancelled};
        }""", self.asset_map["/modules/snow-feature-logic.js"])
        self.assertEqual(result, {"copied": "合成私有台词", "missing": None, "invalid": None,
                                  "cancelled": [None, None, None, None]})
