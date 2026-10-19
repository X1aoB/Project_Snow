"""Synthetic protocol, sidecar and recovery regressions; no provider HTTP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from backend.snow_app.config import Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_contracts import (
    ChatRequest, DialogueV2ChatRequest, PendingRendezvous, SceneActionRequest, SceneOverlay, SessionMemory, StatePayload,
)
from backend.snow_app.public_main import (
    _anonymous_cookie_name, _dialogue_v2_request_body, _model_request_body, _request_hash, create_app,
)
from backend.snow_app.public_security import PublicSecurityError, sign_state, subject_hash, verify_state
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from backend.snow_app.session_context import (
    new_session_context, sign_session_context, verify_session_context,
    sign_session_memory, verify_session_memory,
    session_context_transfer_size, validate_session_context_transfer,
)
from tests.test_public_api import _settings


class SessionContextContractTests(TestCase):
    def setUp(self):
        self.settings = _settings()
        self.character = MVP_CHARACTERS[0].character_id
        self.segment = uuid4()
        self.context = new_session_context(
            self.settings, subject_hash="synthetic-subject", character_id=self.character,
            segment_id=self.segment, catalog_version="locations-1",
        )

    def verify(self, token, **kwargs):
        args = {
            "subject_hash": "synthetic-subject", "character_id": self.character,
            "segment_id": self.segment, "catalog_version": "locations-1",
        }
        return verify_session_context(self.settings, token, **{**args, **kwargs})

    def test_context_roundtrip_and_empty_bootstrap(self):
        self.assertIsNone(self.verify(""))
        token = sign_session_context(self.settings, self.context)
        self.assertLessEqual(len(token), 8192)
        self.assertEqual(self.verify(token), self.context)
        self.assertNotIn("chat", self.context.model_dump())

    def test_tamper_binding_and_version_rejected(self):
        token = sign_session_context(self.settings, self.context)
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        for value, override in [
            (bad, {}), (token, {"subject_hash": "other-subject"}),
            (token, {"character_id": MVP_CHARACTERS[1].character_id}),
            (token, {"segment_id": uuid4()}), (token, {"data_version": "new-data"}),
            (token, {"catalog_version": "new-locations"}),
        ]:
            with self.subTest(override=override), self.assertRaises(PublicSecurityError):
                self.verify(value, **override)

    def test_state_and_context_signatures_have_distinct_domains(self):
        token = sign_session_context(self.settings, self.context)
        with self.assertRaises(PublicSecurityError):
            verify_state(self.settings, token)
        with self.assertRaises(PublicSecurityError):
            self.verify(sign_state(self.settings, self.context.model_dump(mode="json")))

    def test_rotation_accepts_only_configured_previous_key(self):
        token = sign_session_context(self.settings, self.context)
        rotated = replace(self.settings, state_hmac_key=b"n" * 32, state_key_id="next",
                          state_hmac_previous_key=self.settings.state_hmac_key,
                          state_previous_key_id=self.settings.state_key_id)
        self.assertEqual(verify_session_context(
            rotated, token, subject_hash="synthetic-subject", character_id=self.character,
            segment_id=self.segment,
        ).revision, 0)
        with self.assertRaises(PublicSecurityError):
            verify_session_context(replace(rotated, state_hmac_previous_key=None), token,
                subject_hash="synthetic-subject", character_id=self.character, segment_id=self.segment)

    def test_context_size_and_scene_scope_are_bounded(self):
        memories = [SessionMemory(
            memory_id=f"memory-{number:04d}", kind="user_statement", value="合成" * 80,
            source_request_id=uuid4(), segment_id=self.segment,
        ) for number in range(12)]
        huge = self.context.model_copy(update={"memories": memories})
        token = sign_session_context(self.settings, huge)
        self.assertLessEqual(session_context_transfer_size(segment_id=self.segment,
                             session_context_package=token), 8192)
        self.assertLess(len(self.verify(token).memories), len(memories))
        self.assertEqual(len(huge.memories), 12)
        with self.assertRaises(ValidationError):
            self.context.model_validate({**self.context.model_dump(), "scene_epoch": 2,
                "scene_overlay": SceneOverlay(scene_id="scene-synthetic", scene_epoch=1).model_dump()})
        with self.assertRaises(ValidationError):
            sign_session_context(self.settings, self.context.model_copy(update={"scene_epoch": -1}))

    def test_individual_memory_proof_preserves_provenance_across_segments(self):
        memory = SessionMemory(memory_id="memory-synthetic", kind="user_statement", value="喜欢散步",
            source_request_id=uuid4(), segment_id=self.segment)
        proof = sign_session_memory(self.settings, memory, subject_hash="synthetic-subject",
                                    character_id=self.character)
        restored = verify_session_memory(self.settings, proof, subject_hash="synthetic-subject",
                                         character_id=self.character)
        self.assertEqual(restored, memory)
        self.assertEqual(restored.kind, "user_statement")
        self.assertLessEqual(len(proof), 1536)
        with self.assertRaises(PublicSecurityError):
            verify_session_memory(self.settings, proof, subject_hash="other", character_id=self.character)
        with self.assertRaises(PublicSecurityError):
            self.verify(proof)

    def test_legacy_near_limit_token_remains_verifiable_but_new_signing_reserves_wrapper(self):
        # Reproduce the previous token format without the new signer/transfer
        # limit, so this is an actual backward-verification regression.
        legacy_token = None
        legacy_context = None
        for width in range(1, 161):
            memories = [SessionMemory(memory_id=f"memory-{number:04d}", kind="user_statement",
                value="x" * width, source_request_id=uuid4(), segment_id=self.segment,
                source_message_ids=[f"user:{uuid4()}", f"reply:{uuid4()}"])
                for number in range(12)]
            candidate = self.context.model_copy(update={"memories": memories})
            raw = json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
            signature = hmac.new(self.settings.state_hmac_key,
                b"project-snow-public-session-context-1\x00" + raw, hashlib.sha256).digest()
            encoded = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
            token = encoded(raw) + "." + encoded(signature)
            if len(token) <= 8192 and session_context_transfer_size(
                segment_id=self.segment, session_context_package=token,
            ) > 8192:
                legacy_token, legacy_context = token, candidate
                break
        self.assertIsNotNone(legacy_token)
        self.assertEqual(self.verify(legacy_token), legacy_context)
        fresh = sign_session_context(self.settings, legacy_context)
        self.assertLessEqual(session_context_transfer_size(segment_id=self.segment,
                             session_context_package=fresh), 8192)
        self.assertEqual(len(legacy_context.memories), 12)
        self.assertLess(len(self.verify(fresh).memories), 12)

    def test_utf8_transfer_counts_full_receipts_and_wrapper_not_original_chat(self):
        memory = SessionMemory(memory_id="unicode-memory", kind="user_statement", value="中文🙂",
            source_request_id=uuid4(), segment_id=self.segment)
        proof = sign_session_memory(self.settings, memory, subject_hash="synthetic-subject",
                                    character_id=self.character)
        candidates = [{"memory": memory.model_dump(mode="json"), "proof": proof}]
        envelope = DialogueV2ChatRequest(chat=ChatRequest(request_id=uuid4(), provider="openai",
            credential="synthetic-credential-value", model="synthetic", character_id=self.character,
            message="hello", history_summary="原请求" * 1800), segment_id=self.segment,
            memory_candidates=candidates)
        expected = json.dumps({"segment_id": str(self.segment), "session_context_package": "",
                               "memory_candidates": candidates}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"))
        self.assertEqual(session_context_transfer_size(segment_id=self.segment,
                         memory_candidates=envelope.memory_candidates), len(expected.encode("utf-8")))
        self.assertGreater(len(expected.encode("utf-8")), len(expected))
        self.assertGreater(len(json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")), 8192)
        validate_session_context_transfer(envelope)

    def test_new_token_soft_target_keeps_recent_audit_copies_and_room_for_long_receipt(self):
        memories = [SessionMemory(memory_id=f"memory-{number:04d}", kind="user_statement",
            value="合成记忆" * 40, source_request_id=uuid4(), segment_id=self.segment,
            source_message_ids=[f"user:{uuid4()}", f"reply:{uuid4()}"])
            for number in range(12)]
        context = self.context.model_copy(update={"memories": memories})
        before = context.model_dump(mode="json")
        token = sign_session_context(self.settings, context)
        self.assertLessEqual(len(token), 4096)
        self.assertEqual(context.model_dump(mode="json"), before)
        retained = self.verify(token).memories
        self.assertTrue(retained)
        self.assertEqual(retained, memories[-len(retained):])
        memory = memories[-1]
        receipt = {"memory": memory.model_dump(mode="json"), "proof": sign_session_memory(
            self.settings, memory, subject_hash="synthetic-subject", character_id=self.character)}
        self.assertLessEqual(session_context_transfer_size(segment_id=self.segment,
            session_context_package=token, memory_candidates=[receipt]), 8192)
        self.assertEqual(verify_session_memory(self.settings, receipt["proof"],
            subject_hash="synthetic-subject", character_id=self.character), memory)

    def test_large_legal_scene_without_redundant_memories_can_exceed_soft_target(self):
        pending = PendingRendezvous(rendezvous_id="synthetic-rendezvous", character_id=self.character,
            location_id="park", location_name="公园", activity_id="strolling_together",
            waiting_activity="合成等候" * 60, joined_activity="合成散步" * 60)
        overlay = SceneOverlay(scene_id="synthetic-scene", analyst_location="合成地点" * 30,
            character_location="合成地点" * 30, character_activity="合成活动" * 60,
            source_event_id="e" * 160, pending_rendezvous=pending)
        context = self.context.model_copy(update={"scene_overlay": overlay})
        token = sign_session_context(self.settings, context)
        self.assertGreater(len(token), 4096)
        self.assertLessEqual(session_context_transfer_size(segment_id=self.segment,
            session_context_package=token), 8192)
        self.assertEqual(self.verify(token).scene_overlay, overlay)
        self.assertEqual(context.scene_overlay, overlay)

    def test_v1_contract_and_hash_are_unchanged(self):
        request = ChatRequest(request_id=uuid4(), provider="openai", credential="synthetic-credential-value",
            model="synthetic", character_id=self.character, message="hello")
        expected = request.model_dump(mode="json", exclude={"credential", "reasoning_effort"})
        self.assertEqual(_model_request_body(request), expected)
        self.assertEqual(_request_hash(_model_request_body(request)), _request_hash(expected))
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate({**request.model_dump(), "session_context_package": ""})
        envelope = DialogueV2ChatRequest(chat=request, segment_id=self.segment)
        self.assertEqual(_dialogue_v2_request_body(envelope)["chat"], expected)
        self.assertNotEqual(_request_hash(_dialogue_v2_request_body(envelope)), _request_hash(expected))
        with self.assertRaises(ValidationError):
            DialogueV2ChatRequest.model_validate({**envelope.model_dump(), "turn_frame": {}})


class DialogueV2RouteTests(TestCase):
    def setUp(self):
        self.settings = replace(_settings(), dialogue_domains_v2=True, scene_actions_v2=True,
                                session_memory_v2=True, verified_block_stream=True)
        self.store = PublicStore(self.settings.database_url)
        self.store.create_schema()
        self.calls = 0
        self.actions = 0
        self.segment = uuid4()
        self.service = SimpleNamespace(validate_chat=Mock(), chat=self.generate, scene_action=self.scene)
        self.app = create_app(self.settings, Settings.from_environment(), self.store,
                              dialogue_v2_service=self.service)
        self.directory = TemporaryDirectory()
        views = Path(self.directory.name) / "character_views.jsonl"
        views.write_text("".join(json.dumps({"character_id": item.character_id}) + "\n"
                                 for item in MVP_CHARACTERS), encoding="utf-8")
        self.app.state.chat_service.mvp.views_path = views
        self.app.state.chat_service.mvp._views_cache = None
        self.client = TestClient(self.app)
        self.character = MVP_CHARACTERS[0].character_id
        self.headers = {"Origin": "http://testserver", "X-Snow-Stream-Version": "2"}
        issued = self.client.post("/public/v1/byok/session", headers=self.headers, json={
            "provider": "openai", "api_key": "sk-synthetic-no-network", "turnstile_token": "development-bypass",
            "accepted_transit_notice": True, "accepted_cost_notice": True, "accepted_local_history_notice": True,
        })
        self.credential = issued.json()["credential"]
        self.subject = subject_hash(self.client.cookies.get(_anonymous_cookie_name(self.settings)))

    def tearDown(self):
        self.client.close()
        self.app.state.chat_service.close()
        asyncio.run(self.app.state.async_store.close())
        self.directory.cleanup()

    def envelope(self):
        return {"protocol": "public-dialogue-v2", "segment_id": str(self.segment),
            "chat": {"request_id": str(uuid4()), "provider": "openai", "credential": self.credential,
                "model": "synthetic", "character_id": self.character, "message": "hello"}}

    def metadata(self, subject):
        context = new_session_context(self.settings, subject_hash=subject, character_id=self.character,
                                      segment_id=self.segment, catalog_version="locations-1")
        return {"session_context_package": sign_session_context(self.settings, context),
                "session_memories": [], "scene_action": {"status": "not_requested"}}

    def world_package(self, subject, revision):
        now = datetime.now(UTC)
        world = StatePayload(data_version=self.settings.data_version, revision=revision,
            subject_binding=subject, schedule_date=now.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
            generated_at=now.isoformat(), expires_at=(now + timedelta(hours=12)).isoformat())
        return sign_state(self.settings, world.model_dump(mode="json"))

    async def generate(self, envelope, subject, provider, api_key, *, gate_reserved=False):
        self.calls += 1
        return {"request_id": str(envelope.chat.request_id), "character_id": self.character,
                "answer": "synthetic reply", "content_blocks": [{"type": "message", "text": "synthetic reply"}],
                "communication_channel": "text", "state_package": self.world_package(subject, 1),
                "diagnostics": {"timings_ms": {"provider_http_calls": 1}}, **self.metadata(subject)}

    def scene(self, envelope, subject):
        self.actions += 1
        return {"request_id": str(envelope.request_id), "character_id": self.character,
                "state_package": self.world_package(subject, self.actions),
                **self.metadata(subject), "scene_action": {"status": "applied", "action": envelope.action}}

    def post(self, body, *, recovery=False):
        headers = {**self.headers, **({"X-Snow-Recovery-Only": "1"} if recovery else {})}
        return self.client.post("/public/v2/chat/stream", headers=headers, json=body)

    def large_candidates(self):
        result = []
        for number in range(12):
            memory = SessionMemory(memory_id=f"budget-memory-{number:04d}", kind="user_statement",
                value="合成记忆" * 40, source_request_id=uuid4(), segment_id=self.segment,
                source_message_ids=[f"user:{uuid4()}", f"reply:{uuid4()}"])
            result.append({"memory": memory.model_dump(mode="json"), "proof": sign_session_memory(
                self.settings, memory, subject_hash=self.subject, character_id=self.character)})
        return result

    def test_oversize_context_chat_and_scene_are_rejected_before_claim_or_model(self):
        candidates = self.large_candidates()
        body = {**self.envelope(), "memory_candidates": candidates}
        scene_body = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()),
            "character_id": self.character, "action": "resolve", "segment_id": str(self.segment),
            "memory_candidates": candidates}
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            chat_response = self.post(body)
            scene_response = self.client.post("/public/v2/scene/action", headers=self.headers, json=scene_body)
            claim.assert_not_called()
        for response in (chat_response, scene_response):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["code"], "session_context_invalid")
            self.assertFalse(response.json()["detail"]["retryable"])
        self.service.validate_chat.assert_not_called()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.actions, 0)

    def test_unicode_context_cannot_pass_by_counting_characters(self):
        candidates = self.large_candidates()
        subset = None
        for count in range(1, len(candidates) + 1):
            context = {"segment_id": str(self.segment), "session_context_package": "",
                       "memory_candidates": candidates[:count]}
            serialized = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(serialized) <= 8192 < len(serialized.encode("utf-8")):
                subset = candidates[:count]
                break
        self.assertIsNotNone(subset)
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            response = self.post({**self.envelope(), "memory_candidates": subset})
            claim.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, 0)

    def test_underbudget_context_does_not_limit_the_original_chat_request(self):
        body = {**self.envelope(), "memory_candidates": self.large_candidates()[:1]}
        body["chat"]["history_summary"] = "原请求" * 1800
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertIn("synthetic reply", response.text)
        self.assertEqual(self.calls, 1)

    def test_original_oversize_context_snapshots_recover_without_rewriting_or_claiming(self):
        candidates = self.large_candidates()
        body = {**self.envelope(), "memory_candidates": candidates}
        request_id = body["chat"]["request_id"]
        envelope = DialogueV2ChatRequest.model_validate(body)
        request_hash = _request_hash(_dialogue_v2_request_body(envelope))
        self.store.claim_request(request_id, self.subject, request_hash)
        self.store.complete_request(request_id, {"request_id": request_id,
            "character_id": self.character, "communication_channel": "text", "answer": "saved reply",
            "content_blocks": [{"type": "message", "text": "saved reply"}], **self.metadata(self.subject)})
        scene_body = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()),
            "character_id": self.character, "action": "resolve", "segment_id": str(self.segment),
            "memory_candidates": candidates}
        scene_hash = _request_hash(SceneActionRequest.model_validate(scene_body).model_dump(mode="json"))
        cache_id = "scene-action:" + scene_body["request_id"]
        self.store.claim_request(cache_id, self.subject, scene_hash)
        self.store.complete_request(cache_id, {"request_id": scene_body["request_id"],
            "character_id": self.character, **self.metadata(self.subject), "scene_action": {"status": "applied"}})
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            chat = self.post(body, recovery=True)
            scene = self.client.post("/public/v2/scene/action",
                headers={**self.headers, "X-Snow-Recovery-Only": "1"}, json=scene_body)
            claim.assert_not_called()
        self.assertEqual(chat.status_code, 200)
        self.assertIn("saved reply", chat.text)
        self.assertEqual(scene.status_code, 200)
        self.assertTrue(scene.json()["idempotent_replay"])
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.actions, 0)
        self.service.validate_chat.assert_not_called()

    def test_v2_final_context_replay_is_exactly_once(self):
        body = self.envelope()
        first = self.post(body)
        self.assertEqual(first.status_code, 200)
        self.assertIn("session_context_package", first.text)
        self.assertNotIn("event: committed_block", first.text)
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            replay = self.post(body, recovery=True)
            claim.assert_not_called()
        self.assertEqual(replay.status_code, 200)
        self.assertIn('"idempotent_replay":true', replay.text)
        self.assertEqual(self.calls, 1)
        self.service.validate_chat.assert_called_once()
        saved = self.store.request_result(body["chat"]["request_id"], self.subject)
        world = StatePayload.model_validate(verify_state(self.settings, saved["state_package"]))
        self.assertEqual(world.subject_binding, self.subject)
        self.assertEqual(world.schedule_date, datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat())

    def test_foreign_subject_generated_world_is_rejected_before_publication(self):
        async def generate_foreign(*args, **kwargs):
            result = await self.generate(*args, **kwargs)
            return {**result, "state_package": self.world_package("synthetic-foreign-subject", 1)}

        self.service.chat = generate_foreign
        result = self.post(self.envelope())
        self.assertIn("event: error", result.text)
        self.assertNotIn("synthetic reply", result.text)
        self.assertNotIn("session_context_package", result.text)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM public_rendezvous_authority")).scalar_one(), 0)

    def test_foreign_subject_scene_world_is_rejected_without_committing(self):
        def scene_foreign(*args, **kwargs):
            result = self.scene(*args, **kwargs)
            return {**result, "state_package": self.world_package("synthetic-foreign-subject", 1)}

        self.service.scene_action = scene_foreign
        body = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()),
                "character_id": self.character, "action": "join_character", "segment_id": str(self.segment)}
        result = self.client.post("/public/v2/scene/action", headers=self.headers, json=body)
        self.assertEqual(result.status_code, 401)
        self.assertNotIn("session_context_package", result.text)
        self.assertIsNone(self.store.request_result("scene-action:" + body["request_id"], self.subject))
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM public_rendezvous_authority")).scalar_one(), 0)

    def test_recovery_missing_expired_conflict_never_claims_or_generates(self):
        body = self.envelope()
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            missing = self.post(body, recovery=True)
            self.assertEqual(missing.status_code, 410)
            claim.assert_not_called()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.post(body).status_code, 200)
        changed = json.loads(json.dumps(body))
        changed["chat"]["message"] = "changed snapshot"
        self.assertEqual(self.post(changed, recovery=True).status_code, 409)
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_cache SET expires_at = :old WHERE request_id = :id"),
                {"old": datetime.now(UTC) - timedelta(minutes=1), "id": body["chat"]["request_id"]})
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            expired = self.post(body, recovery=True)
            self.assertEqual(expired.status_code, 410)
            claim.assert_not_called()
        self.assertEqual(self.calls, 1)
        with self.store.begin() as connection:
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM public_request_cache")).scalar_one(), 1)

    def test_cache_failure_does_not_publish_context_or_valid_body(self):
        with patch.object(self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic")):
            response = self.post(self.envelope())
        self.assertEqual(response.status_code, 200)
        self.assertIn("public_database_unavailable", response.text)
        self.assertNotIn("synthetic reply", response.text)
        self.assertNotIn("session_context_package", response.text)
        self.assertEqual(self.calls, 1)

    def test_scene_action_is_zero_model_and_recovery_does_not_execute(self):
        body = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()),
                "character_id": self.character, "action": "join_character", "segment_id": str(self.segment)}
        result = self.client.post("/public/v2/scene/action", headers=self.headers, json=body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["scene_action"], {"status": "applied", "action": "join_character"})
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            replay = self.client.post("/public/v2/scene/action",
                headers={**self.headers, "X-Snow-Recovery-Only": "1"}, json=body)
            claim.assert_not_called()
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(self.actions, 1)
        self.assertEqual(self.calls, 0)

    def test_context_rejection_happens_before_claim_and_model(self):
        self.service.validate_chat.side_effect = PublicSecurityError("Session context scope does not match request")
        body = self.envelope()
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            response = self.post(body)
            claim.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["request_id"], body["chat"]["request_id"])
        self.assertFalse(response.json()["detail"]["retryable"])
        self.assertEqual(self.calls, 0)

    def test_feedback_scene_namespace_remains_subject_character_and_medium_scoped(self):
        reference = str(uuid4())
        self.store.claim_request("scene-action:" + reference, self.subject, "a" * 64)
        self.store.complete_request("scene-action:" + reference, {
            "request_id": reference, "character_id": self.character,
            "communication_channel": "in_person", "usage": {"provider_calls": 0},
            "diagnostics": {"timings_ms": {"provider_http_calls": 0}},
        })
        for char, medium, owned in ((self.character, "in_person", True),
                                   (MVP_CHARACTERS[1].character_id, "in_person", False),
                                   (self.character, "text", False)):
            response = self.client.post("/public/v1/feedback", headers=self.headers, json={
                "request_id": str(uuid4()), "chat_request_id": reference,
                "character_id": char, "request_stage": medium,
                "body": "Synthetic scene feedback " + char + medium,
                "turnstile_token": "development-bypass",
            })
            self.assertEqual(response.status_code, 200)
            context = self.store.feedback_rows()[0]["context"]
            if owned:
                self.assertEqual(context["related_request_kind"], "scene_action")
                self.assertEqual(context["generation_diagnostics"]["timings_ms"]["provider_http_calls"], 0)
            else:
                self.assertNotIn("related_request_kind", context)
        self.assertEqual(self.actions, 0)
        self.assertEqual(self.calls, 0)

    def test_lease_loss_recovery_reports_interruption_without_mutation(self):
        body = self.envelope()
        request_id = body["chat"]["request_id"]
        request_hash = _request_hash(_dialogue_v2_request_body(DialogueV2ChatRequest.model_validate(body)))
        self.store.claim_request(request_id, self.subject, request_hash, owner_token="synthetic-owner")
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_leases SET lease_expires_at = :old WHERE request_id = :id"),
                {"old": datetime.now(UTC) - timedelta(minutes=1), "id": request_id})
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            response = self.post(body, recovery=True)
            claim.assert_not_called()
        self.assertIn("generation_interrupted", response.text)
        self.assertEqual(self.calls, 0)
        with self.store.begin() as connection:
            self.assertEqual(connection.execute(text("SELECT status FROM public_request_cache WHERE request_id = :id"),
                {"id": request_id}).scalar_one(), "processing")

    def test_recovery_still_works_when_feature_switches_are_off(self):
        body = self.envelope()
        self.assertEqual(self.post(body).status_code, 200)
        disabled = create_app(replace(_settings(), enabled_providers=()), Settings.from_environment(), self.store,
                              dialogue_v2_service=self.service)
        client = TestClient(disabled)
        client.cookies.set(_anonymous_cookie_name(self.settings),
                           self.client.cookies.get(_anonymous_cookie_name(self.settings)))
        try:
            with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
                recovered = client.post("/public/v2/chat/stream",
                    headers={**self.headers, "X-Snow-Recovery-Only": "1"}, json=body)
                claim.assert_not_called()
            self.assertEqual(recovered.status_code, 200)
            self.assertIn("synthetic reply", recovered.text)
            self.assertIn("session_context_package", recovered.text)
            self.assertEqual(self.calls, 1)
            self.service.validate_chat.assert_called_once()
        finally:
            client.close()
            disabled.state.chat_service.close()
            asyncio.run(disabled.state.async_store.close())

    def test_default_features_disabled_and_capabilities_preserve_v1(self):
        disabled = create_app(_settings(), Settings.from_environment(), self.store,
                              dialogue_v2_service=self.service)
        client = TestClient(disabled)
        try:
            config = client.get("/public/v1/config").json()
            for name in ("dialogue_domains_v2", "scene_actions_v2", "session_memory_v2", "ui_snow_adv"):
                self.assertFalse(config["features"][name])
            self.assertEqual(client.get("/public/v1/build-info").json()["api_schema"], "public-v1")
            denied = client.post("/public/v2/chat/stream", headers=self.headers, json=self.envelope())
            self.assertEqual(denied.status_code, 503)
            self.assertEqual(self.calls, 0)
            missing = client.post("/public/v2/chat/stream",
                headers={**self.headers, "X-Snow-Recovery-Only": "1"}, json=self.envelope())
            self.assertEqual(missing.status_code, 410)
        finally:
            client.close()
            disabled.state.chat_service.close()
            asyncio.run(disabled.state.async_store.close())
