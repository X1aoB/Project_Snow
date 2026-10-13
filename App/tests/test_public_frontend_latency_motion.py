"""Synthetic browser coverage for verified replies and independent presentation."""
from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as fixture


class LatencyHandler(fixture.PublicFrontendHandler):
    release = threading.Event()
    started = threading.Event()
    requests = []
    scenario = "success"

    def do_POST(self):  # noqa: N802
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
            packet("done", {"communication_channel": payload["communication_channel"], "content_blocks": [first if cls.scenario != "conflict" else {"type": block_type, "text": "不允许的前缀改写"}, second], "truncated": False,
                            "timings_ms": {"queue_ms": 2, "guard_rewrite_ms": 0}})
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

    def tearDown(self):
        LatencyHandler.release.set()
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
        target = Path(__file__).resolve().parents[2] / ".tmp" / "latency-motion"
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
                for forbidden in ["request_id", "credential", "合成", "第一段"]:
                    self.assertNotIn(forbidden, encoded)
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
                page.evaluate("""() => {
                  const root = document.querySelector('#timeline');
                  for (let index=0;index<200;index++) {
                    const row=document.createElement('article');row.className='message assistant';
                    row.dataset.messageId=`synthetic-history-${index}`;
                    row.innerHTML='<div class="message-body"><div class="content-message">仅用于测量转场的合成历史。</div></div>';
                    root.append(row);
                  }
                }""")
                page.evaluate("""() => {
                  window.__motionLongTasks = [];
                  window.__motionObserver = new PerformanceObserver(list => {
                    for (const entry of list.getEntries()) window.__motionLongTasks.push(entry.duration);
                  }); window.__motionObserver.observe({type:'longtask', buffered:false});
                  window.__motionTrace = {start:0,end:0,frames:[],last:0};
                  const track = now => { const t=window.__motionTrace;
                    if (t.last) t.frames.push(now-t.last);t.last=now;
                    if (document.querySelector('.surface-transition-snapshot')) {if (!t.start)t.start=now;}
                    else if(t.start){t.end=now;return;}
                    requestAnimationFrame(track);
                  };requestAnimationFrame(track);
                }""")
                page.locator("#go-in-person").click(); page.locator("#confirm-presence-transition").click()
                page.wait_for_function("window.__motionTrace.end > 0")
                result = page.evaluate("""() => {
                  window.__motionObserver.disconnect();const t=window.__motionTrace;
                  return {duration_ms:t.end-t.start,long_tasks_ms:window.__motionLongTasks,
                    frame_intervals_ms:t.frames,interactive_surfaces:[...document.querySelectorAll('#text-surface,#in-person-surface')].filter(node=>!node.hidden&&!node.inert).length,
                    snapshots:document.querySelectorAll('.surface-transition-snapshot').length};
                }""")
                result.update(browser=browser.version, viewport={"width": 390, "height": 844}, synthetic_history_rows=200, environment="Windows headless Chromium; warm synthetic fixture; no CPU emulation")
                target = Path(__file__).resolve().parents[2] / ".tmp" / "latency-motion"
                target.mkdir(parents=True, exist_ok=True)
                (target / "performance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                self.assertEqual(result["interactive_surfaces"], 1)
                self.assertEqual(result["snapshots"], 0)
                self.assertGreaterEqual(result["duration_ms"], 300)
                self.assertLessEqual(result["duration_ms"], 430)
                self.assertEqual(result["long_tasks_ms"], [])
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
