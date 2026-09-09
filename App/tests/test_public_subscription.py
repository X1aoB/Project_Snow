from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.snow_app.config import Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_main import _anonymous_cookie_name, create_app
from backend.snow_app.public_providers import PROVIDERS, ProviderHTTPPool, discover_models
from backend.snow_app.public_security import open_byok_credential
from backend.snow_app.public_service import _public_immersive_thinking_decision
from backend.snow_app.public_store import PublicStore
from backend.snow_app.public_subscription import (
    BASE_URL,
    MODEL,
    PROVIDER,
    SubscriptionBroker,
    SubscriptionError,
    SubscriptionSyncClient,
)
from tests.test_public_api import _settings

BODY = {"model": MODEL, "messages": [{"role": "user", "content": "synthetic request"}],
        "response_format": {"type": "json_object"}}
ORIGIN = {"Origin": "https://snow.xiaob.dev"}
NOTICES = {"accepted_transit_notice": True, "accepted_cost_notice": True,
           "accepted_local_history_notice": True}


class SubscriptionBrokerTests(TestCase):
    def connected(self, broker, subject="first"):
        relay, code, _ = broker.pair(subject, 43200)
        token, _ = broker.connect(code)
        return relay, token

    def test_pair_once_ttl_capacity_subject_isolation_and_restart(self):
        now = [1000.0]
        broker = SubscriptionBroker(True, clock=lambda: now[0], max_connections=2)
        relay, code, expires = broker.pair("first", 999999)
        self.assertEqual(expires, 44200)
        token, _ = broker.connect(code)
        with self.assertRaisesRegex(SubscriptionError, "subscription_pairing_expired"):
            broker.connect(code)
        broker.pair("second", 43200)
        with self.assertRaisesRegex(SubscriptionError, "subscription_capacity_reached"):
            broker.pair("third", 43200)
        self.assertEqual(broker.status("third")["status"], "disconnected")
        self.assertEqual(broker.status("first")["status"], "connected")
        self.assertNotIn(token, json.dumps(broker.status("first")))
        broker.disconnect("second")
        broker.preflight(relay, MODEL)
        _, new_code, _ = broker.pair("second", 43200)
        now[0] += 301
        with self.assertRaisesRegex(SubscriptionError, "subscription_pairing_expired"):
            broker.connect(new_code)
        self.assertEqual(broker.status("first")["status"], "disconnected")
        broker.close()
        self.assertEqual(len(broker._subjects), 0)
        with self.assertRaisesRegex(SubscriptionError, "credential_invalid"):
            broker.authenticate(token)

    def test_sync_job_redelivery_dedup_and_fixed_model_no_http(self):
        broker = SubscriptionBroker(True)
        relay, token = self.connected(broker)
        upstream = Mock()
        client = SubscriptionSyncClient(broker, upstream)
        with ThreadPoolExecutor() as executor:
            result = executor.submit(client.post, BASE_URL + "/chat/completions",
                                     headers={"Authorization": "Bearer " + relay}, json=BODY, timeout=120)
            first = asyncio.run(broker.poll(token))["job"]
            second = asyncio.run(broker.poll(token))["job"]
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(first["model"], MODEL)
            self.assertEqual(first["effort"], "max")
            self.assertTrue(0 < second["timeout_seconds"] <= first["timeout_seconds"] <= 120)
            with self.assertRaisesRegex(SubscriptionError, "subscription_busy"):
                broker.complete(relay, BODY)
            self.assertEqual(broker.result(token, first["job_id"], '{"answer":"ok"}', None,
                                          {"total_tokens": 4, "secret": "not retained"}), {"accepted": True})
            self.assertEqual(broker.result(token, first["job_id"], "duplicate", None, {}), {"accepted": True})
            response = result.result(timeout=2)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], '{"answer":"ok"}')
        self.assertEqual(response.json()["usage"], {"total_tokens": 4})
        self.assertEqual(asyncio.run(broker.poll(token, wait_seconds=0))["job"], None)
        upstream.post.assert_not_called()
        with self.assertRaisesRegex(SubscriptionError, "subscription_model_unavailable"):
            broker.complete(relay, {**BODY, "model": "other"})
        client.post("https://api.openai.com/v1/chat/completions", json={})
        upstream.post.assert_called_once()

    def test_timeout_and_late_result_never_start_a_second_generation(self):
        broker = SubscriptionBroker(True)
        relay, token = self.connected(broker)
        with ThreadPoolExecutor() as executor:
            result = executor.submit(broker.complete, relay, BODY, 0.08)
            job = asyncio.run(broker.poll(token))["job"]
            with self.assertRaisesRegex(SubscriptionError, "subscription_result_unknown"):
                result.result(timeout=2)
        self.assertEqual(broker.status("first")["status"], "disconnected")
        self.assertEqual(broker.result(token, job["job_id"], "late success", None, {}), {"accepted": True})
        with self.assertRaisesRegex(SubscriptionError, "subscription_not_connected"):
            broker.complete(relay, BODY)
        self.assertEqual(broker._subjects["first"].job.job_id, job["job_id"])
        self.connected(broker)
        with self.assertRaisesRegex(SubscriptionError, "credential_invalid"):
            broker.authenticate(token)

    def test_async_pool_and_cancelled_job_are_fenced(self):
        broker = SubscriptionBroker(True)
        relay, token = self.connected(broker)

        async def exercise():
            pool = ProviderHTTPPool(broker)
            self.assertEqual(await discover_models(PROVIDERS[PROVIDER], relay, client=pool), [MODEL])
            task = asyncio.create_task(pool.post(BASE_URL + "/chat/completions",
                                                headers={"Authorization": "Bearer " + relay}, json=BODY))
            job = (await broker.poll(token))["job"]
            self.assertEqual(job["messages"], BODY["messages"])
            task.cancel()
            with self.assertRaisesRegex(SubscriptionError, "subscription_result_unknown"):
                await task
            with self.assertRaisesRegex(SubscriptionError, "subscription_not_connected"):
                await pool.post(BASE_URL + "/chat/completions", headers={"Authorization": "Bearer " + relay}, json=BODY)
            self.assertIsNone(pool._client)
            await pool.close()

        asyncio.run(exercise())

    def test_multiple_workers_fail_closed_and_invalid_messages_create_no_job(self):
        for workers in ("2", "invalid", "0"):
            with self.subTest(workers=workers), patch.dict("os.environ", {"WEB_CONCURRENCY": workers}):
                broker = SubscriptionBroker(True)
                with self.assertRaisesRegex(SubscriptionError, "subscription_disabled"):
                    broker.pair("first", 3600)
        broker = SubscriptionBroker(True)
        relay, _ = self.connected(broker)
        for messages in ([{"role": "tool", "content": "no"}], [{"role": "user", "content": [{}]}], []):
            with self.assertRaisesRegex(SubscriptionError, "subscription_protocol_error"):
                broker.complete(relay, {**BODY, "messages": messages})
        with self.assertRaisesRegex(SubscriptionError, "subscription_request_too_large"):
            broker.complete(relay, {**BODY, "messages": [{"role": "user", "content": "x" * 262144}]})
        self.assertIsNone(broker._subjects["first"].job)


class SubscriptionAPITests(TestCase):
    def setUp(self):
        self.settings = replace(_settings(), subscription_enabled=True)
        self.store = PublicStore(self.settings.database_url)
        self.store.create_schema()
        self.app = create_app(self.settings, Settings.from_environment(), self.store)
        self.temporary = TemporaryDirectory()
        views = Path(self.temporary.name) / "views.jsonl"
        views.write_text("".join(json.dumps({"character_id": character.character_id}) + "\n"
                                for character in MVP_CHARACTERS), encoding="utf-8")
        self.app.state.chat_service.mvp.views_path = views
        self.app.state.chat_service.mvp._views_cache = None
        self.client = TestClient(self.app)
        self.connector = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.connector.close()
        self.app.state.subscription_broker.close()
        self.app.state.chat_service.close()
        self.temporary.cleanup()

    def pair(self):
        response = self.client.post("/public/v1/subscription/pair", headers=ORIGIN, json=NOTICES)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def connect(self, code):
        response = self.connector.post("/public/v1/subscription/connector/connect", headers=ORIGIN,
                                       json={"pairing_code": code, "protocol_version": 1, "model": MODEL, "effort": "max"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["connector_token"]

    def test_config_notice_origin_pairing_and_subject_scope(self):
        config = self.client.get("/public/v1/config").json()
        self.assertTrue(config["subscription"]["enabled"])
        self.assertIn(PROVIDER, [provider["provider_id"] for provider in config["providers"]])
        self.assertEqual(self.client.post("/public/v1/subscription/pair", json=NOTICES).status_code, 403)
        response = self.client.post("/public/v1/subscription/pair", headers=ORIGIN,
                                    json={**NOTICES, "accepted_cost_notice": False})
        self.assertEqual(response.status_code, 422)
        pairing = self.pair()
        self.assertEqual(len(pairing["pairing_code"]), 43)
        self.assertEqual(self.client.get("/public/v1/subscription/status").json()["status"], "waiting")
        token = self.connect(pairing["pairing_code"])
        self.assertEqual(self.client.get("/public/v1/subscription/status").json()["status"], "connected")
        self.assertEqual(self.connector.get("/public/v1/subscription/status").json()["status"], "disconnected")
        self.connector.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
        self.assertEqual(self.client.get("/public/v1/subscription/status").json()["status"], "connected")
        # Credential encloses only an opaque relay token, bound to the browser cookie.
        claims = open_byok_credential(self.settings,
                                     anonymous_id=self.client.cookies.get(_anonymous_cookie_name(self.settings)),
                                     token=pairing["credential"], expected_provider=PROVIDER)
        self.assertEqual(len(claims["api_key"]), 43)
        self.assertNotEqual(claims["api_key"], token)
        response = self.connector.post("/public/v1/byok/models", headers=ORIGIN,
                                       json={"provider": PROVIDER, "credential": pairing["credential"], "request_id": str(uuid4())})
        self.assertEqual(response.status_code, 401)
        self.client.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
        response = self.connector.post("/public/v1/subscription/connector/poll", headers=ORIGIN,
                                       json={"connector_token": token})
        self.assertEqual(response.status_code, 401)

    def test_chat_relay_replays_after_disconnect_without_another_job(self):
        pairing = self.pair()
        token = self.connect(pairing["pairing_code"])
        service = self.app.state.chat_service
        request_id = str(uuid4())
        body = {"provider": PROVIDER, "credential": pairing["credential"], "model": MODEL,
                "request_id": request_id, "character_id": MVP_CHARACTERS[0].character_id, "message": "synthetic hello"}

        async def chat(payload, subject, provider, relay, **kwargs):
            response = await asyncio.to_thread(service.mvp._model_http_client.post, BASE_URL + "/chat/completions",
                                               headers={"Authorization": "Bearer " + relay}, json=BODY, timeout=120)
            content = response.json()["choices"][0]["message"]["content"]
            return {"request_id": request_id, "character_id": payload.character_id, "provider": PROVIDER,
                    "model": MODEL, "answer": content, "communication_channel": "text",
                    "content_blocks": [{"type": "message", "text": content}], "state_package": "", "usage": {}}

        with patch.object(service, "chat", side_effect=chat) as generate, ThreadPoolExecutor() as executor:
            pending = executor.submit(self.client.post, "/public/v1/chat/stream", headers=ORIGIN, json=body)
            poll = self.connector.post("/public/v1/subscription/connector/poll", headers=ORIGIN,
                                       json={"connector_token": token})
            self.assertEqual(poll.status_code, 200, poll.text)
            job = poll.json()["job"]
            response = self.connector.post("/public/v1/subscription/connector/result", headers=ORIGIN,
                                           json={"connector_token": token, "job_id": job["job_id"], "content": "synthetic reply", "usage": {}})
            self.assertEqual(response.status_code, 200, response.text)
            first = pending.result(timeout=5)
            self.assertIn("synthetic reply", first.text)
            self.client.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
            replay = self.client.post("/public/v1/chat/stream", headers=ORIGIN, json=body)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertIn("synthetic reply", replay.text)
            self.assertEqual(generate.call_count, 1)
            offline = self.client.post("/public/v1/chat/stream", headers=ORIGIN,
                                       json={**body, "request_id": str(uuid4())})
            self.assertEqual(offline.status_code, 409, offline.text)
            self.assertEqual(offline.json()["detail"]["code"], "subscription_not_connected")

    def test_connector_validates_token_before_independent_limits_and_rejects_unsafe_result(self):
        pairing = self.pair()
        token = self.connect(pairing["pairing_code"])
        with patch.object(self.app.state.async_store, "call", wraps=self.app.state.async_store.call) as store_call:
            response = self.connector.post("/public/v1/subscription/connector/result", headers=ORIGIN,
                                           json={"connector_token": token, "job_id": "x" * 32,
                                                 "error": "do not reflect secret details", "usage": {}})
            self.assertEqual(response.status_code, 422)
            limits = [call.args[2] for call in store_call.call_args_list if call.args[0] == "consume_limits"]
            self.assertEqual(len(limits), 1)
            self.assertTrue(all(item[0].startswith("subscription_connector_") for item in limits[0]))
            store_call.reset_mock()
            response = self.connector.post("/public/v1/subscription/connector/poll", headers=ORIGIN,
                                           json={"connector_token": "x" * 43})
            self.assertEqual(response.status_code, 401)
            store_call.assert_not_called()
        response = self.client.post("/public/v1/byok/session", headers=ORIGIN,
                                    json={**NOTICES, "provider": PROVIDER, "api_key": "must-not-be-used"})
        self.assertEqual(response.status_code, 422)

    def test_chat_unknown_crosses_real_facade_and_replays_without_rewrite(self):
        pairing = self.pair()
        self.connect(pairing["pairing_code"])
        service = self.app.state.chat_service
        body = {"provider": PROVIDER, "credential": pairing["credential"], "model": MODEL,
                "request_id": str(uuid4()), "character_id": MVP_CHARACTERS[0].character_id,
                "message": "synthetic hello"}
        with patch.object(service, "_request_state", return_value=nullcontext(("session", "world", {}))), \
                patch.object(service, "_movement_intent_for_request", return_value=None), \
                patch.object(service, "sticker_candidates", return_value=[]), \
                patch.object(service.mvp, "chat", side_effect=SubscriptionError(
                    "subscription_result_unknown", submitted=True)) as generation:
            first = self.client.post("/public/v1/chat/stream", headers=ORIGIN, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertIn("subscription_result_unknown", first.text)
            self.assertNotIn("provider_request_failed", first.text)
            self.client.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
            replay = self.client.post("/public/v1/chat/stream", headers=ORIGIN, json=body)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertIn("subscription_result_unknown", replay.text)
            self.assertEqual(generation.call_count, 1)

    def test_mvp_http_layer_preserves_unknown_without_compatibility_retry(self):
        service = self.app.state.chat_service
        error = SubscriptionError("subscription_result_unknown", submitted=True)
        decision = _public_immersive_thinking_decision(PROVIDERS[PROVIDER])
        with patch.object(service.mvp._model_http_client, "post", side_effect=error) as request:
            with self.assertRaises(SubscriptionError) as caught:
                service.mvp._call_model("synthetic rules", "synthetic request",
                                        model_settings=(BASE_URL, "synthetic-relay", MODEL),
                                        thinking_decision=decision)
            self.assertIs(caught.exception, error)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["json"]["reasoning_effort"], "max")

    def test_arrival_unknown_crosses_real_facade_and_replays_without_rewrite(self):
        pairing = self.pair()
        self.connect(pairing["pairing_code"])
        service = self.app.state.chat_service
        body = {"provider": PROVIDER, "credential": pairing["credential"], "model": MODEL,
                "arrival_id": str(uuid4()), "character_id": MVP_CHARACTERS[0].character_id}
        prepared = {"decision": "noticed", "state_package": "", "state": {}, "model_called": False}
        with patch.object(service, "prepare_presence_arrival", return_value=prepared), \
                patch.object(service, "_request_state", return_value=nullcontext(("session", "world", {}))), \
                patch.object(service.mvp, "chat", side_effect=SubscriptionError(
                    "subscription_result_unknown", submitted=True)) as generation:
            first = self.client.post("/public/v1/presence/arrival", headers=ORIGIN, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["terminal_error"], "subscription_result_unknown")
            self.assertTrue(first.json()["model_called"])
            self.client.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
            replay = self.client.post("/public/v1/presence/arrival", headers=ORIGIN, json=body)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["terminal_error"], "subscription_result_unknown")
            self.assertEqual(generation.call_count, 1)

    def test_summary_unknown_is_terminal_and_replays_the_specific_error(self):
        pairing = self.pair()
        self.connect(pairing["pairing_code"])
        service = self.app.state.chat_service
        body = {"provider": PROVIDER, "credential": pairing["credential"], "model": MODEL,
                "request_id": str(uuid4()), "character_id": MVP_CHARACTERS[0].character_id,
                "turns": [{"role": "user", "content": "synthetic question"},
                          {"role": "assistant", "content": "synthetic answer"}]}
        with patch.object(service.provider_client, "post", side_effect=SubscriptionError(
                "subscription_result_unknown", submitted=True)) as generation:
            first = self.client.post("/public/v1/chat/summarize", headers=ORIGIN, json=body)
            self.assertEqual(first.status_code, 409, first.text)
            self.assertEqual(first.json()["detail"]["code"], "subscription_result_unknown")
            self.client.post("/public/v1/subscription/disconnect", headers=ORIGIN, json={})
            replay = self.client.post("/public/v1/chat/summarize", headers=ORIGIN, json=body)
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertEqual(replay.json()["detail"]["code"], "subscription_result_unknown")
            self.assertEqual(generation.call_count, 1)

    def test_disabled_config_does_not_enable_subscription_through_provider_list(self):
        settings = replace(self.settings, subscription_enabled=False, enabled_providers=(PROVIDER, "openai"))
        app = create_app(settings, Settings.from_environment(), self.store)
        try:
            with TestClient(app) as client:
                config = client.get("/public/v1/config").json()
                self.assertFalse(config["subscription"]["enabled"])
                self.assertNotIn(PROVIDER, [provider["provider_id"] for provider in config["providers"]])
                self.assertEqual(client.post("/public/v1/subscription/pair", headers=ORIGIN, json=NOTICES).status_code, 404)
        finally:
            app.state.chat_service.close()
