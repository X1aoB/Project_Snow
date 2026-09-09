from __future__ import annotations

import json
import base64
import os
import hashlib
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from urllib.parse import urlparse

from tests import test_public_frontend_e2e as frontend_fixture
from tests.test_public_frontend_e2e import (
    PublicFrontendHandler,
    _launch_browser,
    sync_playwright,
)


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class PublicFrontendReliabilityTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        frontend_fixture.PublicFrontendE2ETests.setUp(self)

    def open(self, page):
        page.goto(self.base_url, wait_until="networkidle")
        if page.locator("#accept-experience-notice").is_visible():
            page.locator("#accept-experience-notice").click()
        page.wait_for_function("document.querySelector('#connection-status').textContent === '服务已连接'")

    @staticmethod
    def seed(page, count=61, old_count=0):
        page.evaluate("""async ({count, oldCount}) => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open('project-snow-public', 4);
            request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
          });
          await new Promise((resolve, reject) => {
            const tx = db.transaction(['threads', 'messages'], 'readwrite');
            const threads = tx.objectStore('threads'); const messages = tx.objectStore('messages');
            messages.clear();
            const request = threads.get('25b23cb64398');
            request.onsuccess = () => {
              const thread = {...request.result, characterId:'25b23cb64398', messageCount:count};
              threads.put(thread);
              const timestamp = Date.now() - 1000;
              for (let i=0; i<count; i++) messages.put({
                id:`fixture-${String(i).padStart(6, '0')}`, characterId:thread.characterId,
                role:'user', content:`历史消息 ${i}`, contentBlocks:[{type:'message',text:`历史消息 ${i}`}],
                communicationChannel:'text', status:'sent', conversationSegmentId:thread.conversationSegmentId,
                createdAt:i<oldCount ? timestamp-60*86400000 : timestamp,
              });
            };
            tx.oncomplete=resolve; tx.onerror=()=>reject(tx.error);
          }); db.close();
        }""", {"count": count, "oldCount": old_count})

    @staticmethod
    def stored_count(page):
        return page.evaluate("""async () => {
          const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
          const count=await new Promise(resolve=>{const r=db.transaction('messages').objectStore('messages').count();r.onsuccess=()=>resolve(r.result)});
          db.close(); return count;
        }""")

    @staticmethod
    def read_record(page, store, key):
        return page.evaluate("""async ({store,key}) => {
          const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
          const value=await new Promise(resolve=>{const r=db.transaction(store).objectStore(store).get(key);r.onsuccess=()=>resolve(r.result || null)});
          db.close();return value;
        }""", {"store":store,"key":key})

    @staticmethod
    def wait_saved_draft(page, value):
        page.evaluate("""async expected => {
          const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
          try {
            for(let attempt=0;attempt<50;attempt++) {
              const record=await new Promise(resolve=>{const r=db.transaction('app_state').objectStore('app_state').get('drafts');r.onsuccess=()=>resolve(r.result)});
              if(record?.values['25b23cb64398:text:message']===expected) return;
              await new Promise(resolve=>setTimeout(resolve,100));
            }
            throw new Error('draft did not persist');
          } finally { db.close(); }
        }""", value)

    def test_all_web_storage_blocked_still_accepts_notice_and_configures_chat(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""for (const method of ['getItem','setItem','removeItem'])
              Storage.prototype[method] = () => { throw new DOMException('blocked','SecurityError'); };
              Object.defineProperty(window,'indexedDB',{get(){throw new DOMException('blocked','SecurityError')}});
            """)
            self.open(page)
            frontend_fixture.PublicFrontendE2ETests._configure_model(page)
            page.locator("#message-input").fill("本次会话仍然可用")
            page.locator("#send-message").click()
            page.locator("#timeline .message.assistant").wait_for()
            self.assertEqual(errors, [])
            browser.close()

    def test_equal_timestamp_pagination_has_no_missing_messages(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page)
            self.open(page)
            self.assertEqual(page.locator("#timeline .message").count(), 60)
            page.locator("#load-older-messages").click()
            page.wait_for_function("document.querySelectorAll('#timeline .message').length === 61")
            self.assertFalse(page.locator("#load-older-messages").count())
            browser.close()

    def test_retention_does_not_resurrect_cached_messages(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page, count=2, old_count=1)
            self.open(page)
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            page.locator("#history-retention").select_option("30")
            page.wait_for_function("document.querySelectorAll('#timeline .message').length === 1")
            page.locator('[data-close-dialog="settings-dialog"]').click()
            page.locator('[data-character="9f5804761c56"]').click()
            page.locator('[data-character="25b23cb64398"]').click()
            self.assertEqual(self.stored_count(page), 1)
            self.open(page)
            self.assertEqual(self.stored_count(page), 1)
            browser.close()

    def test_cross_breakpoint_updates_contact_focus_and_interactivity(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width":390,"height":844})
            self.open(page)
            self.assertTrue(page.locator("#contact-panel").evaluate("element=>element.inert"))
            page.set_viewport_size({"width":1280,"height":800})
            page.wait_for_function("!document.querySelector('#contact-panel').inert")
            page.locator('[data-character="9f5804761c56"]').click()
            page.set_viewport_size({"width":390,"height":844})
            page.wait_for_function("document.querySelector('#contact-panel').inert")
            page.locator("#open-contacts").click()
            self.assertFalse(page.locator("#contact-panel").evaluate("element=>element.inert"))
            browser.close()

    def test_second_tab_requires_handoff_and_preserves_draft(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context()
            first = context.new_page()
            self.open(first)
            first.locator("#message-input").fill("尚未发送的草稿")
            second = context.new_page()
            self.open(second)
            self.assertTrue(second.locator("#tab-session-banner").is_visible())
            self.assertTrue(second.locator("#send-message").is_disabled())
            second.locator("#take-over-session").click()
            second.wait_for_function("document.querySelector('#tab-session-banner').hidden")
            second.wait_for_function("document.querySelector('#message-input').value === '尚未发送的草稿'")
            self.assertTrue(first.locator("#send-message").is_disabled())
            browser.close()

    def test_ten_thousand_messages_boot_is_bounded_and_backup_is_portable(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page, count=10000)
            self.open(page)
            self.assertEqual(page.locator("#timeline .message").count(), 60)
            self.assertEqual(self.stored_count(page), 10000)
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            with page.expect_download() as download_info:
                page.locator("#export-history").click()
            payload = json.loads(download_info.value.path().read_text(encoding="utf-8"))
            self.assertEqual(payload["databaseVersion"], 4)
            self.assertEqual(len(payload["messages"]), 10000)
            self.assertNotIn("writer_lease", json.dumps(payload))
            self.assertNotIn("credential", json.dumps(payload))
            browser.close()

    def test_background_summary_is_cancelled_by_new_message_and_late_result_is_ignored(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            page.add_init_script("""const originalFetch = window.fetch;
              window.__summaryStarted = 0; window.__summaryAborted = 0;
              window.fetch = (url, options) => {
                if (String(url).endsWith('/chat/summarize')) {
                  window.__summaryStarted += 1;
                  options.signal.addEventListener('abort',()=>window.__summaryAborted++);
                  return new Promise(resolve=>window.__finishSummary=()=>resolve(new Response(JSON.stringify({summary:'迟到摘要不能写入'}))));
                }
                return originalFetch(url, options);
              };""")
            self.open(page)
            frontend_fixture.PublicFrontendE2ETests._configure_model(page)
            self.seed(page, count=12)
            page.evaluate("""async () => {
              const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
              await new Promise(resolve=>{const tx=db.transaction('messages','readwrite'); const r=tx.objectStore('messages').openCursor();
                r.onsuccess=()=>{const c=r.result;if(c){c.update({...c.value,role:'assistant'});c.continue()}};tx.oncomplete=resolve});db.close();
            }""")
            self.open(page)
            calls = []

            def stream(route):
                calls.append(1)
                result = {"communication_channel":"text", "content_blocks":[{"type":"message","text":f"测试回复 {len(calls)}"}],"usage":{"provider_calls":1 if len(calls)==1 else 2}}
                route.fulfill(status=200,content_type="text/event-stream",body="event: done\ndata: "+json.dumps(result,ensure_ascii=False)+"\n\n")

            page.route("**/public/v1/chat/stream", stream)
            page.locator("#message-input").fill("开始新的总结")
            page.locator("#send-message").click()
            page.wait_for_function("window.__summaryStarted === 1")
            self.assertTrue(page.locator("#send-message").is_enabled())
            page.locator("#message-input").fill("用户输入应优先")
            page.locator("#send-message").click()
            page.wait_for_function("window.__summaryAborted === 1")
            page.locator("#timeline").get_by_text("测试回复 2").wait_for()
            page.evaluate("window.__finishSummary()")
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            self.assertEqual(page.locator("#summary-last-updated").inner_text(), "尚未生成摘要")
            browser.close()

    def test_build_update_saves_draft_before_reload(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            revision = ["first"]
            seen = []

            def build_info(route):
                seen.append(revision[0])
                route.fulfill(status=200,content_type="application/json",body=json.dumps({"app_version":"e2e","revision":revision[0],"api_schema":"public-v1","state_schema":"public-state-2"}))

            page.route("**/public/v1/build-info", build_info)
            self.open(page)
            page.wait_for_load_state("networkidle")
            self.assertIn("first", seen)
            page.locator("#message-input").fill("版本更新仍应保留我的草稿")
            revision[0] = "second"
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.locator("#build-update-banner").wait_for()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("#apply-build-update").click()
            page.wait_for_function("document.querySelector('#connection-status').textContent === '服务已连接'")
            self.assertEqual(page.locator("#message-input").input_value(), "版本更新仍应保留我的草稿")
            browser.close()

    def test_import_transaction_abort_preserves_all_original_stores(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page, count=2)
            self.open(page)
            page.locator("#message-input").fill("不能被失败导入替换")
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            page.evaluate("""() => { const originalPut = IDBObjectStore.prototype.put;
              IDBObjectStore.prototype.put = function(value, ...args) {
                const result=originalPut.call(this,value,...args);
                if(this.name==='messages' && value.id==='import-fail') { const tx=this.transaction;queueMicrotask(()=>tx.abort()); }
                return result;
              }; }""")
            backup = {"schema":"project-snow-history-1","databaseVersion":4,"exportedAt":"2026-09-07", "threads":[{"characterId":"import-only"}],"messages":[{"id":"import-fail","characterId":"import-only","createdAt":1,"role":"user","content":"cannot partially commit"}],"appState":[{"key":"drafts","values":{"25b23cb64398:text:message":"wrong draft"}}]}
            page.locator("#history-import-file").set_input_files({"name":"backup.json","mimeType":"application/json","buffer":json.dumps(backup).encode()})
            page.locator("#confirm-history-import").click()
            page.wait_for_function("document.querySelector('#history-import-error').textContent.length > 0")
            self.assertEqual(self.stored_count(page), 2)
            exists = page.evaluate("""async()=>{const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});const value=await new Promise(resolve=>{const r=db.transaction('threads').objectStore('threads').get('import-only');r.onsuccess=()=>resolve(Boolean(r.result))});db.close();return value}""")
            self.assertFalse(exists)
            page.locator('[data-close-dialog="history-import-dialog"]').click()
            page.locator('[data-close-dialog="settings-dialog"]').click()
            self.assertEqual(page.locator("#message-input").input_value(), "不能被失败导入替换")
            browser.close()

    def test_import_merge_is_idempotent_preserves_current_data_and_never_replays_imported_requests(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page, count=2)
            self.open(page)
            page.locator("#message-input").fill("当前草稿优先")
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            thread = self.read_record(page, "threads", "25b23cb64398")
            original = self.read_record(page, "messages", "fixture-000000")
            incoming = {**original,"id":"imported-pending","status":"pending","requestId":"import-request","requestSnapshot":{"request_id":"import-request","state_package":"subject-bound"}}
            backup = {"schema":"project-snow-history-1","databaseVersion":4,"exportedAt":"2026-09-07","threads":[thread],"messages":[{**original,"content":"must not replace"},incoming],"appState":[{"key":"drafts","values":{"25b23cb64398:text:message":"imported draft"}}]}
            for _ in range(2):
                page.locator("#history-import-file").set_input_files({"name":"backup.json","mimeType":"application/json","buffer":json.dumps(backup).encode()})
                page.locator("#confirm-history-import").click()
                page.wait_for_function("!document.querySelector('#history-import-dialog').open")
            self.assertEqual(self.stored_count(page), 3)
            self.assertEqual(self.read_record(page,"messages","fixture-000000")["content"],original["content"])
            imported = self.read_record(page,"messages","imported-pending")
            self.assertEqual(imported["status"],"failed")
            self.assertIsNone(imported["requestSnapshot"])
            page.locator('[data-close-dialog="settings-dialog"]').click()
            self.assertEqual(page.locator("#message-input").input_value(),"当前草稿优先")
            self.assertEqual(PublicFrontendHandler.chat_payloads,[])
            browser.close()

    def test_history_windows_are_bounded_and_unchanged_message_nodes_survive_render(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self.open(page)
            self.seed(page,count=350)
            self.open(page)
            page.evaluate("window.__keptMessage = document.querySelector('[data-message-id=\"fixture-000348\"]')")
            for _ in range(3):
                page.locator("#load-older-messages").click()
            self.assertTrue(page.evaluate("window.__keptMessage === document.querySelector('[data-message-id=\"fixture-000348\"]')"))
            for _ in range(5):
                page.locator("#load-older-messages").click()
                self.assertLessEqual(page.locator("#timeline .message").count(),200)
            page.locator('[data-message-id="fixture-000000"]').wait_for()
            page.locator("#new-replies").click()
            self.assertTrue(page.locator('[data-message-id="fixture-000349"]').count())
            self.assertEqual(page.locator("#transcript-content .transcript-entry").count(),0)
            browser.close()

    def test_settings_tabs_and_mobile_contacts_support_keyboard_navigation(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width":390,"height":844})
            self.open(page)
            page.locator("#open-contacts").click()
            page.locator("#close-contacts").focus()
            page.keyboard.press("Shift+Tab")
            self.assertTrue(page.evaluate("document.querySelector('#contact-panel').contains(document.activeElement)"))
            page.keyboard.press("Escape")
            self.assertTrue(page.locator("#open-contacts").evaluate("node=>node===document.activeElement"))
            page.locator("#open-contacts").click()
            page.locator("#open-settings").click()
            page.locator("#settings-tab-models").focus()
            page.keyboard.press("ArrowRight")
            self.assertEqual(page.locator("#settings-tab-history").get_attribute("aria-selected"),"true")
            self.assertEqual(page.locator('[role="tablist"][aria-label="设置分类"] [tabindex="0"]').count(),1)
            page.keyboard.press("End")
            self.assertTrue(page.locator("#settings-panel-about").is_visible())
            browser.close()

    def test_supported_096_patch_old_new_old_preserves_v4_drafts_and_pending_snapshot(self):
        root = frontend_fixture.APP_ROOT.parent
        manifest = json.loads((root/"App/compat/public-0.9.6.manifest.json").read_text(encoding="utf-8"))
        patch = root/"App/compat"/manifest["patch_file"]
        self.assertEqual(hashlib.sha256(patch.read_bytes()).hexdigest(),manifest["patch_sha256"])
        with TemporaryDirectory() as directory, sync_playwright() as playwright:
            compatible = Path(directory)
            original = {}
            for item in manifest["files"]:
                if item["baseline_sha256"] is None:
                    continue
                result = subprocess.run(["git","show",f'{manifest["baseline_commit"]}:{item["path"]}'],cwd=root,capture_output=True)
                self.assertEqual(result.returncode,0,"Compatibility tier requires the fixed baseline commit; use fetch-depth: 0")
                self.assertEqual(hashlib.sha256(result.stdout).hexdigest(),item["baseline_sha256"])
                target = compatible/item["path"]
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(result.stdout)
                original[item["path"]] = result.stdout
            subprocess.run(["git","-c","core.autocrlf=false","apply","--check",str(patch)],cwd=compatible,check=True,capture_output=True)
            subprocess.run(["git","-c","core.autocrlf=false","apply",str(patch)],cwd=compatible,check=True,capture_output=True)
            for item in manifest["files"]:
                self.assertEqual(hashlib.sha256((compatible/item["path"]).read_bytes()).hexdigest(),item["compatible_sha256"])
            phase = ["compatible"]
            browser = _launch_browser(playwright)
            page = browser.new_page()

            def frontend_version(route):
                path = urlparse(route.request.url).path
                relative = "App/public_frontend/"+("index.html" if path=="/" else path.lstrip("/"))
                target = compatible/relative
                if phase[0] != "current" and path in ["/","/app.js","/app.css","/modules/runtime.js"] and target.is_file():
                    body = original.get(relative,target.read_bytes()) if phase[0]=="original" else target.read_bytes()
                    kind = "text/html" if path=="/" else "text/css" if path.endswith(".css") else "application/javascript"
                    route.fulfill(status=200,content_type=kind,body=body)
                else:
                    route.continue_()

            page.route("**/*",frontend_version)
            self.open(page)
            self.seed(page,count=2)
            snapshot = {"request_id":"de305d54-75b4-431b-adb2-eb6b9e546014","character_id":"25b23cb64398","communication_channel":"text","message":"pending original","state_package":"fixed-subject.signature","local_day_key":"2026-09-07"}
            page.evaluate("""async snapshot => {
              const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
              await new Promise(resolve=>{const tx=db.transaction('messages','readwrite');const store=tx.objectStore('messages');const r=store.get('fixture-000001');
                r.onsuccess=()=>store.put({...r.result,status:'pending',requestId:snapshot.request_id,requestSnapshot:snapshot});tx.oncomplete=resolve});db.close();
            }""",snapshot)
            page.locator("#message-input").fill("兼容旧界面创建的草稿")
            self.wait_saved_draft(page,"兼容旧界面创建的草稿")
            phase[0] = "current"
            self.open(page)
            page.wait_for_function("document.querySelector('#message-input').value === '兼容旧界面创建的草稿'")
            self.assertEqual(self.read_record(page,"messages","fixture-000001")["requestSnapshot"],snapshot)
            page.locator("#message-input").fill("新版草稿应能安全回退")
            self.wait_saved_draft(page,"新版草稿应能安全回退")
            phase[0] = "compatible"
            self.open(page)
            self.assertEqual(page.locator("#message-input").input_value(),"新版草稿应能安全回退")
            stored = self.read_record(page,"messages","fixture-000001")
            self.assertEqual(stored["requestSnapshot"],snapshot)
            self.assertEqual(stored["requestId"],snapshot["request_id"])
            self.assertEqual(self.stored_count(page),2)
            self.assertEqual(PublicFrontendHandler.chat_payloads,[])
            # The untouched baseline has a known boot-time overwrite defect;
            # the journal makes returning to the current version recoverable.
            phase[0] = "original"
            self.open(page)
            self.assertEqual(page.locator("#message-input").input_value(),"")
            phase[0] = "current"
            self.open(page)
            self.assertEqual(page.locator("#message-input").input_value(),"新版草稿应能安全回退")
            browser.close()

    def test_enabled_stage_manifest_uses_verified_generic_art_and_rejects_tampering(self):
        image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII=")
        ids = ["25b23cb64398","9f5804761c56","702f4375675b","78aa7ab99154"] + [f"{number:012x}" for number in range(18)]
        asset = {"url":"/assets/stage/fixture.png","sha256":hashlib.sha256(image_bytes).hexdigest(),"media_type":"image/png","source":{"kind":"fixture","reference":"synthetic pixel"},"approval":{"status":"approved","approved_by":"fixture","approved_at":"2026-09-07T00:00:00Z","evidence":"synthetic test; not a real release approval"}}
        payload = json.dumps({"schema":"project-snow-stage-1","version":"fixture-22","characters":[{"character_id":identifier,"states":{"neutral":asset},"motions":["none"]} for identifier in ids]}).encode()
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            tampered = [False]

            def config(route):
                value = route.fetch().json()
                value["stage_release"] = {"enabled":True,"manifest_url":"/assets/stage/fixture.json","sha256":hashlib.sha256(payload).hexdigest()}
                route.fulfill(status=200,content_type="application/json",body=json.dumps(value))

            def characters(route):
                value = route.fetch().json()
                value["characters"] += [{"character_id":identifier,"display_name":f"Synthetic {index}","avatar":None} for index,identifier in enumerate(ids[4:])]
                value["count"] = 22
                route.fulfill(status=200,content_type="application/json",body=json.dumps(value))

            page.route("**/public/v1/config",config)
            page.route("**/public/v1/characters",characters)
            page.route("**/assets/stage/fixture.json",lambda route:route.fulfill(status=200,content_type="application/json",body=payload))
            page.route("**/assets/stage/fixture.png",lambda route:route.fulfill(status=200,content_type="image/png",body=b"tampered" if tampered[0] else image_bytes))
            self.open(page)
            page.wait_for_load_state("networkidle")
            result = page.evaluate("""async()=>{
              const art=document.querySelector('#stage-character-art');
              for(let attempt=0;attempt<40;attempt++) {
                const ready=await window.__projectSnowTest.updateStageCharacterArt(art,{character_id:'25b23cb64398'},'happy');
                if(ready || art.dataset.stageArtFailedKey) return {ready,src:art.getAttribute('src'),state:art.dataset.expressionState};
                await new Promise(resolve=>setTimeout(resolve,50));
              }
              throw new Error('verified stage did not initialize');
            }""")
            self.assertTrue(result["ready"])
            self.assertTrue(result["src"].startswith("data:image/png;base64,"))
            self.assertEqual(result["state"],"neutral")
            advertised = page.evaluate("""async(hash)=>{
              const art=document.querySelector('#stage-character-art');
              const character={character_id:'25b23cb64398', expression_manifest_url:'/media/fixture/expressions/25b23cb64398/manifest.json',expression_manifest_sha256:hash};
              const ready=await window.__projectSnowTest.updateStageCharacterArt(art,character,'happy');
              return {ready,src:art.getAttribute('src'),state:art.dataset.expressionState};
            }""", PublicFrontendHandler._expression_digest("25b23cb64398"))
            self.assertTrue(advertised["ready"])
            self.assertTrue(advertised["src"].startswith("data:image/"))
            self.assertEqual(advertised["state"],"happy")
            tampered[0] = True
            self.open(page)
            page.wait_for_load_state("networkidle")
            rejected = page.evaluate("""async()=>{
              const art=document.querySelector('#stage-character-art');
              for(let attempt=0;attempt<40;attempt++) {
                await window.__projectSnowTest.updateStageCharacterArt(art,{character_id:'25b23cb64398'},'neutral');
                if(art.dataset.stageArtFailedKey) return art.hidden && !art.hasAttribute('src');
                await new Promise(resolve=>setTimeout(resolve,50));
              }
              return false;
            }""")
            self.assertTrue(rejected)
            page.locator('[data-character="702f4375675b"]').click()
            frontend_fixture.PublicFrontendE2ETests._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            page.wait_for_function("document.querySelector('#stage-character-art').dataset.expressionCharacterId === '702f4375675b'")
            bundled = page.evaluate("""async()=>{
              const art=document.querySelector('#stage-character-art');
              const ready=await window.__projectSnowTest.updateStageCharacterArt(art,{character_id:'702f4375675b'},'happy');
              return {ready,src:art.getAttribute('src'),state:art.dataset.expressionState};
            }""")
            self.assertTrue(bundled["ready"])
            self.assertTrue(bundled["src"].startswith("data:image/"))
            self.assertEqual(bundled["state"],"neutral")
            browser.close()

    def test_verified_stage_encoding_is_cancelled_bounded_and_recovers_without_refetch(self):
        image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII=")
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            requests = []

            def image(route):
                requests.append(route.request.url)
                route.fulfill(status=200, content_type="image/png", body=image_bytes)

            page.route("**/encoding-fixture.png", image)
            self.open(page)
            result = page.evaluate("""async hash => {
                const originalReader = window.FileReader, nativeDecode = HTMLImageElement.prototype.decode;
                const later = window.setTimeout, readers = [];
                let aborts = 0, decodes = 0;
                const hooks = window.__projectSnowTest;
                const presentation = {stageAssetPath:'/encoding-fixture.png',stageAssetSha256:hash,renderStrategy:'single_sprite'};
                window.FileReader = class {
                    static LOADING = 1;
                    constructor() { this.readyState = 0; this.result = null; }
                    readAsDataURL() { this.readyState = 1; this.lateLoad = this.onload; readers.push(this); }
                    abort() { this.readyState = 2; aborts++; this.onabort?.(); }
                };
                HTMLImageElement.prototype.decode = function() { decodes++; return nativeDecode.call(this); };
                const outcome = promise => promise.then(()=>'unexpected success',error=>error.name+':'+error.message);
                try {
                    const controller = new AbortController();
                    const pending = outcome(hooks.preloadStagePresentation(presentation, controller.signal));
                    for (let count=0; readers.length<1 && count<100; count++) await new Promise(resolve=>later(resolve,10));
                    if (readers.length !== 1) throw new Error('encoder did not start');
                    controller.abort();
                    const cancelled = await pending;
                    // Simulate an already-queued load event after cancellation.
                    readers[0].result = 'data:image/png;base64,untrusted-late-result';
                    readers[0].lateLoad();
                    window.setTimeout = (callback, ms, ...args) => later(callback, ms===8000 ? 30 : ms, ...args);
                    const timedOut = await outcome(hooks.preloadStagePresentation(presentation));
                    const cleaned = readers.every(reader=>reader.onload===null && reader.onerror===null && reader.onabort===null);
                    const rejectedDecodes = decodes;
                    window.FileReader = originalReader;
                    window.setTimeout = later;
                    const {prepared} = await hooks.preloadStagePresentation(presentation);
                    const loaded = {src:prepared.src.startsWith('data:image/png;base64,'),width:prepared.image.naturalWidth};
                    const verifiedHash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',
                        await (await fetch(prepared.src)).arrayBuffer()))].map(value=>value.toString(16).padStart(2,'0')).join('');
                    prepared.dispose();
                    return {cancelled,timedOut,aborts,cleaned,rejectedDecodes,loaded,verifiedHash,
                        disposed:!prepared.image.hasAttribute('src')};
                } finally {
                    window.FileReader = originalReader;
                    HTMLImageElement.prototype.decode = nativeDecode;
                    window.setTimeout = later;
                }
            }""", image_hash)
            self.assertTrue(result["cancelled"].startswith("AbortError:"))
            self.assertEqual(result["timedOut"], "Error:expression_image_encoding_timeout")
            self.assertEqual(result["aborts"], 2)
            self.assertTrue(result["cleaned"])
            self.assertEqual(result["rejectedDecodes"], 0)
            self.assertEqual(result["loaded"], {"src": True, "width": 1})
            self.assertEqual(result["verifiedHash"], image_hash)
            self.assertTrue(result["disposed"])
            self.assertEqual(len(requests), 1)
            self.assertEqual(PublicFrontendHandler.chat_payloads, [])
            browser.close()
