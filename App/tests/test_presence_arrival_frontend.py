"""Synthetic arrival generation/recovery contracts; never contact a provider."""
from __future__ import annotations

import copy
import os
from unittest import TestCase, skipUnless

from tests import test_dialogue_v2_frontend as fixture


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class PresenceArrivalFrontendTests(TestCase):
    setUpClass = classmethod(fixture.DialogueV2FrontendTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.DialogueV2FrontendTests.tearDownClass.__func__)
    tearDown = fixture.DialogueV2FrontendTests.tearDown
    open = fixture.DialogueV2FrontendTests.open
    local = fixture.DialogueV2FrontendTests.local
    rows = fixture.DialogueV2FrontendTests.rows

    def setUp(self):
        fixture.DialogueV2FrontendTests.setUp(self)
        self.arrivals = []
        self.arrival_results = {}
        self.generations = 0
        self.lose_responses = False

        def scene(route):
            response = route.fetch()
            payload = response.json()
            if payload.get("scene_action", {}).get("status") == "joined":
                payload["arrival_ticket"] = "synthetic-ticket:" + payload["request_id"]
            route.fulfill(response=response, json=payload)

        def arrival(route):
            payload = route.request.post_data_json
            recovery = route.request.headers.get("x-snow-recovery-only") == "1"
            self.arrivals.append({"payload": copy.deepcopy(payload), "recovery": recovery})
            request_id = payload["arrival_id"]
            if request_id not in self.arrival_results:
                if recovery:
                    route.fulfill(status=410, json={"detail": {"code": "request_result_unavailable", "retryable": False}})
                    return
                self.generations += 1
                self.arrival_results[request_id] = {"arrival_id": request_id, "character_id": payload["character_id"],
                    "communication_channel": "in_person", "decision": "noticed", "model_called": True,
                    "reaction": {"message_id": "arrival:" + request_id, "content_blocks": [{"type": "speech", "text": "合成到场问候。"}]},
                    "usage": {"provider_calls": 1, "total_tokens": 12}}
            if self.lose_responses:
                route.fulfill(status=503, json={"detail": {"code": "provider_network_error", "retryable": True}})
                return
            route.fulfill(json=self.arrival_results[request_id])

        self.page.route("**/public/v2/scene/action", scene)
        self.page.route("**/public/v2/presence/arrival", arrival)

    def enter(self):
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")

    def test_each_real_entry_once_refresh_and_sidebar_never_generate(self):
        self.open(snow=True)
        self.enter()
        self.assertEqual(self.generations, 1)
        join = [r for r in self.rows("/public/v2/scene/action") if r["payload"]["action"] == "join_character"][-1]
        self.assertEqual(self.arrivals[0]["payload"]["arrival_id"], join["payload"]["request_id"])
        self.page.locator("#open-stage-contacts").click()
        self.page.locator("#close-contacts").click()
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person'")
        self.assertEqual(self.generations, 1)
        self.page.locator("#open-communicator").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='text'")
        self.enter()
        self.assertEqual(self.generations, 2)
        self.assertNotEqual(self.arrivals[0]["payload"]["arrival_id"], self.arrivals[-1]["payload"]["arrival_id"])
        local = self.local()
        self.assertEqual(local["version"], 4)
        self.assertEqual(len([m for m in local["messages"] if m.get("source") == "presence_arrival"]), 2)
        self.assertFalse([m for m in local["messages"] if m["role"] == "user"])
        self.assertFalse(self.errors)

    def test_face_character_switch_joins_new_role_and_closes_drawer(self):
        self.open(snow=True)
        self.enter()
        self.page.locator("#open-stage-contacts").click()
        self.page.locator(f'[data-character="{fixture.OTHER}"]').click()
        self.page.wait_for_function("document.querySelector('#active-character').textContent.includes('安卡希雅') && document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")
        self.assertEqual(self.generations, 2)
        self.assertEqual(self.arrivals[-1]["payload"]["character_id"], fixture.OTHER)
        self.assertEqual(self.page.locator("#contact-panel").get_attribute("aria-hidden"), "true")
        self.assertFalse(self.errors)

    def test_lost_result_reload_recovers_original_snapshot_without_new_generation(self):
        self.open(snow=True)
        self.lose_responses = True
        self.enter()
        self.assertEqual(self.generations, 1)
        original = copy.deepcopy(self.arrivals[0]["payload"])
        before = self.local()
        record = next(r for r in before["state"] if r["key"] == f"dialogue_context_v1:{fixture.CHARACTER}")
        self.assertNotIn("credential", record["arrivalSnapshot"]["envelope"])
        self.assertTrue(record["arrivalSnapshot"]["dispatched"])
        self.assertEqual(record["arrivalSnapshot"]["status"], "recoverable")
        self.lose_responses = False
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#stage-speech').textContent.includes('合成到场问候')")
        self.assertEqual(self.generations, 1)
        self.assertTrue(all(row["recovery"] for row in self.arrivals[1:]))
        self.assertTrue(all(row["payload"] == original for row in self.arrivals))
        after = self.local()
        self.assertEqual(len([m for m in after["messages"] if m.get("source") == "presence_arrival"]), 1)
        self.assertIsNone(next(r for r in after["state"] if r["key"] == record["key"])["arrivalSnapshot"])
        self.assertFalse(self.errors)

    def test_missing_recovery_result_never_reissues_paid_generation(self):
        self.open(snow=True)
        self.lose_responses = True
        self.enter()
        self.arrival_results.clear()
        self.lose_responses = False
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#request-status').textContent.includes('无法恢复')")
        self.assertEqual(self.generations, 1)
        self.assertTrue(self.arrivals[-1]["recovery"])
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "in_person")
        self.assertEqual(self.page.locator("#request-status .request-recovery").count(), 0)
        self.assertFalse(self.errors)

    def test_unsent_snapshot_survives_dispatch_storage_fault_and_resumes_same_entry(self):
        self.open(snow=True)
        self.page.evaluate("""() => {
          const put=IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put=function(value,...rest) {
            if(this.name==='app_state' && value?.arrivalSnapshot?.dispatched)
              throw new DOMException('synthetic dispatch write failure','QuotaExceededError');
            return put.call(this,value,...rest);
          };
        }""")
        self.enter()
        self.assertEqual(self.generations, 0)
        self.assertIn("尚未发送", self.page.locator("#request-status").inner_text())
        before = self.local()
        record = next(r for r in before["state"] if r["key"] == f"dialogue_context_v1:{fixture.CHARACTER}")
        self.assertFalse(record["arrivalSnapshot"]["dispatched"])
        original_id = record["arrivalSnapshot"]["envelope"]["arrival_id"]
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.querySelector('#stage-speech').textContent.includes('合成到场问候')")
        self.assertEqual(self.generations, 1)
        self.assertEqual(self.arrivals[0]["payload"]["arrival_id"], original_id)
        self.assertFalse(self.arrivals[0]["recovery"])
        self.assertFalse(self.errors)

    def test_reentry_does_not_overwrite_an_unresolved_paid_arrival(self):
        self.open(snow=True)
        self.lose_responses = True
        self.enter()
        original = copy.deepcopy(self.arrivals[0]["payload"])
        self.page.locator("#open-communicator").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='text'")
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#system-banner').textContent.includes('上一次到场回应还未确认')")
        self.assertEqual(self.generations, 1)
        self.assertEqual(self.page.locator("#chat-app").get_attribute("data-channel"), "text")
        self.assertTrue(all(row["payload"] == original for row in self.arrivals))
        joins = [r for r in self.rows("/public/v2/scene/action") if r["payload"]["action"] == "join_character"]
        self.assertEqual(len(joins), 1)
        self.assertFalse(self.errors)
