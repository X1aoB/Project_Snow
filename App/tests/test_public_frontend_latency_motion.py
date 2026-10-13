"""Synthetic browser coverage for verified replies and independent presentation."""
from __future__ import annotations

import json
import os
import platform
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as fixture
from tests import test_public_frontend_reliability as reliability_fixture


class LatencyHandler(fixture.PublicFrontendHandler):
    release = threading.Event()
    started = threading.Event()
    requests = []
    scenario = "success"
    transition_calls = 0
    transition_started = threading.Event()
    transition_release = None

    def do_POST(self):  # noqa: N802
        if self.path == "/public/v1/presence/transition":
            cls = type(self)
            cls.transition_calls += 1
            cls.transition_started.set()
            if cls.transition_release is not None:
                cls.transition_release.wait(timeout=5)
        if self.path != "/public/v1/chat/stream":
            return super().do_POST()
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        cls = type(self)
        cls.requests.append((payload["request_id"], self.headers.get("X-Snow-Stream-Version")))
        if cls.scenario == "database_unknown" and len(cls.requests) == 1:
            cls.started.set()
            self._json({"detail": {"code": "public_database_unavailable", "stage": "idempotency_store", "retryable": False}}, status=503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def packet(event, body):
            self.wfile.write((f"event: {event}\ndata: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        block_type = "speech" if payload["communication_channel"] == "in_person" else "message"
        first = {"type": block_type, "text": "第一段已经确认。"}
        if cls.scenario == "typed_measurement":
            first["text"] = "这一整段合成对白需要逐字显示直到完整内容出现在舞台上才可以计入首次有效回复时间。"
        second = {"type": block_type, "text": "第二段完整回复。"}
        try:
            packet("progress", {"phase": "generating"})
            packet("delta", {"block_index": 0, "block_type": block_type, "text": "未经确认的原始输出"})
            if self.headers.get("X-Snow-Stream-Version") == "2":
                packet("committed_block", {"request_id": payload["request_id"], "character_id": payload["character_id"], "sequence": 0, "block_id": "first", "block": first})
            cls.started.set()
            if cls.scenario == "reconnect" and len(cls.requests) == 1:
                return
            cls.release.wait(timeout=10)
            if cls.scenario == "partial":
                packet("error", {"code": "provider_network_error", "partial": True, "retryable": False})
                return
            if self.headers.get("X-Snow-Stream-Version") == "2" and cls.scenario != "conflict":
                packet("committed_block", {"request_id": payload["request_id"], "character_id": payload["character_id"], "sequence": 1, "block_id": "second", "block": second})
            timings = {"queue_ms": 2, "guard_rewrite_ms": 0, "context_build_ms": 11.8,
                       "retrieval_ms": 12, "prompt_build_ms": 13, "hybrid_total": 14,
                       "initial_model_ms": 15, "provider_headers_ms": 16,
                       "provider_first_content_ms": 17, "provider_first_event_ms": 18,
                       "validation_ms": 20, "validation_total_ms": 22,
                       "first_verified_block_ms": 23, "first_committed_block_ms": 25, "total": 26,
                       "empty_recovery_ms": -1, "validation_local_ms": "not-a-duration",
                       "durable_commit_ms": True, "private_provider_id": "private-fixture-provider",
                       "request_id": payload["request_id"], "body": "private-fixture-body"}
            if cls.scenario == "cached_timings":
                # Cached completion has no new verification/commit acknowledgement.
                timings = {"queue_ms": 2, "guard_rewrite_ms": 0, "context_build_ms": 11,
                           "retrieval_ms": 12, "total": 26, "private_provider_id": "private-fixture-provider"}
            packet("done", {"communication_channel": payload["communication_channel"], "content_blocks": [first if cls.scenario != "conflict" else {"type": block_type, "text": "不允许的前缀改写"}, second], "truncated": False,
                            "cached": cls.scenario == "cached_timings", "timings_ms": timings})
        except (BrokenPipeError, ConnectionResetError):
            pass


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class PublicFrontendLatencyMotionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LatencyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        LatencyHandler.release = threading.Event()
        LatencyHandler.started = threading.Event()
        LatencyHandler.requests = []
        LatencyHandler.scenario = "success"
        LatencyHandler.transition_calls = 0
        LatencyHandler.transition_started = threading.Event()
        LatencyHandler.transition_release = None

    def tearDown(self):
        LatencyHandler.release.set()
        if LatencyHandler.transition_release is not None:
            LatencyHandler.transition_release.set()
        fixture.PublicFrontendE2ETests.tearDown(self)

    def open(self, page, verified=True):
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: print(f"browser error: {error}", flush=True))
        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {"chat_latency_v2": True, "verified_block_stream": verified, "motion_v2": True}
            payload["model_profiles"] = {"openai": ["gpt-e2e"]}
            route.fulfill(json=payload)
        page.route("**/public/v1/config", configure)
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        mobile = page.viewport_size["width"] <= 820
        if mobile:
            page.locator("#open-contacts").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if mobile:
            page.locator("#close-contacts").click()

    def snapshot(self, page, name):
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        if page.locator("#toast").is_visible():
            page.locator("#toast").wait_for(state="hidden", timeout=5000)
        if page.locator("#system-banner").is_visible():
            page.locator("#system-banner").wait_for(state="hidden", timeout=8000)
        target = Path(__file__).resolve().parents[2] / ".tmp" / "latency-motion-followup"
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"{name}.png"))

    def send(self, page):
        page.locator("#message-input").fill("合成延迟测试")
        page.evaluate("""() => {
          window.__sentAt=performance.now();window.__feedbackAt=0;
          const observer=new MutationObserver(()=>{
            if(document.querySelector('#timeline .typing-indicator')) {window.__feedbackAt=performance.now();observer.disconnect();}
          });observer.observe(document.querySelector('#timeline'),{childList:true,subtree:true});
        }""")
        page.locator("#send-message").click()
        self.assertTrue(LatencyHandler.started.wait(timeout=5))

    def test_committed_body_precedes_completion_and_retains_nodes_and_private_free_metrics(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page)
                self.send(page)
                page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).wait_for(timeout=2500)
                elapsed = page.evaluate("performance.now() - window.__sentAt")
                self.assertGreaterEqual(elapsed, 550)
                self.assertLess(elapsed, 2000)
                feedback = page.evaluate("window.__feedbackAt - window.__sentAt")
                self.assertGreaterEqual(feedback, 200)
                self.assertLess(feedback, 700)
                self.assertFalse(LatencyHandler.release.is_set())
                self.assertNotIn("未经确认", page.locator("#timeline").inner_text())
                page.evaluate("window.__firstBlock = document.querySelector('#timeline .message.assistant .message-content-stack').firstElementChild")
                LatencyHandler.release.set()
                page.locator("#timeline").get_by_text("第二段完整回复。", exact=True).wait_for()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
                self.assertTrue(page.evaluate("window.__firstBlock === document.querySelector('#timeline .message.assistant .message-content-stack').firstElementChild"))
                page.wait_for_function("window.__projectSnowPerformance.getSamples().length === 1")
                sample = page.evaluate("window.__projectSnowPerformance.getSamples()[0]")
                self.assertEqual(sample["outcome"], "success")
                self.assertGreater(sample["send_to_first_effective_ms"], 0)
                self.assertGreaterEqual(sample["complete_ms"], sample["send_to_first_effective_ms"] - 50)
                encoded = json.dumps(sample)
                for forbidden in ["request_id", "credential", "合成", "第一段", "private-fixture", "private_provider_id", "body"]:
                    self.assertNotIn(forbidden, encoded)
                self.assertEqual(sample["server_timings_ms"], {
                    "queue_ms": 2, "guard_rewrite_ms": 0, "context_build_ms": 11,
                    "retrieval_ms": 12, "prompt_build_ms": 13, "hybrid_total": 14,
                    "initial_model_ms": 15, "provider_headers_ms": 16,
                    "provider_first_content_ms": 17, "provider_first_event_ms": 18,
                    "validation_ms": 20, "validation_total_ms": 22,
                    "first_verified_block_ms": 23, "first_committed_block_ms": 25, "total": 26,
                })
                self.assertEqual(sample["server_queue_ms"], 2)
                self.assertEqual(sample["rewrite_ms"], 0)
                self.assertEqual(LatencyHandler.requests[0][1], "2")
                self.snapshot(page, "communicator")
            finally:
                browser.close()

    def test_reconnect_reuses_uuid_and_never_duplicates_committed_prefix(self):
        LatencyHandler.scenario = "reconnect"
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(reduced_motion="reduce")
                self.open(page); self.send(page)
                page.locator("#timeline").get_by_text("第二段完整回复。", exact=True).wait_for()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(len(LatencyHandler.requests), 2)
                self.assertEqual(LatencyHandler.requests[0], LatencyHandler.requests[1])
                self.assertEqual(page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).count(), 1)
            finally:
                browser.close()

    def test_partial_failure_and_prefix_conflict_keep_body_without_automatic_retry(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                for scenario in ("partial", "conflict"):
                    with self.subTest(scenario=scenario):
                        LatencyHandler.scenario = scenario
                        LatencyHandler.requests = []
                        LatencyHandler.release.clear()
                        page = browser.new_page(reduced_motion="reduce")
                        self.open(page); self.send(page)
                        page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).wait_for()
                        LatencyHandler.release.set()
                        page.locator("#timeline").get_by_text("回复未完成 · 已保留收到的内容", exact=True).wait_for()
                        page.wait_for_function("!document.querySelector('#send-message').disabled")
                        self.assertEqual(len(LatencyHandler.requests), 1)
                        self.assertNotIn("不允许的前缀改写", page.locator("#timeline").inner_text())
                        self.assertEqual(page.evaluate("window.__projectSnowPerformance.getSamples()[0].outcome"), "partial")
                        page.reload(wait_until="networkidle")
                        self.assertEqual(page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).count(), 1)
                        page.close()
            finally:
                browser.close()

    def test_arrival_does_not_cover_scene_and_draft_survives_animation_cancellation(self):
        fixture.PublicFrontendHandler.arrival_started = threading.Event()
        fixture.PublicFrontendHandler.arrival_release = threading.Event()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                self.open(page)
                page.locator("#message-input").fill("通讯器草稿")
                page.locator("#go-in-person").click(); page.locator("#confirm-presence-transition").click()
                self.assertTrue(fixture.PublicFrontendHandler.arrival_started.wait(timeout=5))
                page.locator("#in-person-surface").wait_for(state="visible")
                self.assertTrue(page.locator("#presence-arrival-loading").is_hidden())
                self.assertTrue(page.locator("#stage-speech .typing-indicator").is_visible())
                page.locator("#message-input").fill("到场等待中的草稿")
                page.emulate_media(reduced_motion="reduce")
                page.wait_for_function("document.querySelectorAll('.surface-transition-snapshot').length === 0", timeout=1000)
                self.assertEqual(page.locator(".surface-transition-snapshot").count(), 0)
                self.assertEqual(page.evaluate("document.documentElement.dataset.motionLevel"), "off")
                self.assertEqual(page.evaluate("[...document.querySelectorAll('#text-surface,#in-person-surface')].filter(node=>!node.hidden&&!node.inert).length"), 1)
                fixture.PublicFrontendHandler.arrival_release.set()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(page.locator("#message-input").input_value(), "到场等待中的草稿")
                self.snapshot(page, "in-person-mobile")
                page.locator("#open-communicator").click()
                page.locator("#text-surface").wait_for(state="visible")
                self.assertEqual(page.locator("#message-input").input_value(), "通讯器草稿")
                self.assertEqual(page.locator("[id='stage-speech']").count(), 1)
            finally:
                browser.close()

    def test_database_ack_uncertainty_keeps_request_snapshot_and_uuid_for_manual_recovery(self):
        LatencyHandler.scenario = "database_unknown"
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(reduced_motion="reduce")
                self.open(page, verified=False); self.send(page)
                page.locator("#timeline [data-retry-message]").wait_for()
                self.assertEqual(len(LatencyHandler.requests), 1)
                page.locator("#timeline [data-retry-message]").click()
                page.locator("#timeline").get_by_text("第二段完整回复。", exact=True).wait_for()
                self.assertEqual(len(LatencyHandler.requests), 2)
                self.assertEqual(LatencyHandler.requests[0][0], LatencyHandler.requests[1][0])
                self.assertTrue(page.evaluate("window.__projectSnowPerformance.getSamples().at(-1).recovery"))
                self.assertEqual(page.locator("#timeline .message.user").count(), 1)
                self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
            finally:
                browser.close()

    def test_done_is_durable_and_unlocked_before_natural_wait_and_stop_cannot_lose_it(self):
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page, verified=False); self.send(page)
                page.wait_for_function("window.__projectSnowPerformance.getSamples().length === 1")
                self.assertFalse(page.locator("#send-message").is_disabled())
                stored = page.evaluate("""async () => {
                  const db = await new Promise(resolve => {const request=indexedDB.open('project-snow-public',4);request.onsuccess=()=>resolve(request.result)});
                  const records = await new Promise(resolve => {const request=db.transaction('messages').objectStore('messages').getAll();request.onsuccess=()=>resolve(request.result)});
                  db.close();return records.filter(item => item.role === 'assistant').map(item => ({status:item.status,body:item.content}));
                }""")
                self.assertEqual(stored[0]["status"], "sent")
                self.assertIn("第一段已经确认。", stored[0]["body"])
                page.locator("#stop-waiting").dispatch_event("click")
                page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).wait_for(timeout=1200)
                self.assertEqual(page.locator("#timeline .message.failed").count(), 0)
                self.assertEqual(len(LatencyHandler.requests), 1)
                page.locator("#go-in-person").click(); page.locator("#confirm-presence-transition").click()
                page.wait_for_function("!document.querySelector('#open-communicator').disabled")
                page.locator("#open-communicator").click()
                page.locator("#text-surface").wait_for(state="visible")
                self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
                self.assertEqual(len(LatencyHandler.requests), 1)
            finally:
                browser.close()

    def test_stage_first_glyph_is_paint_but_only_complete_block_is_effective(self):
        LatencyHandler.scenario = "typed_measurement"
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page, verified=False)
                page.locator("#go-in-person").click(); page.locator("#confirm-presence-transition").click()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.send(page)
                page.wait_for_function("""() => {
                  const sample=window.__projectSnowPerformance.getSamples()[0];
                  const speech=document.querySelector('#stage-speech');
                  return sample && sample.ready_to_paint_ms !== null && speech.textContent.startsWith('这一') && !speech.textContent.includes('。');
                }""")
                first_glyph_sample = page.evaluate("window.__projectSnowPerformance.getSamples()[0]")
                self.assertIsNone(first_glyph_sample["send_to_first_effective_ms"])
                self.assertIsNotNone(first_glyph_sample["ready_to_paint_ms"])
                page.wait_for_function("window.__projectSnowPerformance.getSamples()[0].send_to_first_effective_ms > 0")
                self.assertIn("首次有效回复时间。", page.locator("#stage-speech").inner_text())
                effective_sample = page.evaluate("window.__projectSnowPerformance.getSamples()[0]")
                self.assertGreater(effective_sample["send_to_first_effective_ms"], effective_sample["natural_wait_ms"] + effective_sample["ready_to_paint_ms"] + 100)
            finally:
                browser.close()

    def test_cached_completion_does_not_invent_missing_server_acknowledgements(self):
        LatencyHandler.scenario = "cached_timings"
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(reduced_motion="reduce")
                self.open(page, verified=False)
                self.send(page)
                page.wait_for_function("window.__projectSnowPerformance.getSamples().length === 1")
                sample = page.evaluate("window.__projectSnowPerformance.getSamples()[0]")
                self.assertEqual(sample["server_timings_ms"], {
                    "queue_ms": 2, "guard_rewrite_ms": 0, "context_build_ms": 11,
                    "retrieval_ms": 12, "total": 26,
                })
                for absent in ("durable_commit_ms", "first_verified_block_ms", "first_committed_block_ms"):
                    self.assertNotIn(absent, sample["server_timings_ms"])
                self.assertEqual(sample["server_queue_ms"], 2)
                self.assertEqual(sample["rewrite_ms"], 0)
                self.assertNotIn("private-fixture", json.dumps(sample))
                self.assertEqual(len(LatencyHandler.requests), 1)
            finally:
                browser.close()

    def test_rapid_mode_clicks_keep_one_transition_and_channel_drafts(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page)
                page.locator("#message-input").fill("连续切换的通讯器草稿")
                page.locator("#go-in-person").click()
                page.locator("#confirm-presence-transition").click()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                page.locator("#message-input").fill("连续切换的面对面草稿")
                before = LatencyHandler.transition_calls
                LatencyHandler.transition_started.clear()
                LatencyHandler.transition_release = threading.Event()
                page.evaluate("for(let i=0;i<3;i++) document.querySelector('#open-communicator').click()")
                self.assertTrue(LatencyHandler.transition_started.wait(timeout=5))
                self.assertEqual(LatencyHandler.transition_calls, before + 1)
                self.assertTrue(page.locator("#in-person-surface").is_visible())
                self.assertTrue(page.locator("#mode-transition-layer").is_hidden())
                LatencyHandler.transition_release.set()
                page.wait_for_function("!document.querySelector('#text-surface').hidden && !document.querySelector('#send-message').disabled")
                self.assertEqual(page.locator("#message-input").input_value(), "连续切换的通讯器草稿")
                LatencyHandler.transition_release = None
                # A new business operation may begin while the last visual fade is running.
                for _ in range(3):
                    page.locator("#go-in-person").click()
                    page.locator("#confirm-presence-transition").click()
                    page.wait_for_function("!document.querySelector('#in-person-surface').hidden && !document.querySelector('#send-message').disabled")
                    self.assertEqual(page.locator("#message-input").input_value(), "连续切换的面对面草稿")
                    page.locator("#open-communicator").click()
                    page.wait_for_function("!document.querySelector('#text-surface').hidden && !document.querySelector('#send-message').disabled")
                    self.assertEqual(page.locator("#message-input").input_value(), "连续切换的通讯器草稿")
                    self.assertEqual(page.evaluate("[...document.querySelectorAll('#text-surface,#in-person-surface')].filter(node=>!node.hidden&&!node.inert).length"), 1)
                page.wait_for_function("document.querySelectorAll('.surface-transition-snapshot').length === 0")
                self.assertEqual(LatencyHandler.transition_calls, before + 7)
                self.assertEqual(LatencyHandler.requests, [])
                self.assertEqual(page.locator("[id='message-input']").count(), 1)
            finally:
                browser.close()

    def test_synthetic_background_finishes_received_speech_and_resumes_visible_measurement(self):
        """Exercise visibility handlers; headless tabs do not emulate OS backgrounding."""
        LatencyHandler.scenario = "typed_measurement"
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page, verified=False)
                page.locator("#go-in-person").click()
                page.locator("#confirm-presence-transition").click()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.send(page)
                page.wait_for_function("document.querySelector('#stage-speech').textContent.startsWith('这一') && !document.querySelector('#stage-speech').textContent.includes('。')")
                page.locator("#message-input").fill("后台恢复保留的合成草稿")
                page.evaluate("""() => {
                  window.__fixtureHidden=true;
                  Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__fixtureHidden});
                  Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>window.__fixtureHidden?'hidden':'visible'});
                  document.dispatchEvent(new Event('visibilitychange'));
                }""")
                page.wait_for_function("document.querySelector('#stage-speech').textContent.includes('首次有效回复时间。')")
                self.assertEqual(page.evaluate("document.documentElement.dataset.pageHidden"), "true")
                self.assertEqual(page.locator(".surface-transition-snapshot").count(), 0)
                self.assertIsNone(page.evaluate("window.__projectSnowPerformance.getSamples()[0].send_to_first_effective_ms"))
                page.evaluate("window.__fixtureHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_function("window.__projectSnowPerformance.getSamples()[0].send_to_first_effective_ms > 0")
                self.assertEqual(page.evaluate("document.documentElement.dataset.pageHidden"), "false")
                self.assertEqual(page.locator("#message-input").input_value(), "后台恢复保留的合成草稿")
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(len(LatencyHandler.requests), 1)
                page.reload(wait_until="networkidle")
                self.assertEqual(page.locator("#message-input").input_value(), "后台恢复保留的合成草稿")
                self.assertIn("首次有效回复时间。", page.locator("#stage-speech").inner_text())
                self.assertEqual(len(LatencyHandler.requests), 1)
            finally:
                browser.close()

    def test_drawer_keyboard_focus_restores_to_trigger_after_motion(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page)
                page.locator("#message-input").fill("抽屉键盘检查草稿")
                for _ in range(2):
                    page.locator("#open-info").focus()
                    page.keyboard.press("Enter")
                    page.wait_for_function("document.querySelector('#info-panel').contains(document.activeElement)")
                    for key in ("Shift+Tab", "Tab", "Tab"):
                        page.keyboard.press(key)
                        self.assertTrue(page.evaluate("document.querySelector('#info-panel').contains(document.activeElement)"))
                    self.assertTrue(page.locator(".chat-panel").evaluate("node=>node.inert"))
                    page.keyboard.press("Escape")
                    self.assertTrue(page.locator("#open-info").evaluate("node=>node===document.activeElement"))
                    self.assertFalse(page.locator(".chat-panel").evaluate("node=>node.inert"))
                self.assertEqual(page.locator("#message-input").input_value(), "抽屉键盘检查草稿")
                self.assertEqual(LatencyHandler.requests, [])
            finally:
                browser.close()

    def test_recent_stored_bubbles_do_not_replay_after_reload_but_new_replies_animate(self):
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                # Keep historical age inside the old three-second heuristic;
                # monotonic presentation and browser animation clocks stay real.
                clock = page.evaluate("Date.now()")
                page.add_init_script(f"Date.now = () => {clock};")
                page.add_init_script("""(() => {
                  const animate=Element.prototype.animate;
                  window.__bubbleEntrances=[];
                  Element.prototype.animate=function(...args) {
                    if(this.matches('#timeline .message')) window.__bubbleEntrances.push(
                      this.classList.contains('assistant') ? 'assistant' : 'user');
                    return animate.apply(this,args);
                  };
                })();""")
                self.open(page)
                reliability_fixture.PublicFrontendReliabilityTests.seed(page, count=2)
                page.evaluate("""async () => {
                  const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
                  await new Promise((resolve,reject)=>{
                    const tx=db.transaction('messages','readwrite'),store=tx.objectStore('messages');
                    const request=store.get('fixture-000001');
                    request.onsuccess=()=>store.put({...request.result,role:'assistant',createdAtEstimated:true});
                    tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
                  });db.close();
                }""")
                page.reload(wait_until="networkidle")
                self.assertEqual(page.locator("#timeline .message").count(), 2)
                self.assertEqual(page.evaluate("window.__bubbleEntrances"), [])
                self.send(page)
                page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).wait_for()
                page.locator("#timeline").get_by_text("第二段完整回复。", exact=True).wait_for()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                entrances = page.evaluate("window.__bubbleEntrances")
                self.assertEqual(entrances.count("user"), 1)
                self.assertEqual(entrances.count("assistant"), 1)
                self.assertEqual(len(LatencyHandler.requests), 1)
                self.assertNotIn("未经确认", page.locator("#timeline").inner_text())
            finally:
                browser.close()

    def test_actual_runtime_reconciler_restores_external_children_and_preserves_append_focus(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page)
                result = page.evaluate("""async () => {
                  const {reconcileMarkup}=await import('/modules/runtime.js');
                  const root=document.createElement('div');document.body.append(root);
                  const control='<button data-ui-key="control">焦点控件</button>';
                  const tail='<section data-ui-key="tail">末尾控件</section>';
                  const row=blocks=>'<article data-message-id="committed" class="message assistant"><div class="message-body"><span class="meta">角色<input value="original" aria-label="live-value"></span><div class="message-content-stack">'+blocks+'</div><div class="message-tools"></div></div></article>';
                  const first='<p>第一段合成正文。</p>',initial=control+row(first)+tail;
                  try {
                    reconcileMarkup(root,initial);
                    const originalControl=root.firstElementChild,originalRow=root.children[1];
                    const originalLive=originalRow.querySelector('input');originalLive.value='edited-live-value';
                    originalControl.focus();reconcileMarkup(root,initial);
                    const unchanged=root.firstElementChild===originalControl&&root.children[1]===originalRow&&document.activeElement===originalControl
                      &&originalRow.querySelector('input')===originalLive&&originalLive.value==='edited-live-value';
                    const recovered=[];
                    for(const [name,mutate] of [
                      ['add',()=>root.append(document.createTextNode('external-direct-text'))],
                      ['remove',()=>root.children[1].remove()],
                      ['replace',()=>root.children[1].replaceWith(document.createElement('aside'))],
                      ['reorder',()=>root.insertBefore(root.lastChild,root.firstChild)],
                    ]) {
                      mutate();reconcileMarkup(root,initial);
                      recovered.push({name,restored:root.childNodes.length===3&&[...root.children].map(n=>n.dataset.uiKey||n.dataset.messageId).join(',')==='control,committed,tail'});
                    }
                    const retainedRow=root.children[1],retainedBlock=retainedRow.querySelector('.message-content-stack').firstElementChild;
                    const focused=retainedRow.querySelector('input');focused.value='retained-live-value';focused.focus();focused.setSelectionRange(2,7);
                    reconcileMarkup(root,control+row(first+'<p>追加的合成尾段。</p>')+tail);
                    const stack=root.children[1].querySelector('.message-content-stack');
                    return {unchanged,recovered,rowPreserved:root.children[1]===retainedRow,
                      firstBlockPreserved:stack.firstElementChild===retainedBlock,focusPreserved:document.activeElement===focused,
                      liveInputPreserved:root.children[1].querySelector('input')===focused&&focused.value==='retained-live-value'&&focused.selectionStart===2&&focused.selectionEnd===7,
                      texts:[...stack.children].map(n=>n.textContent)};
                  } finally {root.remove();}
                }""")
                self.assertTrue(result["unchanged"])
                self.assertEqual(result["recovered"], [{"name": name, "restored": True} for name in ("add", "remove", "replace", "reorder")])
                self.assertTrue(result["rowPreserved"])
                self.assertTrue(result["firstBlockPreserved"])
                self.assertTrue(result["focusPreserved"])
                self.assertTrue(result["liveInputPreserved"])
                self.assertEqual(result["texts"], ["第一段合成正文。", "追加的合成尾段。"])
                self.assertEqual(LatencyHandler.requests, [])
            finally:
                browser.close()

    def test_history_groups_hong_kong_days_and_keeps_device_clock_and_estimates(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(locale="en-US", timezone_id="America/Los_Angeles")
                self.open(page)
                reliability_fixture.PublicFrontendReliabilityTests.seed(page, count=3)
                clocks = page.evaluate("""async () => {
                  const stamps=['2026-10-13T15:59:59Z','2026-10-13T16:00:00Z','2026-10-13T16:00:02Z'].map(Date.parse);
                  const db=await new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)});
                  await new Promise((resolve,reject)=>{
                    const tx=db.transaction('messages','readwrite'),store=tx.objectStore('messages');
                    stamps.forEach((stamp,index)=>{
                      const request=store.get('fixture-'+String(index).padStart(6,'0'));
                      request.onsuccess=()=>store.put({...request.result,createdAt:stamp,createdAtEstimated:index===1});
                    });tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
                  });db.close();
                  return stamps.slice(0,2).map(stamp=>new Date(stamp).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false}));
                }""")
                page.reload(wait_until="networkidle")
                self.assertEqual(page.locator("#timeline .message").count(), 3)
                labels = page.locator("#timeline .message-time").all_text_contents()
                self.assertEqual(len(labels), 2)
                self.assertTrue(labels[0].endswith(clocks[0]), labels[0])
                self.assertTrue(labels[1].endswith(clocks[1] + "（估算）"), labels[1])
                self.assertEqual(LatencyHandler.requests, [])
            finally:
                browser.close()

    def measure_mode_motion(self, page, trigger):
        page.wait_for_load_state("networkidle")
        page.wait_for_function("document.getAnimations().every(animation=>animation.playState!=='running'||animation.effect.getTiming().iterations===Infinity)")
        page.evaluate("""() => {
          window.__motionLongTasks=[];
          window.__motionObserver=new PerformanceObserver(list=>{
            for(const entry of list.getEntries())window.__motionLongTasks.push({start_ms:entry.startTime,duration_ms:entry.duration});
          });window.__motionObserver.observe({type:'longtask',buffered:false});
          window.__motionTrace={idleStart:performance.now(),actionStart:0,start:0,end:0,frames:[],last:0,interactiveCounts:[]};
        }""")
        page.wait_for_timeout(150)
        page.evaluate("""() => {
          window.__motionTrace.actionStart=performance.now();
          const track=now=>{const t=window.__motionTrace;
            if(t.last)t.frames.push(now-t.last);t.last=now;
            t.interactiveCounts.push([...document.querySelectorAll('#text-surface,#in-person-surface')].filter(n=>!n.hidden&&!n.inert).length);
            if(document.querySelector('.surface-transition-snapshot')){if(!t.start)t.start=now;}
            else if(t.start){t.end=now;return;}requestAnimationFrame(track);
          };requestAnimationFrame(track);
        }""")
        page.locator(trigger).click()
        page.wait_for_function("window.__motionTrace.end > 0")
        page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        return page.evaluate("""() => {
          window.__motionObserver.disconnect();const t=window.__motionTrace;
          return {duration_ms:t.end-t.start,action_to_settled_ms:t.end-t.actionStart,
            idle_long_tasks_ms:window.__motionLongTasks.filter(e=>e.start_ms>=t.idleStart&&e.start_ms<t.actionStart).map(e=>e.duration_ms),
            long_tasks_ms:window.__motionLongTasks.filter(e=>e.start_ms>=t.actionStart&&e.start_ms<=t.end).map(e=>e.duration_ms),
            frame_intervals_ms:t.frames,interactive_surface_counts:t.interactiveCounts,
            interactive_surfaces:[...document.querySelectorAll('#text-surface,#in-person-surface')].filter(n=>!n.hidden&&!n.inert).length,
            snapshots:document.querySelectorAll('.surface-transition-snapshot').length,
            rendered_history_rows:document.querySelectorAll('#timeline .message').length};
        }""")

    def test_warm_mobile_transition_has_one_interactive_surface_and_no_long_task(self):
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                self.open(page)
                if page.locator("#skip-onboarding").is_visible():
                    page.locator("#skip-onboarding").click()
                page.locator("#go-in-person").click(); page.locator("#confirm-presence-transition").click()
                page.wait_for_function("!document.querySelector('#open-communicator').disabled")
                page.wait_for_function("document.querySelectorAll('.surface-transition-snapshot').length === 0")
                page.locator("#open-communicator").click()
                page.wait_for_function("document.querySelector('#text-surface').hidden === false && document.querySelectorAll('.surface-transition-snapshot').length === 0")
                reliability_fixture.PublicFrontendReliabilityTests.seed(page, count=350)
                page.reload(wait_until="networkidle")
                for _ in range(4):
                    page.locator("#load-older-messages").click()
                page.wait_for_function("document.querySelectorAll('#timeline .message').length === 200")
                self.assertEqual(page.locator("#timeline .message").count(), 200)
                self.assertEqual(reliability_fixture.PublicFrontendReliabilityTests.stored_count(page), 350)
                page.locator("#message-input").fill("长历史通讯器草稿")
                page.evaluate("document.querySelector('#timeline').scrollTop=100")
                page.wait_for_function("Math.abs(document.querySelector('#timeline').scrollTop-100)<=1")
                page.evaluate("""() => {
                  const timeline=document.querySelector('#timeline');
                  window.__historyScroll=timeline.scrollTop;
                  window.__keptHistoryNode=timeline.querySelector('[data-message-id]');
                  window.__keptHistoryId=window.__keptHistoryNode.dataset.messageId;
                }""")
                page.locator("#go-in-person").click()
                page.locator("#confirm-presence-transition").wait_for(state="visible")
                forward = self.measure_mode_motion(page, "#confirm-presence-transition")
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                page.locator("#message-input").fill("长历史面对面草稿")
                reverse = self.measure_mode_motion(page, "#open-communicator")
                reverse.update(communicator_draft_preserved=page.locator("#message-input").input_value() == "长历史通讯器草稿",
                               history_node_preserved=page.evaluate("window.__keptHistoryNode === [...document.querySelectorAll('#timeline [data-message-id]')].find(node=>node.dataset.messageId===window.__keptHistoryId)"),
                               scroll_delta_px=page.evaluate("Math.abs(document.querySelector('#timeline').scrollTop-window.__historyScroll)"))
                target = Path(__file__).resolve().parents[2] / ".tmp" / "latency-motion-followup"
                target.mkdir(parents=True, exist_ok=True)
                for direction, result in (("forward", forward), ("reverse", reverse)):
                    result.update(direction=direction, browser=browser.version, viewport={"width": 390, "height": 844},
                                  synthetic_indexeddb_history_rows=350,
                                  environment=f"{platform.system()} headless browser; warm synthetic fixture; no CPU emulation",
                                  platform=platform.system(), browser_channel=os.getenv("PROJECT_SNOW_PLAYWRIGHT_CHANNEL", "").strip() or "bundled",
                                  real_device_profile="unknown", physical_soft_keyboard_tested=False,
                                  phase="idle baseline then confirmed warm mode transition", paid_calls=0)
                    (target / f"performance-{direction}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                    with self.subTest(direction=direction):
                        self.assertEqual(result["interactive_surfaces"], 1)
                        self.assertEqual(set(result["interactive_surface_counts"]), {1})
                        self.assertEqual(result["snapshots"], 0)
                        self.assertEqual(result["rendered_history_rows"], 200)
                        self.assertGreaterEqual(result["duration_ms"], 300)
                        self.assertLessEqual(result["duration_ms"], 430)
                        self.assertEqual(result["idle_long_tasks_ms"], [])
                        self.assertEqual(result["long_tasks_ms"], [])
                self.assertTrue(reverse["communicator_draft_preserved"])
                self.assertTrue(reverse["history_node_preserved"])
                self.assertLessEqual(reverse["scroll_delta_px"], 1)
                saved_drafts = reliability_fixture.PublicFrontendReliabilityTests.read_record(page, "app_state", "drafts")
                self.assertEqual(saved_drafts["values"]["25b23cb64398:in_person:message"], "长历史面对面草稿")
            finally:
                browser.close()

    def test_complete_reply_uses_elapsed_budget_and_no_stream_v2_header(self):
        LatencyHandler.release.set()
        with fixture.sync_playwright() as playwright:
            browser = fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self.open(page, verified=False)
                page.evaluate("""() => {
                  window.__skipped = false;
                  const observer = new MutationObserver(() => {
                    const button = document.querySelector('#fast-forward-reply');
                    if (!button.hidden) { observer.disconnect(); window.__skipped = true; button.click(); }
                  }); observer.observe(document.querySelector('#fast-forward-reply'), {attributes:true, attributeFilter:['hidden']});
                }""")
                self.send(page)
                page.locator("#timeline").get_by_text("第一段已经确认。", exact=True).wait_for(timeout=2500)
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertLess(page.evaluate("performance.now() - window.__sentAt"), 2200)
                self.assertIsNone(LatencyHandler.requests[0][1])
                page.locator("#timeline").get_by_text("第二段完整回复。", exact=True).wait_for(timeout=1000)
                self.assertTrue(page.evaluate("window.__skipped"))
            finally:
                browser.close()
