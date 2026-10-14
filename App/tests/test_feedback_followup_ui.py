"""Hand-authored, loopback-only regressions for the October feedback follow-up.

No user messages, runtime databases or model providers are used by this suite.
"""
from __future__ import annotations

import json
import base64
import os
import threading
import time
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as fixture


class FollowupHandler(fixture.PublicFrontendHandler):
    scenario = "replay"
    requests = []
    release = threading.Event()
    started = threading.Event()

    def do_POST(self):  # noqa: N802
        if self.path != "/public/v1/chat/stream":
            return super().do_POST()
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        cls = type(self)
        cls.requests.append(payload)
        cls.started.set()
        if cls.scenario == "failure":
            self._json({"detail": {"code": "generation_failed", "stage": "unknown", "retryable": False}}, status=500)
            return
        if cls.scenario == "held":
            cls.release.wait(timeout=10)
        channel = payload["communication_channel"]
        block_type = "speech" if channel == "in_person" else "message"
        blocks = [{"type": block_type, "text": text} for text in ("合成第一段。", "合成第二段。", "合成第三段。", "合成第四段。")]
        if cls.scenario == "stage_action":
            blocks = [{"type": "action", "text": "她把合成的蓝色卡片放在桌上。"}, {"type": "speech", "text": "这是合成第一轮对白。"}]
        elif cls.scenario == "stage_speech":
            blocks = [{"type": "speech", "text": "这是合成第二轮对白。"}]
        elif cls.scenario == "action_fallback":
            blocks = [{"type": "action", "text": "她收起合成的绿色卡片。"}]
        packets = ["event: progress\ndata: " + json.dumps({"phase": "generating"}) + "\n\n"]
        if cls.scenario == "action_fallback":
            packets.append("event: delta\ndata: " + json.dumps({"block_index": 0, "block_type": "action", "text": blocks[0]["text"]}, ensure_ascii=False) + "\n\n")
        elif cls.scenario not in {"stage_action", "stage_speech"}:
            for sequence, block in enumerate(blocks):
                packets.append("event: committed_block\ndata: " + json.dumps({
                    "request_id": payload["request_id"], "character_id": payload["character_id"],
                    "sequence": sequence, "block_id": f"synthetic-{sequence}", "block": block,
                }, ensure_ascii=False) + "\n\n")
        if cls.scenario == "quota":
            package = base64.urlsafe_b64encode(json.dumps({"revision": 1, "schedule_date": "2026-10-14"}).encode()).decode().rstrip("=") + ".synthetic"
            packets.append("event: state\ndata: " + json.dumps({"state_package": package, "scene_state": {
                "character_location": "合成地点", "character_activity": "合成活动", "co_located": False, "visual_key": "generic",
            }}, ensure_ascii=False) + "\n\n")
        terminal = {"communication_channel": channel, "usage": {"provider_calls": 1, "total_tokens": 25}}
        if cls.scenario != "action_fallback":
            terminal["content_blocks"] = blocks
        packets.append("event: done\ndata: " + json.dumps(terminal, ensure_ascii=False) + "\n\n")
        if cls.scenario == "partial_once":
            packets = packets[:2] + ["event: error\ndata: " + json.dumps({"code": "provider_network_error", "partial": True, "retryable": False}) + "\n\n"]
        body = "".join(packets).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        # The no-EOF case verifies that terminal completion owns the result.
        if cls.scenario != "no_eof":
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if cls.scenario == "slow":
                progress = packets[0].encode()
                self.wfile.write(progress)
                self.wfile.flush()
                time.sleep(1.15)
                self.wfile.write(body[len(progress):])
            else:
                self.wfile.write(body)
            self.wfile.flush()
            if cls.scenario == "no_eof":
                cls.release.wait(timeout=10)
        except (BrokenPipeError, ConnectionResetError):
            pass


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class FeedbackFollowupUITests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FollowupHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        FollowupHandler.scenario = "replay"
        FollowupHandler.requests = []
        FollowupHandler.release = threading.Event()
        FollowupHandler.started = threading.Event()
        FollowupHandler.feedback_payload = None
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 850}, reduced_motion="no-preference")
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(8000)

    def tearDown(self):
        FollowupHandler.release.set()
        self.browser.close()
        self.playwright.stop()
        fixture.PublicFrontendE2ETests.setUp(self)

    def open(self):
        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {"chat_latency_v2": True, "verified_block_stream": False, "verified_block_replay": True, "motion_v2": True}
            payload["model_profiles"] = {"openai": ["gpt-e2e"]}
            route.fulfill(json=payload)
        self.page.route("**/public/v1/config", configure)
        self.page.goto(self.base_url, wait_until="networkidle")
        self.page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(self.page)
        if self.page.locator("#skip-onboarding").is_visible():
            self.page.locator("#skip-onboarding").click()

    def send(self, text="合成跟进测试"):
        FollowupHandler.started.clear()
        self.page.locator("#message-input").fill(text)
        self.page.evaluate("""() => {
          window.__sentAt=performance.now();window.__blockTimes=[];window.__feedbackAt=0;
          window.__blocksSeen=new Set();
          window.__followupObserver?.disconnect();
          window.__followupObserver=new MutationObserver(()=>{
            for(const node of document.querySelectorAll('#timeline .message.assistant .content-message')) {
              if(!window.__blocksSeen.has(node.textContent) && node.textContent.startsWith('合成')) {
                window.__blocksSeen.add(node.textContent);window.__blockTimes.push(performance.now());
              }
            }
            if(!window.__feedbackAt && (document.querySelector('#timeline .typing-indicator')
               || document.querySelector('#request-status').textContent.includes('正在'))) window.__feedbackAt=performance.now();
          });
          window.__followupObserver.observe(document.querySelector('.chat-panel'),{childList:true,subtree:true,characterData:true});
        }""")
        self.page.locator("#send-message").click()
        self.assertTrue(FollowupHandler.started.wait(timeout=5))

    def completed(self):
        self.page.wait_for_function("!document.querySelector('#send-message').disabled")

    def arrived(self):
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")
        self.page.locator("#fast-forward-reply").evaluate("node=>node.click()")

    def submit_feedback(self):
        self.page.locator("#feedback-body").fill("合成诊断归属验证")
        self.page.locator("#feedback-form button[type=submit]").click()
        self.page.locator("#feedback-dialog").wait_for(state="hidden")
        return FollowupHandler.feedback_payload

    def test_normal_motion_replay_keeps_four_bubble_gaps_and_unlocks_during_pacing(self):
        self.open()
        self.send()
        self.completed()
        # Network completion must release controls before the natural floor.
        self.assertLess(self.page.evaluate("performance.now()-window.__sentAt"), 550)
        self.page.wait_for_function("window.__blockTimes.length===4")
        times = self.page.evaluate("window.__blockTimes")
        self.assertGreaterEqual(times[0] - self.page.evaluate("window.__sentAt"), 550)
        for gap in (right - left for left, right in zip(times, times[1:])):
            self.assertGreaterEqual(gap, 175)
            self.assertLess(gap, 550)
        self.assertLessEqual(times[-1] - times[0], 1300)
        self.assertEqual(self.page.locator("#timeline .message.assistant").count(), 1)
        self.assertEqual(self.errors, [])

    def test_slow_reply_deducts_real_wait_and_delays_early_progress(self):
        FollowupHandler.scenario = "slow"
        self.open()
        self.send()
        self.page.wait_for_timeout(420)
        self.assertEqual(self.page.locator("#timeline .typing-indicator").count(), 0)
        self.assertEqual(self.page.locator("#request-status").inner_text(), "")
        self.page.wait_for_function("window.__feedbackAt>0")
        feedback = self.page.evaluate("window.__feedbackAt-window.__sentAt")
        self.assertGreaterEqual(feedback, 450)
        self.assertLess(feedback, 850)
        self.page.wait_for_function("window.__blockTimes.length===4")
        first = self.page.evaluate("window.__blockTimes[0]-window.__sentAt")
        self.assertGreaterEqual(first, 1100)
        self.assertLess(first, 1500)
        self.assertEqual(self.errors, [])

    def test_reduced_motion_skips_bubble_delays(self):
        self.page.emulate_media(reduced_motion="reduce")
        self.open()
        self.send()
        self.page.wait_for_function("window.__blockTimes.length===4")
        times = self.page.evaluate("window.__blockTimes")
        self.assertLess(times[-1] - self.page.evaluate("window.__sentAt"), 350)
        self.assertLess(times[-1] - times[0], 100)
        self.assertEqual(self.errors, [])

    def test_terminal_done_unlocks_without_socket_eof_and_next_send_preserves_reply(self):
        FollowupHandler.scenario = "no_eof"
        self.open()
        self.send()
        self.completed()
        self.assertFalse(FollowupHandler.release.is_set())
        self.page.wait_for_function("window.__blockTimes.length>=1")
        FollowupHandler.scenario = "replay"
        self.send("合成第二个请求")
        self.completed()
        self.page.wait_for_function("document.querySelectorAll('#timeline .message.assistant .content-message').length===8")
        self.assertEqual(self.page.locator("#timeline .message.user.failed").count(), 0)
        self.assertEqual(len(FollowupHandler.requests), 2)
        self.assertNotEqual(FollowupHandler.requests[0]["request_id"], FollowupHandler.requests[1]["request_id"])
        self.assertEqual(self.errors, [])

    def test_world_metadata_quota_failure_preserves_terminal_reply_and_never_resends(self):
        FollowupHandler.scenario = "quota"
        self.open()
        self.page.evaluate("""() => {
          const put=IDBObjectStore.prototype.put;window.__worldQuotaFaults=0;
          IDBObjectStore.prototype.put=function(value,...args) {
            if(this.name==='app_state' && value?.key==='world' && value.revision===1) {
              window.__worldQuotaFaults++;throw new DOMException('synthetic quota failure','QuotaExceededError');
            }
            return put.call(this,value,...args);
          };
        }""")
        self.send("合成本地持久化故障")
        self.completed()
        self.page.wait_for_function("window.__blockTimes.length===4")
        self.assertEqual(self.page.evaluate("window.__worldQuotaFaults"), 1)
        self.assertIn("合成地点", self.page.locator("#go-in-person-label").text_content())
        self.assertEqual(self.page.locator("#timeline .message.user.failed").count(), 0)
        self.assertEqual(self.page.locator("[data-retry-message]").count(), 0)
        self.assertEqual(len(FollowupHandler.requests), 1)
        self.assertEqual(self.errors, [])

    def test_manual_partial_recovery_keeps_delivered_prefix_and_reuses_the_request(self):
        FollowupHandler.scenario = "partial_once"
        self.open()
        self.send("合成部分结果恢复")
        self.completed()
        self.page.wait_for_function("document.querySelector('#timeline .message.assistant.partial') || document.querySelector('#timeline .message.assistant.failed')")
        self.page.evaluate("""() => {
          window.__keptPrefix=document.querySelector('#timeline .message.assistant .content-message');
          window.__prefixWasRemoved=false;
          new MutationObserver(()=>{if(!window.__keptPrefix.isConnected)window.__prefixWasRemoved=true;})
            .observe(document.querySelector('#timeline'),{subtree:true,childList:true});
        }""")
        FollowupHandler.scenario = "replay"
        self.page.locator("#timeline .message.user.failed [data-retry-message]").click()
        self.page.wait_for_function("document.querySelectorAll('#timeline .message.assistant .content-message').length===4")
        self.completed()
        self.assertFalse(self.page.evaluate("window.__prefixWasRemoved"))
        self.assertTrue(self.page.evaluate("window.__keptPrefix===document.querySelector('#timeline .message.assistant .content-message')"))
        self.assertEqual(self.page.locator("#timeline .message.assistant").count(), 1)
        self.assertEqual(len(FollowupHandler.requests), 2)
        self.assertEqual(FollowupHandler.requests[0]["request_id"], FollowupHandler.requests[1]["request_id"])
        self.assertEqual(self.errors, [])

    def test_presentation_fault_cannot_downgrade_terminal_success_or_create_a_retry(self):
        self.open()
        self.page.evaluate("""() => {
          const animate=Element.prototype.animate;window.__presentationFaults=0;
          Element.prototype.animate=function(...args) {
            if(this.matches('.message.assistant') && !window.__presentationFaults) {
              window.__presentationFaults++;throw new Error('synthetic presentation failure');
            }
            return animate.apply(this,args);
          };
        }""")
        self.send("合成显示故障")
        self.completed()
        self.page.wait_for_function("window.__blockTimes.length===4")
        self.assertEqual(self.page.evaluate("window.__presentationFaults"), 1)
        self.assertEqual(self.page.locator("#timeline .message.user.failed").count(), 0)
        self.assertEqual(self.page.locator("[data-retry-message]").count(), 0)
        self.assertEqual(len(FollowupHandler.requests), 1)
        self.assertEqual(self.errors, [])

    def test_failed_text_status_stays_in_text_and_feedback_binds_its_request(self):
        self.open()
        self.send("合成成功的旧轮")
        self.page.wait_for_function("window.__blockTimes.length===4")
        first_request = FollowupHandler.requests[-1]["request_id"]
        FollowupHandler.scenario = "failure"
        self.send("合成失败的新轮")
        self.completed()
        failed_request = FollowupHandler.requests[-1]["request_id"]
        self.assertEqual(self.page.locator("#timeline .message.user.failed").count(), 1)
        self.page.locator("#open-global-feedback").click()
        preview = self.page.locator("#feedback-context-preview-text").text_content()
        self.assertIn("合成失败的新轮", preview)
        self.assertNotIn("合成成功的旧轮", preview)
        self.assertNotIn("合成第一段", preview)
        failure = self.submit_feedback()
        self.assertEqual(failure["chat_request_id"], failed_request)
        self.assertEqual(failure["error_code"], "generation_failed")
        self.assertEqual(failure["assistant_content_blocks"], [])
        self.page.locator("#timeline [data-feedback-message]").last.click()
        success = self.submit_feedback()
        self.assertEqual(success["chat_request_id"], first_request)
        self.assertEqual(success["error_code"], "")
        self.arrived()
        self.assertNotIn("生成", self.page.locator("#request-status").inner_text())
        self.assertEqual(self.page.locator("#request-status [data-retry-message]").count(), 0)
        self.page.locator("#floating-feedback").evaluate("node=>node.click()")
        self.assertTrue(self.page.locator("#feedback-context-preview-text").text_content().startswith("角色："))
        self.assertIn("到场回应", self.page.locator("#feedback-context").text_content())
        arrival = self.submit_feedback()
        self.assertEqual(arrival["user_content_blocks"], [])
        self.assertEqual(arrival["error_code"], "")
        self.assertEqual(arrival["request_stage"], "in_person")
        self.page.locator("#open-communicator").click()
        self.page.locator("#text-surface").wait_for(state="visible")
        self.assertEqual(self.page.locator("#timeline .message.user.failed [data-retry-message]").count(), 1)
        self.assertEqual(self.errors, [])

    def test_in_person_speech_only_turn_never_replays_scene_intro_or_previous_action(self):
        self.open()
        self.arrived()
        FollowupHandler.scenario = "stage_action"
        self.send("合成动作轮")
        self.completed()
        self.page.wait_for_function("document.querySelector('#stage-speech').textContent==='这是合成第一轮对白。'")
        self.assertIn("蓝色卡片", self.page.locator("#stage-narration").inner_text())
        self.page.evaluate("""() => {window.__stageSeen=[];new MutationObserver(()=>{
          window.__stageSeen.push({speech:document.querySelector('#stage-speech').textContent,narration:document.querySelector('#stage-narration').textContent});
        }).observe(document.querySelector('#in-person-surface'),{subtree:true,childList:true,characterData:true});}""")
        FollowupHandler.scenario = "stage_speech"
        self.send("合成纯对白轮")
        self.completed()
        self.page.wait_for_function("document.querySelector('#stage-speech').textContent==='这是合成第二轮对白。'")
        self.assertEqual(self.page.locator("#stage-narration").inner_text(), "")
        seen = self.page.evaluate("window.__stageSeen")
        self.assertFalse(any("场景已经建立" in item["speech"] for item in seen))
        self.assertFalse(any("正在看雪" in item["narration"] for item in seen))
        self.assertNotIn("蓝色卡片", self.page.locator("#stage-speech").inner_text())
        self.assertEqual(self.errors, [])

    def test_legacy_typed_action_fallback_keeps_action_out_of_speech(self):
        self.open()
        self.arrived()
        FollowupHandler.scenario = "action_fallback"
        self.send("合成旧协议动作保真")
        self.completed()
        self.page.wait_for_function("document.querySelector('#stage-narration').textContent==='她收起合成的绿色卡片。'")
        self.assertEqual(self.page.locator("#stage-speech").text_content(), "")
        self.assertEqual(self.page.locator("#timeline .message.user.failed").count(), 0)
        self.assertEqual(self.errors, [])

    def test_timeline_append_pacing_and_resize_preserve_history_reader_but_follow_bottom(self):
        self.open()
        self.page.evaluate("""() => new Promise((resolve,reject)=>{
          const req=indexedDB.open('project-snow-public',4);req.onerror=()=>reject(req.error);req.onsuccess=()=>{
            const db=req.result,tx=db.transaction(['threads','messages'],'readwrite');
            const get=tx.objectStore('threads').get('25b23cb64398');get.onsuccess=()=>{
              const thread=get.result;thread.messageCount=45;tx.objectStore('threads').put(thread);
              for(let i=0;i<45;i++)tx.objectStore('messages').put({id:'synthetic-history-'+i,characterId:thread.characterId,
                role:i%2?'assistant':'user',communicationChannel:'text',status:'sent',createdAt:Date.now()-500000+i*1000,
                conversationSegmentId:thread.conversationSegmentId,contentBlocks:[{type:'message',text:'历史合成消息 '+i+'。用于检查阅读位置。'.repeat(5)}]});
            };tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);
          };
        })""")
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelectorAll('#timeline .message').length===45")
        self.page.locator("#timeline").evaluate("node=>{node.scrollTop=460;}")
        self.page.wait_for_timeout(100)
        self.page.evaluate("""() => {const t=document.querySelector('#timeline'),top=t.getBoundingClientRect().top;
          const anchor=[...t.querySelectorAll('[data-message-id]')].find(n=>n.getBoundingClientRect().bottom>top);
          window.__historyAnchor={id:anchor.dataset.messageId,offset:anchor.getBoundingClientRect().top-top};}""")
        self.send()
        self.page.wait_for_function("window.__blockTimes.length===4")
        self.assertLess(self.page.evaluate("""() => {const t=document.querySelector('#timeline'),a=window.__historyAnchor;
          const n=[...t.querySelectorAll('[data-message-id]')].find(n=>n.dataset.messageId===a.id);
          return Math.abs(n.getBoundingClientRect().top-t.getBoundingClientRect().top-a.offset);}"""), 3)
        self.page.set_viewport_size({"width": 1100, "height": 720})
        self.page.wait_for_timeout(150)
        offset_error = self.page.evaluate("""() => {const t=document.querySelector('#timeline'),a=window.__historyAnchor;
          const n=[...t.querySelectorAll('[data-message-id]')].find(n=>n.dataset.messageId===a.id);
          return Math.abs(n.getBoundingClientRect().top-t.getBoundingClientRect().top-a.offset);}""")
        self.assertLess(offset_error, 3)
        self.page.locator("#timeline").evaluate("node=>{node.scrollTop=node.scrollHeight;}")
        self.page.wait_for_timeout(100)
        self.send("合成贴底检查")
        self.page.wait_for_function("""() => {const rows=[...document.querySelectorAll('#timeline .message.assistant')];
          return rows.length===24 && rows.at(-1).querySelectorAll('.content-message').length===4;}""")
        self.page.set_viewport_size({"width": 1200, "height": 800})
        self.page.wait_for_timeout(150)
        self.assertLess(self.page.locator("#timeline").evaluate("n=>n.scrollHeight-n.scrollTop-n.clientHeight"), 3)
        self.assertEqual(self.errors, [])
