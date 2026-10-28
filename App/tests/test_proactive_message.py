"""Paid proactive reminder contracts. All model boundaries are synthetic."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from fastapi import HTTPException, Request
from sqlalchemy import text

from backend.snow_app.config import Settings
from backend.snow_app.dialogue_core import GenerationGate
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.proactive_message import authority_change, proactive_config, request_hash
from backend.snow_app.public_knowledge import PublicKnowledge
from backend.snow_app.public_contracts import (ProactiveConsentRequest, ProactiveMessageRequest,
                                             PresenceTransitionRequest, SceneActionRequest)
from backend.snow_app.public_main import create_app
from backend.snow_app.public_security import sign_state, verify_state
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from backend.snow_app.public_subscription import SubscriptionError
from backend.snow_app.session_context import sign_session_context
from tests import test_dialogue_v2_service as fixtures


class ProactiveMessageTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scene = fixtures.DialogueV2ServiceTests(methodName="runTest")
        self.scene.setUp()
        self.settings = replace(self.scene.settings, proactive_messages_enabled=True)
        self.scene.settings = self.settings
        self.scene.service.settings = self.settings
        self.scene.legacy.public_settings = self.settings
        from backend.snow_app.proactive_message import _day
        self.scene.day = _day()
        self.scene.daily.update(schedule_date=self.scene.day,
            expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat())
        self.legacy = self.scene.legacy
        self.legacy.gate = GenerationGate()
        self.legacy.repository = Mock()
        self.legacy.repository.get_persona.return_value = {"review_status": "verified", "summary": "合成角色资料"}
        self.legacy.mvp.public_knowledge = PublicKnowledge()
        self.legacy.mvp._dialogue_profiles.return_value = {}
        self.legacy.mvp._dialogue_profile_prompt_context = MVPService._dialogue_profile_prompt_context
        self.legacy.media, self.legacy.stickers = Mock(), Mock()
        self.legacy.media.verify.return_value = self.legacy.stickers.verify.return_value = {"status": "unavailable"}
        engine = getattr(self, "store_engine", None)
        self.store = PublicStore("sqlite+pysqlite:///:memory:" if engine is None else "", engine=engine)
        if engine is None:
            self.store.create_schema()
        self.app = create_app(self.settings, Settings(Path('unused'),
            Path('unused'), False, "", []), self.store, self.legacy, self.scene.service)
        self.routes = {route.path: route.endpoint for route in self.app.routes if hasattr(route, "endpoint")}
        self.request = self.make_request()
        self.endpoint = self.routes["/public/v2/rendezvous/message"]
        self.open_credential = patch("backend.snow_app.proactive_message.open_byok_credential",
            return_value={"api_key": "synthetic", "exp": int((datetime.now(UTC) + timedelta(hours=2)).timestamp())})
        self.open_credential.start()
        self.completion = patch("backend.snow_app.proactive_message.simple_completion",
            new=AsyncMock(return_value=("我在约好的地方等你，有空时再来就好。", {"total_tokens": 12})))
        self.provider = self.completion.start()
        self.model_session = uuid4()
        self.consent_payload = ProactiveConsentRequest(provider="openai", model="synthetic",
            credential="synthetic-credential-value", model_session_id=self.model_session,
            notice_version="proactive-cost-2", accepted_cost_notice=True, duration="24h")
        self.consent = await self.routes["/public/v2/rendezvous/consent"](self.request, self.consent_payload)
        self.accepted, self.before = await self.new_rendezvous()
        self.payload = self.message_payload(self.accepted)

    async def asyncTearDown(self):
        self.completion.stop()
        self.open_credential.stop()
        await self.app.state.async_store.close()
        await self.app.state.provider_http.close()
        if not hasattr(self, "store_engine"):
            self.store.engine.dispose()

    def make_request(self, recovery=False, **state):
        return Request({"type": "http", "headers": [(b"x-snow-recovery-only", b"1")] if recovery else [],
            "state": {"anonymous_id": "synthetic", "subject_hash": self.scene.subject, **state}})

    async def new_rendezvous(self):
        envelope = self.scene.request("一起去公园吧。")
        accepted = await self.scene.service.chat(envelope, self.scene.subject, None, "synthetic", gate_reserved=True)
        change = authority_change(self.settings, self.scene.subject, accepted, envelope.chat.state_package,
                                  self.scene.character.character_id)
        cache_id = "synthetic-accept:" + str(envelope.chat.request_id)
        await self.app.state.async_store.call("claim_request", cache_id, self.scene.subject, "synthetic-hash")
        await self.app.state.async_store.call("complete_request", cache_id, accepted, rendezvous_change=change)
        return accepted, envelope

    def message_payload(self, accepted):
        pending = verify_state(self.settings, accepted["state_package"])["pending_rendezvous"][self.scene.character.character_id]
        return ProactiveMessageRequest(request_id=uuid4(), character_id=self.scene.character.character_id,
            rendezvous_id=pending["rendezvous_id"], segment_id=self.scene.segment,
            state_package=accepted["state_package"], session_context_package=accepted["session_context_package"],
            provider="openai", model="synthetic", model_session_id=self.model_session,
            notice_version="proactive-cost-2", credential="synthetic-credential-value",
            consent_token=self.consent["consent_token"])

    def quota(self):
        with self.store.engine.connect() as connection:
            return connection.execute(text("SELECT coalesce(sum(reserved),0) FROM public_proactive_day")).scalar_one()

    async def assert_code(self, code, payload=None, request=None):
        with self.assertRaises(HTTPException) as error:
            await self.endpoint(request or self.request, payload or self.payload)
        self.assertEqual(error.exception.detail["code"], code)

    async def test_success_is_one_call_only_character_text_real_usage_and_exact_replay(self):
        before = (self.payload.state_package, self.payload.session_context_package)
        first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["source"], "proactive_reminder")
        self.assertEqual(first["usage"], {"provider_calls": 1, "prompt_tokens": None,
                                         "completion_tokens": None, "total_tokens": 12})
        self.assertTrue(all(block["type"] == "message" for block in first["content_blocks"]))
        self.assertNotIn("state_package", first)
        self.assertNotIn("session_context_package", first)
        self.assertEqual(before, (self.payload.state_package, self.payload.session_context_package))
        for recovery in (False, True):
            replay = await self.endpoint(self.make_request(recovery),
                self.payload.model_copy(update={"credential": "", "consent_token": ""}))
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["content_blocks"], first["content_blocks"])
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)
        call = self.provider.call_args
        self.assertEqual(call.kwargs["max_tokens"], 400)
        self.assertIn("系统提醒事件", call.kwargs["user_prompt"])

    async def test_prompt_shares_main_dialogue_addresses_for_every_formal_character(self):
        expected = self.legacy.mvp.public_knowledge.preferred_addresses()
        self.assertEqual(expected["ca0144ccd81b"], "亲爱的")
        self.assertGreater(len(set(expected.values())), 1)
        self.legacy.mvp._dialogue_profiles.return_value = {
            cid: {"address_terms": [{"term": "分析员", "support_level": "observed"}]}
            for cid in expected
        }
        # A long persona must not truncate the higher-priority address.
        self.legacy.repository.get_persona.return_value = {"review_status": "verified", "summary": "合成资料" * 1200}
        service = self.app.state.proactive_message_service
        pending = service.prepare(self.payload, self.scene.subject)
        for character in MVP_CHARACTERS:
            if character.character_id not in expected:
                continue
            with self.subTest(character_id=character.character_id):
                self.provider.reset_mock()
                publication = Mock()
                await service.generate(self.payload.model_copy(update={"character_id": character.character_id}),
                    pending, Mock(), "synthetic", on_publication=publication)
                prompt = self.provider.call_args.kwargs["system_prompt"]
                reference = json.loads(prompt.split("以下已审阅资料仅供语气参考：", 1)[1].split("\n", 1)[0])
                main_address = MVPService._relationship_address_memory({
                    "character": character, "relationship_background": {"status": "explicit"},
                })["preferred_address"]
                self.assertEqual(reference["preferred_address"], main_address)
                self.assertEqual(reference["preferred_address"], expected[character.character_id])
                self.assertEqual(reference["address_terms"][0]["term"], "分析员")
                self.assertIn("称呼他时用你平时的称呼", prompt)
                self.assertIn("直接称呼只用该称呼", prompt)
                self.assertIn("第三人称身份说明", prompt)
                self.assertNotIn("发给分析员", prompt)
                self.assertNotIn("代写分析员台词", prompt)
                self.assertNotIn("分析员已到场", prompt)
                self.assertEqual(self.provider.await_count, 1)
                publication.assert_called_once_with()
        self.assertEqual(self.quota(), 0)
        self.legacy.repository.documents.assert_not_called()

    async def test_reviewed_address_survives_unverified_persona(self):
        self.legacy.repository.get_persona.return_value = {
            "review_status": "pending", "speaking_style": "synthetic-unreviewed-address",
        }
        result = await self.endpoint(self.request, self.payload)
        self.assertEqual(result["status"], "completed")
        prompt = self.provider.call_args.kwargs["system_prompt"]
        reference = json.loads(prompt.split("以下已审阅资料仅供语气参考：", 1)[1].split("\n", 1)[0])
        expected = self.legacy.mvp.public_knowledge.preferred_addresses().get(self.payload.character_id)
        self.assertEqual(reference["preferred_address"], expected)
        self.assertNotIn("synthetic-unreviewed-address", prompt)
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)

    async def test_other_character_uses_main_profile_terms_without_inventing_intimacy(self):
        known = self.legacy.mvp.public_knowledge.preferred_addresses()
        character = next(item for item in MVP_CHARACTERS if item.character_id not in known)
        terms = [{"term": "队长", "support_level": "observed", "evidence_document_ids": ["synthetic-address-source"]}]
        self.legacy.mvp._dialogue_profiles.return_value = {character.character_id: {"address_terms": terms}}
        service = self.app.state.proactive_message_service
        pending = service.prepare(self.payload, self.scene.subject)
        await service.generate(self.payload.model_copy(update={"character_id": character.character_id}),
            pending, Mock(), "synthetic", on_publication=Mock())
        prompt = self.provider.call_args.kwargs["system_prompt"]
        reference = json.loads(prompt.split("以下已审阅资料仅供语气参考：", 1)[1].split("\n", 1)[0])
        self.assertIsNone(reference["preferred_address"])
        main_profile = MVPService._dialogue_profile_prompt_context({"address_terms": terms})
        self.assertEqual(reference["address_terms"], main_profile["address_terms"])
        self.assertNotIn("亲爱的", prompt)
        self.assertNotIn("发给分析员", prompt)
        self.assertEqual(self.provider.await_count, 1)

    async def test_missing_recovery_is_read_only_and_default_is_off(self):
        self.assertFalse(proactive_config(replace(self.settings, proactive_messages_enabled=False))["enabled"])
        with patch.object(self.store, "claim_proactive", wraps=self.store.claim_proactive) as claim:
            await self.assert_code("request_result_unavailable", request=self.make_request(True))
            claim.assert_not_called()
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)

    async def advanced_other_character_world(self):
        envelope = self.scene.request("我们继续聊。", character_id=self.scene.other.character_id,
            state_package=self.accepted["state_package"])
        with patch("backend.snow_app.public_main.open_byok_credential", return_value={"api_key": "synthetic"}):
            stream = await self.routes["/public/v2/chat/stream"](self.request, envelope)
            async for _chunk in stream.body_iterator:
                pass
        saved = await self.app.state.async_store.call("request_result", str(envelope.chat.request_id), self.scene.subject)
        self.assertTrue(saved)
        before = verify_state(self.settings, self.payload.state_package)
        after = verify_state(self.settings, saved["state_package"])
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(after["pending_rendezvous"][self.payload.character_id],
                         before["pending_rendezvous"][self.payload.character_id])
        return self.payload.model_copy(update={"state_package": saved["state_package"]})

    async def test_other_character_world_progress_preserves_original_waiting_context(self):
        payload = await self.advanced_other_character_world()
        self.assertEqual(payload.session_context_package, self.payload.session_context_package)
        result = await self.endpoint(self.request, payload)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)

    async def test_future_context_world_revision_is_rejected_before_payment(self):
        context = self.scene.context(self.accepted)
        context.scene_overlay.world_revision += 1
        payload = self.payload.model_copy(update={"session_context_package": sign_session_context(self.settings, context)})
        await self.assert_code("invalid_proactive_authorization", payload)
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)

    async def test_cancelled_authority_still_rejects_older_context_with_newer_world(self):
        payload = await self.advanced_other_character_world()
        cancel = SceneActionRequest(request_id=uuid4(), character_id=payload.character_id,
            segment_id=payload.segment_id, action="cancel_rendezvous", rendezvous_id=payload.rendezvous_id,
            state_package=payload.state_package, session_context_package=payload.session_context_package)
        await self.routes["/public/v2/scene/action"](self.request, cancel)
        await self.assert_code("rendezvous_not_active", payload)
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)

    async def test_switch_off_blocks_new_admission_but_exact_recovery_survives(self):
        disabled = {"enabled": False}
        with patch("backend.snow_app.proactive_message.proactive_config", return_value=disabled):
            await self.assert_code("proactive_messages_disabled")
            with self.assertRaises(HTTPException) as error:
                await self.routes["/public/v2/rendezvous/consent"](self.request, self.consent_payload)
            self.assertEqual(error.exception.detail["code"], "proactive_messages_disabled")
        self.provider.assert_not_awaited()
        first = await self.endpoint(self.request, self.payload)
        with patch("backend.snow_app.proactive_message.proactive_config", return_value=disabled):
            replay = await self.endpoint(self.make_request(True), self.payload.model_copy(update={"credential": "", "consent_token": ""}))
        self.assertEqual(replay["content_blocks"], first["content_blocks"])
        self.assertEqual(self.provider.await_count, 1)

    async def test_consent_duration_maps_to_hours_capped_by_credential_and_matches_byok(self):
        from backend.snow_app import proactive_message as proactive
        from backend.snow_app.public_security import BYOK_RETENTION_HOURS
        # The picker options must equal the credential retention policy exactly.
        self.assertEqual(proactive.DURATION_HOURS, BYOK_RETENTION_HOURS)
        route = self.routes["/public/v2/rendezvous/consent"]
        far = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
        with patch("backend.snow_app.proactive_message.open_byok_credential",
                   return_value={"api_key": "synthetic", "exp": far}):
            for duration, hours in proactive.DURATION_HOURS.items():
                payload = self.consent_payload.model_copy(update={"duration": duration})
                issued = await route(self.request, payload)
                self.assertEqual(issued["notice_version"], "proactive-cost-2")
                self.assertEqual(issued["duration"], duration)
                raw = proactive._verify(self.settings, issued["consent_token"], proactive._CONSENT_DOMAIN, 2048)
                remaining = raw["expires_at"] - raw["issued_at"]
                self.assertEqual(remaining, hours * 3600)
        # A short credential life clamps every duration down to the credential exp.
        soon = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        with patch("backend.snow_app.proactive_message.open_byok_credential",
                   return_value={"api_key": "synthetic", "exp": soon}):
            issued = await route(self.request, self.consent_payload.model_copy(update={"duration": "7d"}))
            raw = proactive._verify(self.settings, issued["consent_token"], proactive._CONSENT_DOMAIN, 2048)
            self.assertEqual(raw["expires_at"], soon)
        self.provider.assert_not_awaited()

    async def test_old_notice_version_consent_request_is_rejected(self):
        with self.assertRaises(Exception):
            ProactiveConsentRequest(provider="openai", model="synthetic",
                credential="synthetic-credential-value", model_session_id=self.model_session,
                notice_version="proactive-cost-1", accepted_cost_notice=True)

    async def test_expired_consent_and_yesterday_package_are_not_paid(self):
        from backend.snow_app import proactive_message as proactive
        raw = proactive._verify(self.settings, self.payload.consent_token, proactive._CONSENT_DOMAIN, 2048)
        raw["expires_at"] = int(datetime.now(UTC).timestamp()) - 1
        expired = proactive._sign(self.settings, raw, proactive._CONSENT_DOMAIN, 2048)
        await self.assert_code("invalid_proactive_authorization", self.payload.model_copy(update={"consent_token": expired}))
        with patch("backend.snow_app.proactive_message._day", return_value="1900-01-01"):
            await self.assert_code("invalid_proactive_authorization")
        with self.assertRaises(HTTPException) as error:
            await self.routes["/public/v2/rendezvous/consent"](self.request,
                self.consent_payload.model_copy(update={"accepted_cost_notice": False}))
        self.assertEqual(error.exception.detail["code"], "proactive_cost_notice_required")
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)

    async def test_queue_crossing_midnight_revalidates_before_occupying_allowance(self):
        with patch("backend.snow_app.proactive_message._day", side_effect=[self.scene.day, "1900-01-01"]):
            await self.assert_code("invalid_proactive_authorization")
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)
        self.assertEqual(self.legacy.gate.semaphore._value, 4)

    async def test_local_preparation_failure_does_not_invent_a_provider_call(self):
        self.legacy.repository.get_persona.side_effect = RuntimeError("synthetic local preparation error")
        first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "failed")
        self.assertFalse(first["model_called"])
        self.assertEqual(first["usage"]["provider_calls"], 0)
        self.assertEqual(first["usage"]["total_tokens"], None)
        self.provider.assert_not_awaited()

    async def test_signed_but_unregistered_legacy_pending_does_not_become_authority(self):
        envelope = self.scene.request("一起去公园吧。")
        accepted = await self.scene.service.chat(envelope, self.scene.subject, None, "synthetic", gate_reserved=True)
        await self.assert_code("rendezvous_not_active", self.message_payload(accepted))
        self.provider.assert_not_awaited()

    async def test_replacement_rendezvous_closes_prior_even_from_an_older_signed_branch(self):
        accepted, _ = await self.new_rendezvous()
        self.assertNotEqual(self.message_payload(accepted).rendezvous_id, self.payload.rendezvous_id)
        await self.assert_code("rendezvous_not_active")
        self.provider.assert_not_awaited()

    async def test_legacy_join_also_closes_authority_for_v2_proactive_requests(self):
        transition = PresenceTransitionRequest(request_id=uuid4(), character_id=self.payload.character_id,
            target_channel="in_person", action="join_character", state_package=self.payload.state_package)
        await self.routes["/public/v1/presence/transition"](self.request, transition)
        await self.assert_code("rendezvous_not_active")
        self.provider.assert_not_awaited()

    async def test_real_chat_commit_registers_then_replay_cannot_reopen_cancelled_authority(self):
        envelope = self.scene.request("一起去公园吧。")
        with patch("backend.snow_app.public_main.open_byok_credential", return_value={"api_key": "synthetic"}):
            stream = await self.routes["/public/v2/chat/stream"](self.request, envelope)
            chunks = [chunk async for chunk in stream.body_iterator]
            saved = await self.app.state.async_store.call("request_result", str(envelope.chat.request_id), self.scene.subject)
            self.assertTrue(saved)
            payload = self.message_payload(saved)
            with self.store.engine.connect() as connection:
                row = connection.execute(text("SELECT closed FROM public_rendezvous_authority WHERE rendezvous_id=:rid"),
                    {"rid": payload.rendezvous_id}).scalar_one()
                self.assertEqual(row, 0)
            cancel = SceneActionRequest(request_id=uuid4(), character_id=payload.character_id,
                segment_id=payload.segment_id, action="cancel_rendezvous", rendezvous_id=payload.rendezvous_id,
                state_package=payload.state_package, session_context_package=payload.session_context_package)
            await self.routes["/public/v2/scene/action"](self.request, cancel)
            replay = await self.routes["/public/v2/chat/stream"](self.request, envelope)
            chunks.extend([chunk async for chunk in replay.body_iterator])
        self.assertNotIn("_proactive_authority_change", "".join(chunks))
        await self.assert_code("rendezvous_not_active", payload)
        self.provider.assert_not_awaited()

    async def test_http_contract_origin_cookie_consent_message_and_read_only_recovery(self):
        from backend.snow_app.public_main import _anonymous_cookie_name
        from backend.snow_app.public_security import new_anonymous_id, subject_hash
        anonymous = new_anonymous_id()
        self.scene.subject = subject_hash(anonymous)
        self.scene.daily["subject_binding"] = self.scene.subject
        self.request = self.make_request()
        accepted, _ = await self.new_rendezvous()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
            client.cookies.set(_anonymous_cookie_name(self.settings), anonymous)
            headers = {"Origin": "http://testserver"}
            consent = await client.post("/public/v2/rendezvous/consent", headers=headers,
                json=self.consent_payload.model_dump(mode="json"))
            self.assertEqual(consent.status_code, 200)
            payload = self.message_payload(accepted).model_dump(mode="json")
            payload["consent_token"] = consent.json()["consent_token"]
            denied = await client.post("/public/v2/rendezvous/message", headers={"Origin": "https://synthetic-invalid.example"}, json=payload)
            self.assertEqual(denied.status_code, 403)
            self.provider.assert_not_awaited()
            generated = await client.post("/public/v2/rendezvous/message", headers=headers, json=payload)
            self.assertEqual(generated.status_code, 200)
            self.assertEqual(generated.json()["status"], "completed")
            payload.update(credential="", consent_token="")
            recovered = await client.post("/public/v2/rendezvous/message",
                headers={**headers, "X-Snow-Recovery-Only": "1"}, json=payload)
            self.assertEqual(recovered.status_code, 200)
            self.assertTrue(recovered.json()["idempotent_replay"])
            self.assertEqual(recovered.json()["content_blocks"], generated.json()["content_blocks"])
        self.assertEqual(self.provider.await_count, 1)

    async def test_consent_bound_to_model_session_credential_subject_and_packages(self):
        for changes in ({"model": "other"}, {"model_session_id": uuid4()},
                        {"credential": "different-credential-value"}, {"consent_token": self.consent["consent_token"] + "x"},
                        {"segment_id": uuid4()}, {"rendezvous_id": "not-the-rendezvous"}):
            with self.subTest(changes=list(changes)):
                await self.assert_code("invalid_proactive_authorization", self.payload.model_copy(update=changes))
        await self.assert_code("invalid_proactive_authorization", request=self.make_request(subject_hash="other-subject"))
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 0)

    async def test_same_request_changed_body_conflicts_even_in_recovery(self):
        await self.endpoint(self.request, self.payload)
        await self.assert_code("request_id_conflict", self.payload.model_copy(update={"model": "other"}), self.make_request(True))
        self.assertEqual(self.provider.await_count, 1)

    async def test_every_rendezvous_one_and_day_three(self):
        await self.endpoint(self.request, self.payload)
        await self.assert_code("rendezvous_already_used", self.payload.model_copy(update={"request_id": uuid4()}))
        for index in range(3):
            accepted, _ = await self.new_rendezvous()
            payload = self.message_payload(accepted)
            if index < 2:
                self.assertEqual((await self.endpoint(self.request, payload))["status"], "completed")
            else:
                await self.assert_code("proactive_daily_limit", payload)
        self.assertEqual(self.quota(), 3)
        self.assertEqual(self.provider.await_count, 3)

    async def test_cancel_and_join_tombstones_reject_old_signed_package(self):
        for action in ("cancel_rendezvous", "join_rendezvous"):
            accepted, _ = await self.new_rendezvous()
            payload = self.message_payload(accepted)
            envelope = SceneActionRequest(request_id=uuid4(), character_id=payload.character_id,
                segment_id=payload.segment_id, action=action, rendezvous_id=payload.rendezvous_id,
                state_package=payload.state_package, session_context_package=payload.session_context_package)
            await self.routes["/public/v2/scene/action"](self.request, envelope)
            await self.assert_code("rendezvous_not_active", payload)
        self.provider.assert_not_awaited()

    async def test_unknown_provider_outcome_keeps_quota_never_retries(self):
        self.provider.side_effect = TimeoutError("synthetic timeout after publication")
        first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["content_blocks"], [])
        replay = await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(replay["status"], "unknown")
        await self.assert_code("rendezvous_already_used", self.payload.model_copy(update={"request_id": uuid4()}))
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.provider.await_count, 1)

    async def test_subscription_result_unknown_is_terminal_and_preserves_allowance(self):
        self.provider.side_effect = SubscriptionError("subscription_result_unknown", submitted=True)
        first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["terminal_error"], "subscription_result_unknown")
        self.assertEqual(first["status"], "unknown")
        await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.provider.await_count, 1)

    async def test_expired_short_result_never_resurrects_paid_attempt(self):
        await self.endpoint(self.request, self.payload)
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_cache SET expires_at=:past WHERE request_id=:id"),
                {"past": datetime.now(UTC) - timedelta(seconds=1), "id": "proactive:" + str(self.payload.request_id)})
        self.store.cleanup()
        recovered = await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(recovered["status"], "unknown")
        self.assertEqual(recovered["terminal_error"], "proactive_result_unavailable")
        self.assertIsNone(recovered["usage"]["provider_calls"])
        self.assertIsNone(recovered["model_called"])
        await self.endpoint(self.request, self.payload)
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)

    async def test_expired_zero_call_result_never_invents_a_paid_call(self):
        self.provider.side_effect = SubscriptionError("subscription_preflight_failed", submitted=False)
        first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["usage"]["provider_calls"], 0)
        self.assertFalse(first["model_called"])
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_cache SET expires_at=:past WHERE request_id=:id"),
                {"past": datetime.now(UTC) - timedelta(seconds=1), "id": "proactive:" + str(self.payload.request_id)})
        self.store.cleanup()
        recovered = await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(recovered["status"], "unknown")
        self.assertIsNone(recovered["usage"]["provider_calls"])
        self.assertIsNone(recovered["model_called"])
        self.assertTrue(all(value is None for value in recovered["usage"].values()))
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)

    async def test_lost_worker_without_dispatch_does_not_invent_usage(self):
        await self.app.state.async_store.call("claim_proactive", "proactive:" + str(self.payload.request_id),
            self.scene.subject, request_hash(self.payload), day=self.scene.day,
            rendezvous_id=self.payload.rendezvous_id, character_id=self.payload.character_id,
            expires=datetime.now(UTC) + timedelta(days=3))
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_leases SET lease_expires_at=:past"),
                {"past": datetime.now(UTC) - timedelta(seconds=1)})
        recovered = await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(recovered["status"], "unknown")
        self.assertIsNone(recovered["usage"]["provider_calls"])
        self.assertIsNone(recovered["model_called"])
        self.provider.assert_not_awaited()
        self.assertEqual(self.quota(), 1)

    async def test_lost_commit_ack_recovers_original_result_without_overwrite(self):
        original = self.store.complete_proactive
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise PublicStoreUnavailable("synthetic lost commit acknowledgment")
        with patch.object(self.store, "complete_proactive", side_effect=lost_ack):
            first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["content_blocks"])
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)

    async def test_write_outage_after_provider_keeps_ledger_for_lease_recovery(self):
        with patch.object(self.store, "complete_proactive", side_effect=PublicStoreUnavailable("synthetic outage")):
            first = await self.endpoint(self.request, self.payload)
        self.assertEqual(first["status"], "unknown")
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_leases SET lease_expires_at=:past"),
                {"past": datetime.now(UTC) - timedelta(seconds=1)})
        replay = await self.endpoint(self.make_request(True), self.payload)
        self.assertEqual(replay["status"], "unknown")
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.provider.await_count, 1)

    async def test_concurrent_duplicate_never_starts_second_provider(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def hold(*args, **kwargs):
            started.set()
            await finish.wait()
            return "合成提醒。", {}
        self.provider.side_effect = hold
        first = asyncio.create_task(self.endpoint(self.request, self.payload))
        await asyncio.wait_for(started.wait(), 2)
        try:
            await self.assert_code("request_in_progress")
        finally:
            finish.set()
        await first
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.quota(), 1)
