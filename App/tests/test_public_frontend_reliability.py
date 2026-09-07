from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless

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
