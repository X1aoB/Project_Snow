"""Synthetic browser contracts for stateless v2 dialogue and scene continuity."""
from __future__ import annotations

import base64
import copy
import json
import os
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as fixture

CHARACTER = "25b23cb64398"
OTHER = "9f5804761c56"


def token(value):
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode()).decode().rstrip("=") + ".synthetic"


def untoken(value):
    try:
        part = value.split(".")[0]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, IndexError):
        return {}


def context_bytes(envelope):
    value = {key: envelope[key] for key in ("segment_id", "session_context_package", "memory_candidates")}
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


class DialogueHandler(fixture.PublicFrontendHandler):
    requests = []
    scenario = "success"
    results = {}
    counter = 0
    scope_rejected = False
    memory_enabled = True
    join_started = threading.Event()
    join_release = threading.Event()

    def do_POST(self):  # noqa: N802
        if self.path not in {"/public/v2/scene/action", "/public/v2/chat/stream"}:
            return super().do_POST()
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        cls = type(self)
        recovery = self.headers.get("X-Snow-Recovery-Only") == "1"
        cls.requests.append({"path": self.path, "payload": copy.deepcopy(payload), "recovery": recovery})
        chat = payload.get("chat", payload)
        request_id = chat["request_id"]
        if recovery and cls.scenario == "scene_conflict" and self.path.endswith("/scene/action"):
            return self._json({"detail": {"code": "request_id_conflict", "retryable": False, "stage": "idempotency_store"}}, status=409)
        if recovery and cls.scenario == "unavailable":
            return self._json({"detail": {"code": "request_result_unavailable", "retryable": False}}, status=410)
        if recovery and request_id not in cls.results:
            return self._json({"detail": {"code": "request_result_unavailable", "retryable": False}}, status=410)
        if cls.scenario == "context_rejected" and self.path.endswith("/chat/stream") and not recovery and not cls.scope_rejected:
            cls.scope_rejected = True
            return self._json({"detail": {"code": "session_context_invalid", "retryable": False, "stage": "state_validation"}}, status=422)
        if cls.scenario == "scope_changed" and self.path.endswith("/scene/action") and not recovery:
            if payload.get("session_context_package"):
                return self._json({"detail": {"code": "session_context_subject_mismatch", "retryable": False, "stage": "state_validation"}}, status=409)
            if payload.get("state_package"):
                return self._json({"detail": {"code": "state_subject_mismatch", "retryable": False, "stage": "state_validation"}}, status=409)
        if recovery and request_id in cls.results:
            result = cls.results[request_id]
        else:
            world = untoken(chat.get("state_package", "")) or {"revision": 0, "analyst_location": None,
                "presence": {CHARACTER: {"location": "观景区", "activity": "合成看雪"}, OTHER: {"location": "资料室", "activity": "合成读书"}},
                "pending_rendezvous": {}}
            world["revision"] += 1
            world["schedule_date"] = chat.get("local_day_key") or "2026-10-15"
            character = chat["character_id"]
            context = untoken(payload.get("session_context_package", ""))
            previous_memories = context.get("memories", [])
            context.update(schema_version="public-session-context-1", character_id=character,
                segment_id=payload["segment_id"], revision=context.get("revision", 0) + 1,
                scene_epoch=context.get("scene_epoch", 0), local_day_key=world["schedule_date"])
            if chat.get("continuity_decision") == "start_today":
                world["pending_rendezvous"].pop(character, None)
                context["scene_overlay"] = None
                context["active_invitation"] = None
            location = world["presence"][character]["location"]
            channel = chat.get("communication_channel", context.get("scene_overlay", {}).get("communication_channel", "text") if context.get("scene_overlay") else "text")
            action = {"status": "not_requested"}
            if self.path.endswith("/scene/action"):
                operation = payload["action"]
                pending = world["pending_rendezvous"].get(character)
                if operation in {"join_character", "join_rendezvous"}:
                    location = pending["location_name"] if pending else location
                    world["presence"][character]["location"] = location
                    world["analyst_location"] = location
                    world["pending_rendezvous"].pop(character, None)
                    channel = "in_person"
                    action = {"status": "joined", "location_name": location}
                elif operation == "open_communicator":
                    channel = "text"
                    action = {"status": "applied"}
                elif operation == "cancel_rendezvous":
                    world["pending_rendezvous"].pop(character, None)
                    context["active_invitation"] = None
                    action = {"status": "cancelled"}
            elif "合成未来邀请" in chat["message"]:
                status = next((value for value in ("deferred", "proposed", "clarify", "accepted") if value in chat["message"]), "accepted")
                context["active_invitation"] = {"invitation_id": f"invite:{request_id}", "location_id": "base_restaurant",
                    "activity_id": "eat", "status": status, "time_scope": "future"}
                action = {"status": "deferred", "location_id": "base_restaurant"}
            elif chat.get("movement_location_id") or "合成邀请" in chat["message"]:
                location = "基地餐厅"
                world["presence"][character]["location"] = location
                pending = {"rendezvous_id": f"{request_id}:waiting", "character_id": character,
                    "location_id": chat.get("movement_location_id") or "base_restaurant", "location_name": location,
                    "activity_id": "eat", "schedule_date": world["schedule_date"]}
                world["pending_rendezvous"][character] = pending
                action = {"status": "character_waiting", "location_id": pending["location_id"], "location_name": location}
            memories = []
            if (self.path.endswith("/chat/stream") and "合成未来邀请" not in chat["message"]) or action["status"] == "joined":
                cls.counter += 1
                memory = {"memory_id": f"synthetic-memory-{cls.counter:04d}", "kind": "shared_scene_event" if action["status"] == "joined" else "user_statement",
                    "value": f"合成经历{cls.counter}", "source_request_id": request_id, "segment_id": payload["segment_id"],
                    "occurred_at": datetime.now(timezone.utc).isoformat(), "scene_epoch": context["scene_epoch"],
                    "source_message_ids": [f"user:{request_id}", f"reply:{request_id}"], "location_id": ""}
                memories = [{"memory": memory, "proof": "synthetic-proof"}]
            context["memories"] = (previous_memories + [receipt["memory"] for receipt in memories])[-12:]
            if not cls.memory_enabled:
                memories = []
                context["memories"] = []
            context["scene_overlay"] = {"communication_channel": channel, "scene_epoch": context["scene_epoch"]}
            scene = {"character_location": location, "character_activity": world["presence"][character]["activity"],
                "analyst_location": world["analyst_location"], "co_located": world["analyst_location"] == location,
                "visual_key": "canteen" if location == "基地餐厅" else "archive" if location == "资料室" else "observation"}
            result = {"request_id": request_id, "character_id": character, "state_package": token(world), "scene_state": scene,
                "session_context_package": token(context), "session_memories": memories, "scene_action": action,
                "movement_status": action, "communication_channel": channel,
                "content_blocks": [{"type": "speech" if channel == "in_person" else "message", "text": "合成的有效回复。"}],
                "usage": {"provider_calls": 0 if self.path.endswith("/scene/action") else 1, "total_tokens": 10}}
            if "原作" in chat.get("message", ""):
                result["evidence_sources"] = [{"document_id": "synthetic-canon-1", "title": "合成原作 <img src=x>", "source_type": "personal_story", "local_path": "private-test-path"}]
            cls.results[request_id] = result
        if self.path.endswith("/scene/action"):
            if cls.scenario == "first_join_pending" and not recovery and payload["action"] in {"join_character", "join_rendezvous"}:
                cls.join_started.set()
                cls.join_release.wait(timeout=12)
                try:
                    return self._json(result)
                except (BrokenPipeError, ConnectionResetError):
                    return
            if cls.scenario == "scene_disconnect" and not recovery and payload["action"] != "resolve":
                return self._json({"detail": {"code": "request_failed", "retryable": True}}, status=503)
            return self._json(result)
        attempts = sum(row["payload"].get("chat", {}).get("request_id") == request_id for row in cls.requests)
        if cls.scenario in {"disconnect", "unavailable"} and attempts == 1:
            packets = "event: meta\ndata: {}\n\n"
        elif cls.scenario == "manual":
            packets = 'event: error\ndata: {"code":"provider_network_error","retryable":false}\n\n'
        else:
            packets = "event: state\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n"
            packets += "event: done\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n"
        body = packets.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class DialogueV2FrontendTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DialogueHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        DialogueHandler.requests = []; DialogueHandler.results = {}; DialogueHandler.counter = 0
        DialogueHandler.scope_rejected = False
        DialogueHandler.memory_enabled = True
        DialogueHandler.join_started = threading.Event()
        DialogueHandler.join_release = threading.Event()
        DialogueHandler.scenario = "success"
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 850}, reduced_motion="reduce")
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        DialogueHandler.join_release.set()
        self.browser.close(); self.playwright.stop()

    def open(self, v2=True, motion=True, memory=True, snow=False):
        DialogueHandler.memory_enabled = memory
        def config(route):
            payload = route.fetch().json()
            payload["features"] = {"chat_latency_v2": True, "motion_v2": motion, "verified_block_replay": True,
                "dialogue_domains_v2": v2, "scene_actions_v2": v2, "session_memory_v2": v2 and memory, "ui_snow_adv": snow}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2", "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            route.fulfill(json=payload)
        self.page.route("**/public/v1/config", config)
        self.page.goto(self.base_url, wait_until="networkidle")
        self.page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(self.page)
        if self.page.locator("#skip-onboarding").is_visible(): self.page.locator("#skip-onboarding").click()

    def send(self, message="合成日常聊天"):
        self.page.locator("#message-input").fill(message)
        self.page.locator("#send-message").click()
        self.page.wait_for_function("!document.querySelector('#send-message').disabled")
        self.page.wait_for_timeout(80)

    def recover_chat(self, request_id):
        with self.page.expect_response(lambda response: response.url.endswith("/public/v2/chat/stream")
                and response.request.post_data_json["chat"]["request_id"] == request_id
                and response.request.headers.get("x-snow-recovery-only") == "1"):
            self.page.locator("[data-retry-message]").click()
        # Retry first awaits IndexedDB before marking the composer busy. Its
        # initial enabled state is not evidence that reconciliation finished.
        self.page.wait_for_function("""requestId => new Promise(resolve => {
          const r = indexedDB.open('project-snow-public', 4);
          r.onsuccess = () => {
            const db = r.result, tx = db.transaction('messages', 'readonly'), store = tx.objectStore('messages');
            const user = store.get(`user:${requestId}`), reply = store.get(`reply:${requestId}`);
            tx.oncomplete = () => { resolve(user.result?.status === 'sent' && !user.result.requestSnapshot
              && reply.result?.status === 'sent'); db.close(); };
          };
        })""", arg=request_id)

    def rows(self, path="/public/v2/chat/stream"):
        return [row for row in DialogueHandler.requests if row["path"] == path]

    def local(self):
        return self.page.evaluate("""() => new Promise((resolve,reject)=>{
          const r=indexedDB.open('project-snow-public',4);r.onerror=()=>reject(r.error);
          r.onsuccess=()=>{const db=r.result;const tx=db.transaction(['app_state','messages','threads'],'readonly');
            const state=tx.objectStore('app_state').getAll(),messages=tx.objectStore('messages').getAll(),threads=tx.objectStore('threads').getAll();
            tx.oncomplete=()=>{resolve({version:db.version,state:state.result,messages:messages.result,threads:threads.result});db.close();};};})""")

    def test_v2_request_sidecars_and_final_reply_share_transaction(self):
        self.open()
        self.page.evaluate("""() => {window.__transactions=[];const original=IDBDatabase.prototype.transaction;
          IDBDatabase.prototype.transaction=function(names,mode,...rest){if(mode==='readwrite')window.__transactions.push(Array.from(typeof names==='string'?[names]:names));return original.call(this,names,mode,...rest);};}""")
        self.send()
        row = self.rows()[0]
        self.assertEqual(row["payload"]["protocol"], "public-dialogue-v2")
        self.assertFalse(row["recovery"])
        local = self.local()
        self.assertEqual(local["version"], 4)
        request = row["payload"]["chat"]["request_id"]
        self.assertEqual({m["id"] for m in local["messages"]}, {f"user:{request}", f"reply:{request}"})
        sidecar = next(r for r in local["state"] if r["key"].startswith("dialogue_context_v1:"))
        self.assertEqual(len(sidecar["memories"]), 1)
        self.assertTrue(any(set(names) == {"threads", "messages", "app_state"} for names in self.page.evaluate("window.__transactions")))
        self.assertFalse(self.errors)

    def test_transport_recovery_freezes_envelope_uuid_and_endpoint(self):
        self.open(); DialogueHandler.scenario = "disconnect"
        self.send()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["recovery"]); self.assertTrue(rows[1]["recovery"])
        self.assertEqual(rows[0]["payload"], rows[1]["payload"])
        self.assertEqual(len(self.page.locator("#timeline .message.assistant").all()), 1)

    def test_expired_recovery_cannot_generate_or_downgrade(self):
        self.open(); DialogueHandler.scenario = "unavailable"
        self.send()
        self.assertEqual(len(self.rows()), 2)
        self.assertIn("恢复窗口已结束", self.page.locator("#request-status").inner_text())
        self.assertEqual(self.page.locator("[data-retry-message]").count(), 0)
        self.assertFalse(any(path == "/public/v1/chat/stream" for path in fixture.PublicFrontendHandler.request_paths))

    def test_invitation_survives_long_chat_reopen_and_stay_then_zero_model_join(self):
        self.open(); self.send("合成邀请去餐厅")
        self.page.locator('[data-current-rendezvous="stay"]').click()
        self.assertTrue(self.page.locator('[data-current-rendezvous="open"]').is_visible())
        for _ in range(5): self.send()
        self.page.reload(wait_until="networkidle")
        self.page.locator('[data-current-rendezvous="open"]').click()
        before = len(self.rows())
        self.send("我过去了")
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "in_person")
        self.assertIn("基地餐厅", self.page.locator("#stage-narration").inner_text())
        self.assertFalse(any("presence/arrival" in p for p in fixture.PublicFrontendHandler.request_paths))
        sidecar = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertTrue(any(receipt["memory"]["kind"] == "shared_scene_event" for receipt in sidecar["memories"]))

    def test_memory_transfer_limit_does_not_truncate_local_index(self):
        self.open()
        for _ in range(14): self.send()
        local = self.local()
        sidecar = next(row for row in local["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(len(sidecar["memories"]), 14)
        self.assertTrue(all(len(row["payload"]["memory_candidates"]) <= 12 for row in self.rows()))
        self.assertGreater(len(self.rows()[-1]["payload"]["memory_candidates"]), 0)
        self.assertTrue(all(context_bytes(row["payload"]) <= 8192 for row in self.rows()))

    def test_new_chat_and_scene_context_budget_keeps_large_receipts_only_in_local_index(self):
        self.open(); self.send()
        record = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        original_token = record["sessionContextPackage"]
        receipts = []
        for index in range(15):
            receipt = copy.deepcopy(record["memories"][0])
            receipt["memory"].update(memory_id=f"large-memory-{index:04d}", value="雪" * 160)
            receipt["proof"] = "s" * 1450
            receipts.append(receipt)
        self.page.evaluate("""receipts => new Promise(resolve => {
          const r = indexedDB.open('project-snow-public', 4);
          r.onsuccess = () => { const db = r.result, tx = db.transaction('app_state', 'readwrite');
            const store = tx.objectStore('app_state'), q = store.get('dialogue_context_v1:25b23cb64398');
            q.onsuccess = () => store.put({ ...q.result, memories: receipts });
            tx.oncomplete = () => { db.close(); resolve(); };
          };
        })""", receipts)
        count = len(self.rows("/public/v2/scene/action"))
        self.page.reload(wait_until="networkidle")
        scene = self.rows("/public/v2/scene/action")[count]["payload"]
        self.assertEqual(scene["session_context_package"], original_token)
        self.send("合成中文上下文预算")
        chat = self.rows()[-1]["payload"]
        for envelope in (scene, chat):
            with self.subTest(protocol=envelope["protocol"]):
                candidates = envelope["memory_candidates"]
                self.assertGreater(len(candidates), 0)
                self.assertLess(len(candidates), 12)
                self.assertLessEqual(context_bytes(envelope), 8192)
                self.assertEqual(candidates, list(reversed(receipts))[:len(candidates)])
                larger = {**envelope, "memory_candidates": list(reversed(receipts))[:len(candidates) + 1]}
                self.assertGreater(context_bytes(larger), 8192)
        saved = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(saved["memories"][:len(receipts)], receipts)
        self.assertEqual(len(saved["memories"]), len(receipts) + 1)
        self.assertFalse(self.errors)

    def test_legacy_pending_above_new_context_budget_recovers_original_envelope_after_reload(self):
        self.open(); DialogueHandler.scenario = "manual"; self.send()
        original = copy.deepcopy(self.rows()[0]["payload"])
        memory = copy.deepcopy(DialogueHandler.results[original["chat"]["request_id"]]["session_memories"][0])
        # Emulate a durable envelope written by the preceding client, which
        # allowed twelve large signed proofs before the total transfer cap.
        original["memory_candidates"] = []
        for index in range(12):
            receipt = copy.deepcopy(memory)
            receipt["memory"].update(memory_id=f"legacy-memory-{index:04d}", value="雪" * 160)
            receipt["proof"] = "s" * 1450
            original["memory_candidates"].append(receipt)
        self.assertGreater(context_bytes(original), 8192)
        self.page.evaluate("""envelope => new Promise(resolve => {
          const r = indexedDB.open('project-snow-public', 4);
          r.onsuccess = () => { const db = r.result, tx = db.transaction('messages', 'readwrite');
            const store = tx.objectStore('messages'), q = store.get(`user:${envelope.chat.request_id}`);
            q.onsuccess = () => store.put({ ...q.result, requestSnapshot: { endpoint: '/public/v2/chat/stream', envelope } });
            tx.oncomplete = () => { db.close(); resolve(); };
          };
        })""", original)
        self.page.reload(wait_until="networkidle")
        DialogueHandler.scenario = "success"
        self.recover_chat(original["chat"]["request_id"])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["recovery"])
        self.assertEqual(rows[1]["payload"], original)
        self.assertFalse(fixture.PublicFrontendHandler.chat_payloads)
        self.assertFalse(self.errors)

    def test_switch_contact_does_not_join_or_show_remote_face(self):
        self.open(); self.page.locator("#go-in-person").click(); self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person'")
        count = len(self.rows("/public/v2/scene/action"))
        self.page.locator(f'[data-character="{OTHER}"]').click()
        self.page.wait_for_function("document.querySelector('#active-character').textContent.includes('安卡希雅')")
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "text")
        new = self.rows("/public/v2/scene/action")[count:]
        self.assertTrue(new); self.assertTrue(all(row["payload"]["action"] == "resolve" for row in new))
        self.assertFalse(self.page.locator("#presence-dialog").is_visible())

    def test_old_v1_capability_off_keeps_old_chat_and_database(self):
        self.open(v2=False); self.send("旧版合成聊天")
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(len(fixture.PublicFrontendHandler.chat_payloads), 1)
        self.assertEqual(self.local()["version"], 4)
        self.assertFalse(self.errors)

    def test_scene_recovery_is_same_uuid_and_never_calls_arrival_model(self):
        self.open(); self.send("合成邀请去餐厅")
        DialogueHandler.scenario = "scene_disconnect"
        self.page.locator('[data-current-rendezvous="go"]').click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person'")
        joins = [row for row in self.rows("/public/v2/scene/action") if row["payload"]["action"] == "join_rendezvous"]
        self.assertEqual(len(joins), 2)
        self.assertFalse(joins[0]["recovery"]); self.assertTrue(joins[1]["recovery"])
        self.assertEqual(joins[0]["payload"], joins[1]["payload"])
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.errors)

    def test_manual_recovery_after_reload_keeps_v2_when_feature_is_disabled(self):
        self.open(); DialogueHandler.scenario = "manual"
        self.send()
        original = copy.deepcopy(self.rows()[0]["payload"])
        self.page.unroute("**/public/v1/config")
        def config(route):
            value = route.fetch().json()
            value["features"] = {"chat_latency_v2": True, "verified_block_replay": True, "motion_v2": True}
            route.fulfill(json=value)
        self.page.route("**/public/v1/config", config)
        self.page.reload(wait_until="networkidle")
        DialogueHandler.scenario = "success"
        self.recover_chat(original["chat"]["request_id"])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["recovery"])
        self.assertEqual(rows[1]["payload"], original)
        self.assertFalse(fixture.PublicFrontendHandler.chat_payloads)

    def test_cross_day_new_segment_retires_scene_context_but_keeps_index(self):
        for choice in ("continue_previous", "start_today"):
            with self.subTest(choice=choice):
                if choice == "continue_previous": self.open()
                self.send("合成邀请去餐厅")
                before = self.local()
                old_segment = next(row for row in before["threads"] if row["characterId"] == CHARACTER)["conversationSegmentId"]
                self.page.evaluate("""choice => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
                  const db=r.result;const tx=db.transaction(['threads','app_state'],'readwrite');const s=tx.objectStore('threads');const q=s.get('25b23cb64398');
                  q.onsuccess=()=>s.put({...q.result,localDayKey:'2000-01-01'});
                  tx.objectStore('app_state').put({key:'preferences',dayContinuityPreference:choice,autoSummaryEnabled:false});tx.oncomplete=()=>{db.close();resolve();};};})""", choice)
                count = len(self.rows("/public/v2/scene/action"))
                self.page.reload(wait_until="networkidle")
                resolves = self.rows("/public/v2/scene/action")[count:]
                self.assertTrue(resolves)
                envelope = resolves[0]["payload"]
                self.assertEqual(envelope["continuity_decision"], choice)
                self.assertEqual(envelope["segment_id"] == old_segment, choice == "continue_previous")
                self.assertEqual(bool(envelope["session_context_package"]), choice == "continue_previous")
                local = self.local()
                sidecar = next(row for row in local["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
                self.assertGreaterEqual(len(sidecar["memories"]), 1)
                self.assertEqual(self.page.locator("#current-rendezvous").is_visible(), choice == "continue_previous")

    def test_retention_removes_expired_receipt_and_opaque_memory(self):
        self.open(); self.send()
        self.page.evaluate("""() => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
          const db=r.result;const tx=db.transaction('app_state','readwrite');const s=tx.objectStore('app_state');const q=s.get('dialogue_context_v1:25b23cb64398');
          q.onsuccess=()=>{q.result.memories[0].memory.occurred_at='2000-01-01T00:00:00Z';s.put(q.result);};tx.oncomplete=()=>{db.close();resolve();};};})""")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#open-settings").click(); self.page.locator("#settings-tab-history").click()
        self.page.locator("#history-retention").select_option("30")
        self.page.wait_for_timeout(300)
        sidecar = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(sidecar["memories"], [])
        self.assertEqual(sidecar["sessionContextPackage"], "")
        self.page.locator('[data-close-dialog="settings-dialog"]').click()
        self.send()
        self.assertEqual(self.rows()[-1]["payload"]["memory_candidates"], [])
        self.assertEqual(self.rows()[-1]["payload"]["session_context_package"], "")

    def test_mobile_cancel_and_mixed_join_text_remain_distinct(self):
        self.open()
        self.page.set_viewport_size({"width": 390, "height": 760})
        self.send("合成邀请去餐厅")
        count = len(self.rows())
        self.send("我过去了，顺便聊聊原作这件事")
        self.assertEqual(len(self.rows()), count + 1)
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "text")
        self.page.locator('[data-current-rendezvous="cancel"]').click()
        self.page.wait_for_function("document.querySelector('#current-rendezvous').hidden")
        self.assertEqual(len(self.rows()), count + 1)
        self.assertTrue(any(row["payload"]["action"] == "cancel_rendezvous" for row in self.rows("/public/v2/scene/action")))
        self.assertFalse(self.errors)

    def test_canon_sources_are_collapsed_and_escape_text_without_private_fields(self):
        self.open(); self.send("聊聊合成原作")
        sources = self.page.locator("#timeline .evidence-sources")
        self.assertEqual(sources.count(), 1)
        self.assertIsNone(sources.get_attribute("open"))
        sources.locator("summary").click()
        self.assertIn("<img src=x>", sources.inner_text())
        self.assertEqual(sources.locator("img").count(), 0)
        assistant = next(row for row in self.local()["messages"] if row["role"] == "assistant")
        self.assertEqual(set(assistant["evidenceSources"][0]), {"document_id", "title", "source_type"})

    def test_quota_fault_after_terminal_keeps_completed_reply_and_memory_in_session(self):
        self.open()
        self.page.evaluate("""() => {const original=IDBDatabase.prototype.transaction;
          IDBDatabase.prototype.transaction=function(names,mode,...rest){
            if(mode==='readwrite' && Array.from(typeof names==='string'?[names]:names).includes('app_state'))throw new DOMException('synthetic quota','QuotaExceededError');
            return original.call(this,names,mode,...rest);};}""")
        self.send()
        self.assertEqual(self.page.locator("#timeline .message.assistant").count(), 1)
        self.assertEqual(self.page.locator("[data-retry-message]").count(), 0)
        self.assertIn("本次聊天不会保存", self.page.locator("#system-banner").inner_text())
        self.send()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.rows()[-1]["payload"]["memory_candidates"]), 1)
        self.assertFalse(self.errors)

    def test_backup_omits_context_signatures_and_v2_recovery_envelopes(self):
        self.open(); self.send()
        DialogueHandler.scenario = "manual"; self.send("合成未确认请求")
        self.page.locator("#open-settings").click(); self.page.locator("#settings-tab-history").click()
        with self.page.expect_download() as pending:
            self.page.locator("#export-history").click()
        backup = json.loads(Path(pending.value.path()).read_text(encoding="utf-8"))
        self.assertTrue(backup["messages"])
        self.assertTrue(all(not message.get("requestSnapshot") for message in backup["messages"]))
        self.assertFalse(any(row["key"].startswith("dialogue_context_v1:") for row in backup["appState"]))
        self.assertNotIn("session_context_package", json.dumps(backup))

    def test_old_v4_delete_does_not_restore_orphaned_experiences(self):
        self.open(); self.send()
        self.page.evaluate("""() => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
          const db=r.result;const tx=db.transaction(['threads','messages'],'readwrite');tx.objectStore('threads').delete('25b23cb64398');
          const q=tx.objectStore('messages').openCursor();q.onsuccess=()=>{const cursor=q.result;if(!cursor)return;
            if(cursor.value.characterId==='25b23cb64398')cursor.delete();cursor.continue();};tx.oncomplete=()=>{db.close();resolve();};};})""")
        count = len(self.rows("/public/v2/scene/action"))
        self.page.reload(wait_until="networkidle")
        resolves = self.rows("/public/v2/scene/action")[count:]
        self.assertEqual(resolves[0]["payload"]["memory_candidates"], [])
        self.assertEqual(resolves[0]["payload"]["session_context_package"], "")
        sidecar = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(sidecar["memories"], [])
        self.assertEqual(self.local()["version"], 4)

    def test_v2_scene_actions_do_not_inherit_legacy_wait_with_motion_flag_off(self):
        self.open(motion=False)
        self.page.emulate_media(reduced_motion="no-preference")
        self.page.evaluate("""() => {window.__durations=[];const original=Element.prototype.animate;
          Element.prototype.animate=function(frames,options){window.__durations.push(options?.duration);return original.call(this,frames,options);};}""")
        self.page.locator("#go-in-person").click(); self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")
        self.page.evaluate("document.querySelector('#open-communicator').addEventListener('click',()=>window.__openedAt=performance.now(),{capture:true,once:true})")
        self.page.locator("#open-communicator").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='text' && !document.querySelector('#send-message').disabled")
        elapsed = self.page.evaluate("performance.now()-window.__openedAt")
        self.assertLess(elapsed, 500)
        self.assertTrue(self.page.locator("#mode-transition-layer").is_hidden())
        self.assertTrue(self.page.locator("#scene-transition-layer").is_hidden())
        self.assertIn(360, self.page.evaluate("window.__durations"))
        self.assertFalse(self.errors)

    def test_recovering_retired_segment_keeps_history_and_cannot_restore_old_scene(self):
        self.open(); DialogueHandler.scenario = "manual"
        self.send("合成旧段未确认消息")
        original = copy.deepcopy(self.rows()[0]["payload"])
        self.page.evaluate("""() => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
          const db=r.result;const tx=db.transaction('threads','readwrite');const s=tx.objectStore('threads');const q=s.get('25b23cb64398');
          q.onsuccess=()=>s.put({...q.result,conversationSegmentId:'b02d49b6-5e89-4140-9b9e-8d816768ab26'});tx.oncomplete=()=>{db.close();resolve();};};})""")
        self.page.reload(wait_until="networkidle")
        before = self.local()
        context_before = next(row for row in before["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")["sessionContextPackage"]
        world_before = next(row for row in before["state"] if row["key"] == "world")["statePackage"]
        DialogueHandler.scenario = "success"
        self.recover_chat(original["chat"]["request_id"])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["recovery"]); self.assertEqual(rows[1]["payload"], original)
        after = self.local()
        messages = [row for row in after["messages"] if row["requestId"] == original["chat"]["request_id"]]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(row["conversationSegmentId"] == original["segment_id"] for row in messages))
        self.assertEqual(next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")["sessionContextPackage"], context_before)
        self.assertEqual(next(row for row in after["state"] if row["key"] == "world")["statePackage"], world_before)
        self.assertFalse(self.errors)

    def test_definite_context_rejection_retires_only_context_and_keeps_frozen_chat(self):
        self.open(); self.send()
        before = self.local()
        old_context = next(row for row in before["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        DialogueHandler.scenario = "context_rejected"
        self.send("合成失效凭证请求")
        failed_envelope = copy.deepcopy(self.rows()[-1]["payload"])
        after = self.local()
        retired = next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(retired["sessionContextPackage"], "")
        self.assertEqual(retired["memories"], old_context["memories"])
        failed = next(row for row in after["messages"] if row["requestId"] == failed_envelope["chat"]["request_id"])
        self.assertEqual(failed["requestSnapshot"]["envelope"]["session_context_package"], failed_envelope["session_context_package"])
        self.assertEqual(len(self.rows()), 2)
        self.page.locator(".request-recovery").click()
        self.page.wait_for_function("!document.querySelector('#send-message').disabled")
        self.assertEqual(len(self.rows()), 2)
        self.send("合成修复后新消息")
        self.assertEqual(len(self.rows()), 3)
        self.assertNotEqual(self.rows()[-1]["payload"]["chat"]["request_id"], failed_envelope["chat"]["request_id"])
        self.assertFalse(self.errors)

    def test_cookie_scope_change_reestablishes_scene_without_model_or_history_loss(self):
        self.open(); self.send("合成邀请去餐厅")
        before = self.local()
        old_memories = next(row for row in before["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")["memories"]
        count = len(self.rows("/public/v2/scene/action"))
        DialogueHandler.scenario = "scope_changed"
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#active-character').textContent.includes('凯茜娅')")
        resolves = self.rows("/public/v2/scene/action")[count:]
        self.assertEqual(len(resolves), 3)
        self.assertTrue(all(row["payload"]["action"] == "resolve" and not row["recovery"] for row in resolves))
        self.assertEqual(resolves[-1]["payload"]["session_context_package"], "")
        self.assertEqual(resolves[-1]["payload"]["state_package"], "")
        after = self.local()
        self.assertEqual(len(after["messages"]), len(before["messages"]))
        self.assertEqual(next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")["memories"], old_memories)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.page.locator("#current-rendezvous").is_hidden())
        self.assertFalse(self.errors)

    def test_future_invitation_can_reopen_and_cancel_without_join_or_fact_memory(self):
        self.open(); self.send("合成未来邀请 accepted 明天去餐厅")
        card = self.page.locator("#current-rendezvous")
        self.assertIn("基地餐厅", card.inner_text())
        self.assertIn("时间待定", card.inner_text())
        self.assertEqual(card.locator('[data-current-rendezvous="go"]').count(), 0)
        record = next(row for row in self.local()["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertEqual(record["memories"], [])
        invite = untoken(record["sessionContextPackage"])["active_invitation"]
        self.page.locator('[data-current-rendezvous="stay"]').click()
        self.assertTrue(self.page.locator('[data-current-rendezvous="open"]').is_visible())
        self.page.wait_for_function("""key => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);
          r.onsuccess=()=>{const db=r.result;const q=db.transaction('app_state','readonly').objectStore('app_state').get('ui_preferences');
          q.onsuccess=()=>{resolve((q.result?.rendezvousDismissals||[]).includes(key));db.close();};};})""", arg=invite["invitation_id"])
        self.page.reload(wait_until="networkidle")
        self.page.locator('[data-current-rendezvous="open"]').click()
        self.assertIn("时间待定", card.inner_text())
        count = len(self.rows())
        self.send("我过去了")
        self.assertEqual(len(self.rows()), count + 1)
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "text")
        self.assertEqual(card.locator('[data-current-rendezvous="go"]').count(), 0)
        self.page.locator('[data-current-rendezvous="cancel"]').click()
        self.page.wait_for_function("document.querySelector('#current-rendezvous').hidden")
        cancellation = self.rows("/public/v2/scene/action")[-1]
        self.assertEqual(cancellation["payload"]["action"], "cancel_rendezvous")
        self.assertEqual(cancellation["payload"]["rendezvous_id"], invite["invitation_id"])
        self.assertFalse(any(row["payload"]["action"] in {"join_character", "join_rendezvous"} for row in self.rows("/public/v2/scene/action")))
        self.assertEqual(len(self.rows()), count + 1)
        self.assertFalse(self.errors)

    def test_proposed_deferred_and_clarify_invitation_cards_never_offer_join(self):
        self.open()
        for status in ("proposed", "deferred", "clarify"):
            with self.subTest(status=status):
                self.send(f"合成未来邀请 {status} 以后再去餐厅")
                card = self.page.locator("#current-rendezvous")
                self.assertTrue(card.is_visible())
                self.assertIn("时间待定", card.inner_text())
                self.assertEqual(card.locator('[data-current-rendezvous="go"]').count(), 0)
                self.page.locator('[data-current-rendezvous="cancel"]').click()
                self.page.wait_for_function("document.querySelector('#current-rendezvous').hidden")
        self.assertFalse(self.errors)

    def test_old_v4_delete_retires_future_or_memory_disabled_context_without_other_world_deletion(self):
        self.open()
        for mode in ("future", "memory_disabled"):
            with self.subTest(mode=mode):
                if mode == "future": self.send("合成未来邀请 accepted 明天去餐厅")
                else:
                    DialogueHandler.memory_enabled = False
                    self.send("合成不生成经历的普通聊天")
                before = self.local()
                old_record = next(row for row in before["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
                self.assertEqual(old_record["memories"], [])
                self.assertTrue(old_record["sessionContextPackage"])
                old_world = untoken(next(row for row in before["state"] if row["key"] == "world")["statePackage"])
                self.page.evaluate("""() => new Promise(resolve=>{const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
                  const db=r.result;const tx=db.transaction(['threads','messages'],'readwrite');tx.objectStore('threads').delete('25b23cb64398');
                  const q=tx.objectStore('messages').openCursor();q.onsuccess=()=>{const cursor=q.result;if(!cursor)return;
                    if(cursor.value.characterId==='25b23cb64398')cursor.delete();cursor.continue();};tx.oncomplete=()=>{db.close();resolve();};};})""")
                count = len(self.rows("/public/v2/scene/action"))
                self.page.reload(wait_until="networkidle")
                resolves = self.rows("/public/v2/scene/action")[count:]
                self.assertEqual(resolves[0]["payload"]["session_context_package"], "")
                self.assertEqual(resolves[0]["payload"]["memory_candidates"], [])
                after = self.local()
                record = next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
                self.assertEqual(record["memories"], [])
                self.assertIsNone(untoken(record["sessionContextPackage"]).get("active_invitation"))
                self.assertTrue(self.page.locator("#current-rendezvous").is_hidden())
                new_world = untoken(next(row for row in after["state"] if row["key"] == "world")["statePackage"])
                self.assertEqual(new_world["presence"][OTHER], old_world["presence"][OTHER])
                self.assertEqual(after["version"], 4)
        self.assertFalse(self.errors)

    def test_face_future_invitation_card_is_visible_and_cancel_keeps_confirmed_location(self):
        self.open()
        self.page.locator("#go-in-person").click(); self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")
        before_location = self.page.locator("#stage-location").inner_text()
        self.send("合成未来邀请 accepted 明天去餐厅")
        card = self.page.locator("#current-rendezvous")
        self.assertTrue(card.is_visible())
        self.assertEqual(card.evaluate("node=>node.parentElement.id"), "composer")
        self.assertEqual(card.locator('[data-current-rendezvous="go"]').count(), 0)
        self.page.locator('[data-current-rendezvous="stay"]').focus()
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.evaluate("document.activeElement.dataset.currentRendezvous"), "open")
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.evaluate("document.activeElement.dataset.currentRendezvous"), "cancel")
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("document.querySelector('#current-rendezvous').hidden")
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "in_person")
        self.assertEqual(self.page.locator("#stage-location").inner_text(), before_location)
        self.page.wait_for_function("document.activeElement.id==='message-input'")
        self.assertFalse(self.errors)

    def _arrival_notices(self, snow):
        self.open(snow=snow)
        self.page.locator("#go-in-person").click(); self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person'")
        self.page.wait_for_timeout(400)
        return self.page.evaluate("""() => {
          const shown = id => { const node = document.getElementById(id); return node && !node.hidden && !node.closest('[hidden]') ? node.textContent.trim() : ''; };
          return { banner: shown('system-banner'), status: shown('request-status'), narration: shown('stage-narration') + shown('stage-speech') };
        }""")

    def test_snow_adv_scene_arrival_keeps_a_single_notice(self):
        legacy = self._arrival_notices(snow=False)
        self.assertFalse(self.errors)
        self.page.close()
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 850}, reduced_motion="reduce")
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        snow = self._arrival_notices(snow=True)
        # Flag off keeps whatever the legacy path shows; flag on drops the duplicate banner/status only.
        self.assertTrue("已来到" in legacy["banner"] or "已来到" in legacy["status"], legacy)
        self.assertNotIn("已来到", snow["banner"])
        self.assertNotIn("已来到", snow["status"])
        self.assertNotIn("正在确认会合地点", snow["status"])
        self.assertEqual(bool(legacy["narration"]), bool(snow["narration"]))
        self.assertFalse(self.errors)

    def test_first_join_pending_without_history_recovers_original_uuid_before_orphan_cleanup(self):
        self.open()
        self.assertEqual(self.local()["messages"], [])
        DialogueHandler.scenario = "first_join_pending"
        self.page.locator("#go-in-person").click(); self.page.locator("#confirm-presence-transition").click()
        self.assertTrue(DialogueHandler.join_started.wait(timeout=5))
        pending = self.local()
        self.assertEqual(pending["messages"], [])
        record = next(row for row in pending["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        original = record["sceneActionSnapshot"]["envelope"]
        self.assertTrue(record["sessionContextPackage"])
        self.assertTrue(any(row["conversationSegmentId"] == original["segment_id"] for row in pending["threads"]))
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person'")
        joins = [row for row in self.rows("/public/v2/scene/action") if row["payload"]["action"] == "join_character"]
        self.assertEqual(len(joins), 2)
        self.assertFalse(joins[0]["recovery"]); self.assertTrue(joins[1]["recovery"])
        self.assertEqual(joins[1]["payload"], original)
        after = self.local()
        self.assertTrue(any(row["id"] == f"reply:{original['request_id']}" for row in after["messages"]))
        record = next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertIsNone(record["sceneActionSnapshot"])
        self.assertEqual(len(self.rows()), 0)
        self.assertFalse(self.errors)

    def test_scene_recovery_conflict_clears_only_snapshot_without_silent_action(self):
        self.open(); self.send()
        DialogueHandler.scenario = "manual"; self.send("合成未确认聊天保留快照")
        DialogueHandler.scenario = "scene_disconnect"
        self.page.route("**/public/v2/scene/action", lambda route: route.abort("failed"))
        self.page.locator("#go-in-person").click()
        self.page.wait_for_function("!document.querySelector('#go-in-person').disabled")
        before = self.local()
        old_context = next(row for row in before["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertIsNotNone(old_context["sceneActionSnapshot"])
        old_world = next(row for row in before["state"] if row["key"] == "world")["statePackage"]
        paid_snapshot = next(row for row in before["messages"] if row["status"] == "failed")["requestSnapshot"]
        count = len(self.rows("/public/v2/scene/action"))
        self.page.unroute("**/public/v2/scene/action")
        DialogueHandler.scenario = "scene_conflict"
        self.page.locator("#go-in-person").click()
        self.page.wait_for_function("!document.querySelector('#go-in-person').disabled")
        after = self.local()
        record = next(row for row in after["state"] if row["key"] == f"dialogue_context_v1:{CHARACTER}")
        self.assertIsNone(record["sceneActionSnapshot"])
        self.assertEqual(record["sessionContextPackage"], old_context["sessionContextPackage"])
        self.assertEqual(record["memories"], old_context["memories"])
        self.assertEqual(next(row for row in after["state"] if row["key"] == "world")["statePackage"], old_world)
        self.assertEqual(next(row for row in after["messages"] if row["status"] == "failed")["requestSnapshot"], paid_snapshot)
        recovery = self.rows("/public/v2/scene/action")[count:]
        self.assertEqual(len(recovery), 1)
        self.assertTrue(recovery[0]["recovery"])
        self.assertEqual(recovery[0]["payload"], old_context["sceneActionSnapshot"]["envelope"])
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(self.errors)


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvVeilProductionTests(TestCase):
    """P4 后续：生产态（motion_v2=true, scene_actions_v2=true）下的独立白雪转场遮罩。

    复用本模块的 DialogueHandler 夹具与 open() 的配置形状（snow=True 开启皮肤且保持
    motion_v2/scene_actions_v2 为真），每个用例自建页面（按需设置 reduced-motion）。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DialogueHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        DialogueHandler.requests = []; DialogueHandler.results = {}; DialogueHandler.counter = 0
        DialogueHandler.scope_rejected = False
        DialogueHandler.memory_enabled = True
        DialogueHandler.join_started = threading.Event()
        DialogueHandler.join_release = threading.Event()
        DialogueHandler.scenario = "success"
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors = []
        self.page = None

    def tearDown(self):
        DialogueHandler.join_release.set()
        self.browser.close(); self.playwright.stop()

    def _open(self, width=1200, height=850, reduced_motion="no-preference", motion_pref=None, snow=True):
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion=reduced_motion)
        page.set_default_timeout(10000)
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        if motion_pref is not None:
            page.add_init_script(f"window.localStorage.setItem('project-snow-public:motion', {json.dumps(motion_pref)})")
        self.page = page

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {"chat_latency_v2": True, "motion_v2": True, "verified_block_replay": True,
                "dialogue_domains_v2": True, "scene_actions_v2": True, "session_memory_v2": True, "ui_snow_adv": snow}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2", "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            route.fulfill(json=payload)

        page.route("**/public/v1/config", config)
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()

    def _reach_in_person(self):
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.locator("#in-person-surface:not([hidden])").wait_for()

    def _veil(self):
        return self.page.evaluate("""() => {
          const veil = document.getElementById('snow-scene-veil');
          if (!veil) return { present: false };
          const title = veil.querySelector('.snow-scene-veil-title');
          const sub = veil.querySelector('.snow-scene-veil-sub');
          return {
            present: true,
            phase: veil.dataset.phase,
            opacity: Number(getComputedStyle(veil).opacity),
            ariaHidden: veil.getAttribute('aria-hidden'),
            pointerEvents: getComputedStyle(veil).pointerEvents,
            titleText: title ? title.textContent : '',
            titleFont: title ? getComputedStyle(title).fontFamily : '',
            titleSize: title ? parseFloat(getComputedStyle(title).fontSize) : 0,
            subText: sub ? sub.textContent : '',
          };
        }""")

    def test_veil_plays_with_location_title_on_entering_in_person(self):
        self._open()
        # 进入面对面（从文字进入已知地点 观景区）应播放遮罩。
        self._reach_in_person()
        self.page.wait_for_function(
            "document.getElementById('snow-scene-veil') && document.getElementById('snow-scene-veil').dataset.phase !== 'idle'",
            timeout=4000)
        # 等遮罩真正升到可见（淡入+保持 600ms），再读属性，避免采在淡入起点。
        self.page.wait_for_function(
            "Number(getComputedStyle(document.getElementById('snow-scene-veil')).opacity) > 0.6", timeout=4000)
        veil = self._veil()
        self.assertTrue(veil["present"])
        self.assertEqual(veil["titleText"], "观景区")
        self.assertIn("LXGW WenKai Screen", veil["titleFont"])
        self.assertGreaterEqual(veil["titleSize"], 28)
        self.assertEqual(veil["ariaHidden"], "true")
        self.assertEqual(veil["pointerEvents"], "none")
        self.assertGreater(veil["opacity"], 0.5)
        self.page.wait_for_function(
            "document.getElementById('snow-scene-veil').dataset.phase === 'idle'", timeout=4000)
        self.assertLess(self._veil()["opacity"], 0.05)
        self.assertFalse(self.errors)

    def test_real_arrival_push_exposes_exterior_before_destination(self):
        self._open()
        self._reach_in_person()
        self.page.locator('#chat-app[data-snow-pushing="1"]').wait_for()
        self.page.wait_for_timeout(250)
        push = self.page.evaluate("""() => {
          const stage = document.getElementById('scene-stage');
          const backdrop = document.getElementById('snow-backdrop-stage').snowWorld;
          const target = backdrop.room.querySelector('.push-target').getBoundingClientRect();
          return {
            scene: backdrop.scene, roomHidden: backdrop.room.hidden,
            targetWidth: target.width, targetHeight: target.height,
            transform: getComputedStyle(backdrop.room).transform,
            stageBackground: getComputedStyle(stage).backgroundColor,
            surfaceBackground: getComputedStyle(document.getElementById('in-person-surface')).backgroundColor,
            stageWorldVisibility: getComputedStyle(stage.querySelector('[data-snow-world]')).visibility,
            activeLoops: window.__projectSnowTest.snowProbe().activeLoops,
            stageRunning: stage.snowWorld.isRunning(), backdropRunning: backdrop.isRunning(),
          };
        }""")
        self.assertEqual(push["scene"], "generic")
        self.assertFalse(push["roomHidden"])
        self.assertGreater(push["targetWidth"], 0)
        self.assertGreater(push["targetHeight"], 0)
        self.assertNotIn(push["transform"], ("none", "matrix(1, 0, 0, 1, 0, 0)"))
        self.assertEqual(push["stageBackground"], "rgba(0, 0, 0, 0)")
        self.assertEqual(push["surfaceBackground"], "rgba(0, 0, 0, 0)")
        self.assertEqual(push["stageWorldVisibility"], "hidden")
        self.assertEqual(push["activeLoops"], 1)
        self.assertFalse(push["stageRunning"])
        self.assertTrue(push["backdropRunning"])
        self.page.wait_for_function("document.querySelector('#snow-scene-veil .snow-scene-veil-title').textContent === '观景区'")
        self.page.wait_for_function("!document.getElementById('chat-app').dataset.snowPushing")
        self.assertEqual(self.page.evaluate("window.__projectSnowTest.snowProbe().worldScene"), "observation")
        self.assertEqual(self.page.evaluate("window.__projectSnowTest.snowProbe().activeLoops"), 1)
        self.assertFalse(self.errors)

    def test_reduced_motion_during_push_cancels_camera_and_pending_title(self):
        self._open()
        self._reach_in_person()
        self.page.locator('#chat-app[data-snow-pushing="1"]').wait_for()
        self.page.emulate_media(reduced_motion="reduce")
        self.page.wait_for_function("!document.getElementById('chat-app').dataset.snowPushing && window.__projectSnowTest.snowProbe().activeLoops === 0")
        self.page.wait_for_timeout(1900)
        self.assertEqual(self._veil()["phase"], "idle")
        self.assertEqual(self._veil()["titleText"], "")
        self.assertFalse(self.page.evaluate("window.__projectSnowTest.snowProbe().pushing"))
        self.assertFalse(self.errors)

    def test_opening_communicator_during_push_cancels_pending_title(self):
        self._open()
        self._reach_in_person()
        self.page.locator('#chat-app[data-snow-pushing="1"]').wait_for()
        self.page.locator("#open-communicator").click()
        self.page.locator("#text-surface:not([hidden])").wait_for()
        self.page.wait_for_timeout(1900)
        self.assertEqual(self._veil()["phase"], "idle")
        self.assertEqual(self._veil()["titleText"], "")
        self.assertFalse(self.page.evaluate("window.__projectSnowTest.snowProbe().pushing"))
        self.assertEqual(self.page.evaluate("window.__projectSnowTest.snowProbe().activeLoops"), 1)
        self.assertFalse(self.errors)

    def test_communicator_snow_reacts_to_live_motion_preferences(self):
        self._open()
        self.assertEqual(self.page.evaluate("window.__projectSnowTest.snowProbe().activeLoops"), 1)
        self.page.locator("#open-settings").click()
        self.page.locator("#settings-tab-about").click()
        for motion, loops in (("light", 0), ("off", 0), ("auto", 1)):
            self.page.locator("#motion-preference").select_option(motion)
            self.page.wait_for_function(f"window.__projectSnowTest.snowProbe().activeLoops === {loops}")
        self.page.emulate_media(reduced_motion="reduce")
        self.page.wait_for_function("window.__projectSnowTest.snowProbe().activeLoops === 0")
        self.page.emulate_media(reduced_motion="no-preference")
        self.page.wait_for_function("window.__projectSnowTest.snowProbe().activeLoops === 1")
        self.assertFalse(self.errors)

    def test_light_first_arrival_does_not_replay_camera_when_auto_returns(self):
        self._open(motion_pref="light")
        self._reach_in_person()
        self.page.wait_for_function("document.getElementById('snow-scene-veil').dataset.phase === 'idle'")
        self.page.locator("#open-communicator").click()
        self.page.locator("#text-surface:not([hidden])").wait_for()
        self.page.locator("#open-settings").click()
        self.page.locator("#settings-tab-about").click()
        self.page.locator("#motion-preference").select_option("auto")
        self.assertEqual(self.page.evaluate("window.__projectSnowTest.snowProbe().backdropScene"), "observation")
        self.assertFalse(self.page.evaluate("window.__projectSnowTest.snowProbe().pushPlayed"))
        self.assertFalse(self.errors)

    def test_veil_does_not_replay_when_reopening_communicator_to_same_location(self):
        self._open()
        self._reach_in_person()
        # 首次入室在桌面 auto 档会先播镜头推进，veil 比直接进入晚约 1.3s 升起；先等它真正
        # 升起（phase=up）再回到 idle，确保首播已完成，避免把首播误计为「重播」。
        self.page.wait_for_function(
            "document.getElementById('snow-scene-veil').dataset.phase === 'up'", timeout=6000)
        self.page.wait_for_function(
            "document.getElementById('snow-scene-veil').dataset.phase === 'idle'", timeout=5000)
        self.page.evaluate("() => { window.__veilReplayed = false; const v = document.getElementById('snow-scene-veil');"
                           " new MutationObserver(() => { if (v.dataset.phase === 'up') window.__veilReplayed = true; })"
                           " .observe(v, { attributes: true, attributeFilter: ['data-phase'] }); }")
        # 回到通讯器（文字），再返回同一地点：不应重播遮罩。
        self.page.locator("#open-communicator").click()
        self.page.locator("#text-surface:not([hidden])").wait_for()
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.locator("#in-person-surface:not([hidden])").wait_for()
        self.page.wait_for_timeout(900)
        self.assertFalse(self.page.evaluate("() => window.__veilReplayed"),
                         "returning to the same location must not replay the veil")
        self.assertFalse(self.errors)

    def test_veil_never_plays_at_motion_off(self):
        self._open(reduced_motion="reduce")
        self.page.wait_for_function("document.getElementById('snow-scene-veil')", timeout=4000)
        self.page.evaluate("() => { window.__veilEverUp = false; const v = document.getElementById('snow-scene-veil');"
                           " new MutationObserver(() => { if (v.dataset.phase !== 'idle') window.__veilEverUp = true; })"
                           " .observe(v, { attributes: true, attributeFilter: ['data-phase'] }); }")
        self._reach_in_person()
        self.page.wait_for_timeout(900)
        self.assertFalse(self.page.evaluate("() => window.__veilEverUp"),
                         "no veil at motion off / reduced motion")
        self.assertEqual(self._veil()["phase"], "idle")
        self.assertFalse(self.errors)

    def test_no_veil_element_when_flag_off(self):
        self._open(snow=False)
        self._reach_in_person()
        self.page.wait_for_timeout(400)
        self.assertFalse(self._veil()["present"], "no veil element exists when the flag is off")
        self.assertFalse(self.errors)

