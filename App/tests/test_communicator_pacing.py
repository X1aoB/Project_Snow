"""Local synthetic HTTP + real browser regressions for whole-bubble delivery."""
from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as fixture


FIRST = "雪" * 39 + "。"
SECOND = "第二条完整气泡。"


class PacingHandler(fixture.PublicFrontendHandler):
    second = threading.Event()
    terminal = threading.Event()
    requests = []
    disconnect_first = False

    def do_POST(self):  # noqa: N802
        if self.path != "/public/v1/chat/stream":
            return super().do_POST()
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).requests.append((payload["request_id"], self.headers.get("X-Snow-Stream-Version")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def packet(event, value):
            self.wfile.write((f"event: {event}\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        blocks = [{"type": "message", "text": FIRST}, {"type": "message", "text": SECOND}]
        try:
            packet("progress", {"phase": "generating"})
            packet("delta", {"block_type": "message", "block_index": 0, "text": "未经确认的输出不可显示"})
            if self.headers.get("X-Snow-Stream-Version") == "2":
                packet("committed_block", {"request_id": payload["request_id"], "character_id": payload["character_id"],
                       "sequence": 0, "block_id": "first", "block": blocks[0]})
            if type(self).disconnect_first and len(type(self).requests) == 1:
                return
            type(self).second.wait(timeout=30)
            if self.headers.get("X-Snow-Stream-Version") == "2":
                packet("committed_block", {"request_id": payload["request_id"], "character_id": payload["character_id"],
                       "sequence": 1, "block_id": "second", "block": blocks[1]})
            type(self).terminal.wait(timeout=30)
            packet("done", {"communication_channel": "text", "content_blocks": blocks, "truncated": False})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class CommunicatorPacingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PacingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        PacingHandler.second = threading.Event()
        PacingHandler.terminal = threading.Event()
        PacingHandler.requests = []
        PacingHandler.disconnect_first = False

    def tearDown(self):
        PacingHandler.second.set(); PacingHandler.terminal.set()

    def open(self, page, verified=True):
        page.set_default_timeout(8000)
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))

        def configure(route):
            value = route.fetch().json()
            value["features"] = {"chat_latency_v2": True, "verified_block_replay": verified, "motion_v2": True}
            route.fulfill(json=value)

        page.route("**/public/v1/config", configure)
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()

    def send(self, page):
        page.locator("#message-input").fill("本地合成节奏测试")
        page.evaluate("window.__pacingSentAt=performance.now()")
        page.locator("#send-message").click()

    def wait_stored(self, page, count, status):
        return page.evaluate("""async expected => {
          const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
          const deadline=performance.now()+2500;
          try {
            while(performance.now()<deadline){
              const rows=await new Promise(resolve=>{const r=db.transaction('messages').objectStore('messages').getAll();r.onsuccess=()=>resolve(r.result)});
              const row=rows.find(item=>item.role==='assistant'&&item.contentBlocks.length===expected.count&&item.status===expected.status);
              if(row)return {content:row.content,blocks:row.contentBlocks,committed:row.committedBlocks,status:row.status};
              await new Promise(resolve=>setTimeout(resolve,20));
            }
            throw new Error('confirmed reply was not persisted before presentation');
          } finally {db.close();}
        }""", {"count": count, "status": status})

    def test_ordinary_and_replay_save_before_wait_and_deliver_whole_bubbles_at_chosen_pace(self):
        PacingHandler.second.set(); PacingHandler.terminal.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                for verified in (False, True):
                    with self.subTest(verified=verified):
                        page = browser.new_page()
                        self.open(page, verified=verified)
                        self.send(page)
                        stored = self.wait_stored(page, 2, "sent")
                        self.assertEqual([row["text"] for row in stored["blocks"]], [FIRST, SECOND])
                        self.assertEqual(len(stored["committed"]), 2 if verified else 0)
                        self.assertNotIn(FIRST, page.locator("#timeline").inner_text())
                        self.assertFalse(page.locator("#send-message").is_disabled())
                        page.locator("#timeline .typing-indicator").wait_for(state="visible")
                        page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=13000)
                        elapsed = page.evaluate("performance.now()-window.__pacingSentAt")
                        self.assertGreaterEqual(elapsed, 8400)
                        self.assertLess(elapsed, 12500)
                        self.assertNotIn(SECOND, page.locator("#timeline").inner_text())
                        self.assertNotIn("未经确认", page.locator("#timeline").inner_text())
                        page.locator("#fast-forward-reply").click()
                        page.locator("#timeline").get_by_text(SECOND, exact=True).wait_for(timeout=1000)
                        self.assertEqual(self.errors, [])
                        page.close()
            finally:
                browser.close()

    def test_verified_prefix_is_saved_before_delivery_and_reload_does_not_replay(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                stored = self.wait_stored(page, 1, "pending")
                self.assertEqual(stored["committed"][0]["block"]["text"], FIRST)
                self.assertNotIn(FIRST, page.locator("#timeline").inner_text())
                page.reload(wait_until="domcontentloaded")
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=2500)
                self.assertEqual(len(PacingHandler.requests), 1)
                self.assertNotIn(SECOND, page.locator("#timeline").inner_text())
                self.assertEqual(self.errors, [])
            finally:
                PacingHandler.second.set(); PacingHandler.terminal.set(); browser.close()

    def test_transport_recovery_flushes_verified_content_without_replaying_delivery(self):
        PacingHandler.disconnect_first = True
        PacingHandler.second.set(); PacingHandler.terminal.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=2500)
                page.locator("#timeline").get_by_text(SECOND, exact=True).wait_for(timeout=2500)
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(len(PacingHandler.requests), 2)
                self.assertEqual(PacingHandler.requests[0], PacingHandler.requests[1])
                self.assertEqual(page.locator("#timeline").get_by_text(FIRST, exact=True).count(), 1)
                self.assertEqual(self.errors, [])
            finally:
                browser.close()

    def test_switching_contacts_keeps_received_prefix_visible_before_new_blocks_arrive(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                self.wait_stored(page, 1, "pending")
                self.assertNotIn(FIRST, page.locator("#timeline").inner_text())
                first = page.locator('#character-list [data-character][aria-current="true"]').get_attribute("data-character")
                second = page.locator("#character-list [data-character]").evaluate_all("nodes=>nodes.find(node=>node.getAttribute('aria-current')!=='true').dataset.character")
                for character in (second, first):
                    if page.locator("#open-contacts").is_visible():
                        page.locator("#open-contacts").click()
                    page.locator(f'#character-list [data-character="{character}"]').click()
                    page.wait_for_function("id=>document.querySelector('#character-list [data-character][aria-current=true]')?.dataset.character===id", arg=character)
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=1500)
                self.assertTrue(page.locator("#send-message").is_disabled())
                self.assertNotIn(SECOND, page.locator("#timeline").inner_text())
                self.assertEqual(len(PacingHandler.requests), 1)
                self.assertEqual(self.errors, [])
            finally:
                PacingHandler.second.set(); PacingHandler.terminal.set(); browser.close()

    def test_fast_forward_only_releases_confirmed_prefix_and_does_not_complete_the_request(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                self.wait_stored(page, 1, "pending")
                page.locator("#fast-forward-reply").click()
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=1000)
                self.assertTrue(page.locator("#send-message").is_disabled())
                self.assertNotIn(SECOND, page.locator("#timeline").inner_text())
                self.assertNotIn("未经确认", page.locator("#timeline").inner_text())
                PacingHandler.second.set()
                self.wait_stored(page, 2, "pending")
                page.locator("#timeline").get_by_text(SECOND, exact=True).wait_for(timeout=1000)
                self.assertTrue(page.locator("#send-message").is_disabled())
                PacingHandler.terminal.set()
                self.wait_stored(page, 2, "sent")
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(len(PacingHandler.requests), 1)
                self.assertEqual(self.errors, [])
            finally:
                PacingHandler.second.set(); PacingHandler.terminal.set(); browser.close()

    def test_hidden_page_finishes_received_reply_without_replaying_on_return(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                self.wait_stored(page, 1, "pending")
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
                PacingHandler.second.set(); PacingHandler.terminal.set()
                self.wait_stored(page, 2, "sent")
                page.evaluate("delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))")
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=1000)
                page.locator("#timeline").get_by_text(SECOND, exact=True).wait_for(timeout=1000)
                self.assertEqual(len(PacingHandler.requests), 1)
                self.assertEqual(self.errors, [])
            finally:
                PacingHandler.second.set(); PacingHandler.terminal.set(); browser.close()

    def test_new_request_flushes_saved_previous_delivery_and_reduced_motion_skips_waits(self):
        PacingHandler.second.set(); PacingHandler.terminal.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page); self.send(page)
                self.wait_stored(page, 2, "sent")
                self.assertNotIn(FIRST, page.locator("#timeline").inner_text())
                self.send(page)
                page.locator("#timeline").get_by_text(FIRST, exact=True).wait_for(timeout=1500)
                page.locator("#timeline").get_by_text(SECOND, exact=True).wait_for(timeout=1500)
                page.emulate_media(reduced_motion="reduce")
                page.wait_for_function("document.querySelectorAll('#timeline .message.assistant').length===2")
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(len(PacingHandler.requests), 2)
                self.assertEqual(self.errors, [])
            finally:
                browser.close()
