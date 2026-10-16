"""Real typed joins, signed tickets and paid-claim faults; synthetic providers only."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request

from backend.snow_app.arrival_ticket import verify_arrival_ticket
from backend.snow_app.config import Settings
from backend.snow_app.dialogue_core import GenerationGate, active_generation_budget
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_contracts import PresenceArrivalV2Request, SceneActionRequest, SceneInvitation
from backend.snow_app.public_main import create_app
from backend.snow_app.public_providers import PROVIDERS
from backend.snow_app.public_security import PublicSecurityError
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from backend.snow_app.session_context import sign_session_context
from tests import test_dialogue_v2_service as fixtures


def arrival_payload(scene, joined):
    return PresenceArrivalV2Request(
        arrival_id=joined["request_id"], arrival_ticket=joined["arrival_ticket"],
        provider="openai", credential="synthetic-credential-value", model="synthetic",
        character_id=scene.character.character_id, segment_id=scene.segment,
        state_package=joined["state_package"], session_context_package=joined["session_context_package"])


class ArrivalV2DurabilityTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scene = fixtures.DialogueV2ServiceTests(methodName="runTest")
        self.scene.setUp()
        self.legacy = self.scene.legacy
        self.legacy.gate = GenerationGate()
        self.legacy.media = Mock()
        self.legacy.media.verify.return_value = {"status": "unavailable"}
        self.legacy.stickers = Mock()
        self.legacy.stickers.verify.return_value = {"status": "unavailable"}
        self.legacy.prepare_presence_arrival = Mock(side_effect=AssertionError("no legacy reroll or movement"))
        self.legacy.finish_presence_arrival = AsyncMock(return_value={
            "model_called": True, "terminal_error": "", "reaction": {
                "message_id": "synthetic-arrival", "content_blocks": [{"type": "speech", "text": "你好。"}],
                "usage": {"provider_calls": 1}}})
        engine = getattr(self, "store_engine", None)
        self.store = PublicStore("sqlite+pysqlite:///:memory:" if engine is None else "", engine=engine)
        if engine is None:
            self.store.create_schema()
        self.app = create_app(self.scene.settings, Settings(Path("unused"), Path("unused"), False, "", []),
                              self.store, self.legacy, self.scene.service)
        self.routes = {route.path: route.endpoint for route in self.app.routes if hasattr(route, "endpoint")}
        self.request = Request({"type": "http", "headers": [],
                                "state": {"anonymous_id": "synthetic", "subject_hash": self.scene.subject}})
        self.endpoint = self.routes["/public/v2/presence/arrival"]
        self.credential = patch("backend.snow_app.public_main.open_byok_credential",
                                return_value={"api_key": "synthetic"})
        self.credential.start()
        self.joined = await self.join()
        self.payload = arrival_payload(self.scene, self.joined)

    async def asyncTearDown(self):
        self.credential.stop()
        await self.app.state.async_store.close()
        await self.app.state.provider_http.close()
        self.store.engine.dispose()

    async def join(self, previous=None, action="join_character"):
        previous = previous or self.scene.action("resolve")
        request = SceneActionRequest(request_id=uuid4(), character_id=self.scene.character.character_id,
            segment_id=self.scene.segment, action=action, state_package=previous["state_package"],
            session_context_package=previous["session_context_package"])
        return await self.routes["/public/v2/scene/action"](self.request, request)

    def recovery(self):
        return Request({"type": "http", "headers": [(b"x-snow-recovery-only", b"1")],
                        "state": {"anonymous_id": "synthetic", "subject_hash": self.scene.subject}})

    async def test_join_is_zero_model_and_repeat_or_recovery_only_replays(self):
        self.assertFalse(self.joined["model_called"])
        self.assertEqual(self.joined["usage"]["provider_calls"], 0)
        before = (self.payload.state_package, self.payload.session_context_package)
        first = await self.endpoint(self.request, self.payload)
        for request in (self.request, self.recovery()):
            replay = await self.endpoint(request, self.payload.model_copy(update={"credential": ""}))
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["reaction"], first["reaction"])
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)
        self.assertEqual(before, (self.payload.state_package, self.payload.session_context_package))
        self.assertNotIn("state_package", first)
        self.assertNotIn("session_context_package", first)
        self.assertEqual(first["arrival_id"], self.joined["request_id"])
        self.assertEqual(first["character_id"], self.scene.character.character_id)
        self.assertEqual(first["decision"], "noticed")

    async def test_missing_recovery_never_generates_or_claims(self):
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            with self.assertRaises(HTTPException) as caught:
                await self.endpoint(self.recovery(), self.payload)
            self.assertEqual(caught.exception.status_code, 410)
            claim.assert_not_called()
        self.legacy.finish_presence_arrival.assert_not_awaited()

    async def test_ticket_and_parent_join_exact_binding_rejects_before_paid(self):
        changes = [dict(arrival_id=uuid4()), dict(segment_id=uuid4()),
                   dict(character_id=self.scene.other.character_id),
                   dict(state_package=self.payload.state_package + "x"),
                   dict(session_context_package=self.payload.session_context_package + "x"),
                   dict(arrival_ticket=self.payload.arrival_ticket + "x")]
        for change in changes:
            with self.subTest(fields=list(change)):
                with self.assertRaises(HTTPException) as caught:
                    await self.endpoint(self.request, self.payload.model_copy(update=change))
                self.assertEqual(caught.exception.status_code, 422)
        other = Request({"type": "http", "headers": [],
                         "state": {"anonymous_id": "other", "subject_hash": "other"}})
        with self.assertRaises(HTTPException):
            await self.endpoint(other, self.payload)
        self.legacy.finish_presence_arrival.assert_not_awaited()

    async def test_expired_ticket_blocks_new_call_but_cached_recovery_still_works(self):
        with patch("backend.snow_app.arrival_ticket.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(UTC) + timedelta(minutes=6)
            with self.assertRaises(HTTPException) as caught:
                await self.endpoint(self.request, self.payload)
            self.assertEqual(caught.exception.status_code, 410)
        self.legacy.finish_presence_arrival.assert_not_awaited()
        await self.endpoint(self.request, self.payload)
        with patch("backend.snow_app.arrival_ticket.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(UTC) + timedelta(minutes=6)
            with patch("backend.snow_app.public_main.open_byok_credential", side_effect=AssertionError("no reopen")):
                replay = await self.endpoint(self.recovery(), self.payload.model_copy(update={"credential": ""}))
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_completed_cache_expiry_cannot_resurrect_paid_arrival(self):
        await self.endpoint(self.request, self.payload)
        later = datetime.now(UTC) + timedelta(minutes=11)
        with patch("backend.snow_app.public_store._utcnow", return_value=later), \
                patch("backend.snow_app.arrival_ticket.datetime", wraps=datetime) as clock:
            clock.now.return_value = later
            for request in (self.recovery(), self.request):
                with self.assertRaises(HTTPException) as caught:
                    await self.endpoint(request, self.payload)
                self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_changed_business_body_conflicts_after_completed_request(self):
        await self.endpoint(self.request, self.payload)
        with self.assertRaises(HTTPException) as caught:
            await self.endpoint(self.recovery(), self.payload.model_copy(update={"model": "different"}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_concurrent_duplicate_does_not_start_a_second_generation(self):
        started, release = asyncio.Event(), asyncio.Event()
        result = self.legacy.finish_presence_arrival.return_value
        async def generate(*_args):
            started.set()
            await release.wait()
            return result
        self.legacy.finish_presence_arrival.side_effect = generate
        first = asyncio.create_task(self.endpoint(self.request, self.payload))
        await started.wait()
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.endpoint(self.request, self.payload)
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            release.set()
        await first
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_lost_commit_ack_preserves_original_paid_result(self):
        complete = self.store.complete_request
        def lose_ack(*args, **kwargs):
            complete(*args, **kwargs)
            raise PublicStoreUnavailable("synthetic lost commit acknowledgement")
        with patch.object(self.store, "complete_request", side_effect=lose_ack):
            with self.assertRaises(PublicStoreUnavailable):
                await self.endpoint(self.request, self.payload)
        replay = await self.endpoint(self.recovery(), self.payload)
        self.assertEqual(replay["terminal_error"], "")
        self.assertEqual(replay["reaction"]["message_id"], "synthetic-arrival")
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_post_provider_failure_is_terminal_and_never_releases_claim(self):
        self.legacy.finish_presence_arrival.side_effect = RuntimeError("synthetic post-provider failure")
        with patch.object(self.store, "release_request", wraps=self.store.release_request) as release:
            with self.assertRaises(RuntimeError):
                await self.endpoint(self.request, self.payload)
            release.assert_not_called()
        replay = await self.endpoint(self.recovery(), self.payload)
        self.assertEqual(replay["terminal_error"], "generation_interrupted")
        self.assertTrue(replay["model_called"])
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_persistent_write_outage_reconciles_expired_lease_without_rebilling(self):
        now = datetime.now(UTC)
        with patch("backend.snow_app.public_store._utcnow", return_value=now), \
                patch.object(self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic outage")):
            with self.assertRaises(PublicStoreUnavailable):
                await self.endpoint(self.request, self.payload)
            with self.assertRaises(HTTPException) as busy:
                await self.endpoint(self.recovery(), self.payload)
            self.assertEqual(busy.exception.status_code, 409)
        self.assertEqual(self.app.state.active_request_leases, {})
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=46)):
            replay = await self.endpoint(self.recovery(), self.payload)
        self.assertEqual(replay["terminal_error"], "generation_interrupted")
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 1)

    async def test_leave_and_reenter_has_new_ticket_and_one_new_reaction(self):
        await self.endpoint(self.request, self.payload)
        left = await self.join(self.joined, "open_communicator")
        self.assertNotIn("arrival_ticket", left)
        entered = await self.join(left)
        self.assertNotEqual(entered["request_id"], self.joined["request_id"])
        await self.endpoint(self.request, arrival_payload(self.scene, entered))
        self.assertEqual(self.legacy.finish_presence_arrival.await_count, 2)


class ArrivalV2GenerationTests(TestCase):
    def setUp(self):
        self.stack = fixtures.DialogueV2RealGenerationStackTests(methodName="runTest")
        self.stack.setUp()
        self.scene, self.mvp = self.stack.fixture, self.stack.mvp
        self.joined = self.scene.action("join_character")
        self.payload = arrival_payload(self.scene, self.joined)

    def generate(self):
        prepared = self.scene.service.prepare_arrival(self.payload, self.scene.subject, self.joined)
        return self.scene.legacy._finish_presence_arrival_sync(
            prepared, self.payload, self.scene.subject, PROVIDERS["openai"], "synthetic-key")

    def test_typed_system_event_one_call_and_no_fake_user_turn_or_move(self):
        result = self.generate()
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(self.mvp._call_model.call_count, 1)
        args = self.mvp.chat.call_args
        self.assertEqual(args.args[1], "")
        self.assertTrue(args.kwargs["system_arrival"])
        self.assertFalse(args.kwargs["persist_exchange"])
        self.assertFalse(args.kwargs["remember_session"])
        self.assertEqual(args.kwargs["thinking_decision"]["max_provider_http_calls"], 1)
        context = self.mvp._prompt.call_args.args[2]
        self.assertEqual(context["arrival_event"]["source"], "verified_scene_join")
        self.assertEqual(context["analyst_content_blocks"], [])
        self.mvp.repository = Mock()
        self.mvp.repository.documents_by_id.return_value = {}
        rendered = json.loads(MVPService._prompt(self.mvp, self.scene.character, "", context))
        self.assertEqual(rendered["user_message"], "")
        self.assertIn("arrival_event", rendered)
        self.assertEqual(result["state_package"], self.joined["state_package"])

    def test_empty_output_has_no_second_paid_repair(self):
        def empty(*_args, **_kwargs):
            self.assertEqual(active_generation_budget().max_provider_calls, 1)
            self.mvp._reserve_model_call()
            return "{}", {"total_tokens": 2}
        self.mvp._call_model.side_effect = empty
        result = self.generate()
        self.assertEqual(self.mvp._call_model.call_count, 1)
        self.assertTrue(result["terminal_error"])
        self.assertIsNone(result["reaction"])
        self.assertEqual(result["usage"]["provider_calls"], 1)

    def test_guard_rejection_does_not_pay_for_a_rewrite(self):
        with patch.object(self.mvp, "_answer_guardrail_violations", return_value=["in_person_speech_contains_action"]):
            result = self.generate()
        self.assertEqual(self.mvp._call_model.call_count, 1)
        self.assertTrue(result["terminal_error"])
        self.assertIsNone(result["reaction"])
        self.assertEqual(result["usage"]["provider_calls"], 1)

    def test_ticket_cannot_be_used_for_a_different_join_or_segment(self):
        other = self.scene.action("join_character", previous=self.joined)
        with self.assertRaises(PublicSecurityError):
            self.scene.service.prepare_arrival(self.payload, self.scene.subject, other)
        with self.assertRaises(PublicSecurityError):
            verify_arrival_ticket(self.scene.settings, self.payload.model_copy(update={"segment_id": uuid4()}),
                                  self.scene.subject)

    def test_real_transport_boundary_sends_once_on_success_empty_or_http_error(self):
        for kind in ("success", "empty", "http_error"):
            with self.subTest(kind=kind):
                self.setUp()
                calls = []
                def transport(request):
                    calls.append(request)
                    if kind == "http_error":
                        return httpx.Response(503, json={"error": "synthetic unavailable"})
                    content = "{}" if kind == "empty" else json.dumps({
                        "content_blocks": [{"type": "speech", "text": "你好，今天过得怎么样？"}],
                        "state_updates": [], "expression_state": "neutral", "stage_motion": "none"})
                    return httpx.Response(200, json={"choices": [{"message": {"content": content},
                        "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
                self.mvp.settings.mvp_chat_timeout_seconds = 1
                self.mvp._call_model = MVPService._call_model.__get__(self.mvp)
                with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                    self.mvp._model_http_client = client
                    result = self.generate()
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["usage"]["provider_calls"], 1)
                self.assertEqual(bool(result["terminal_error"]), kind != "success")


class CompletedInvitationTests(TestCase):
    def setUp(self):
        self.scene = fixtures.DialogueV2ServiceTests(methodName="runTest")
        self.scene.setUp()

    def test_immediate_face_acceptance_consumes_invitation(self):
        result = self.scene.run_chat("一起去公园吧。", channel="in_person")
        self.assertEqual(result["movement_status"]["status"], "applied")
        self.assertIsNone(self.scene.context(result).active_invitation)
        self.assertIsNone(result["pending_rendezvous"])
        self.assertTrue(result["scene_state"]["co_located"])

    def test_old_exact_completed_sidecar_is_normalized_but_future_is_retained(self):
        moved = self.scene.run_chat("一起去公园吧。", channel="in_person")
        context = self.scene.context(moved)
        context.active_invitation = SceneInvitation(
            invitation_id=f"invite:{context.scene_overlay.source_event_id}", location_id="park",
            activity_id=context.scene_overlay.activity_id, status="accepted", time_scope="now")
        old = {**moved, "session_context_package": sign_session_context(self.scene.settings, context)}
        self.assertIsNone(self.scene.context(self.scene.action("resolve", previous=old)).active_invitation)
        for changes in ({"time_scope": "future"}, {"status": "clarify"},
                        {"invitation_id": f"invite:{uuid4()}"}, {"activity_id": "different_activity"}):
            with self.subTest(changes=changes):
                altered = context.model_copy(deep=True)
                altered.active_invitation = altered.active_invitation.model_copy(update=changes)
                old["session_context_package"] = sign_session_context(self.scene.settings, altered)
                self.assertIsNotNone(self.scene.context(self.scene.action("resolve", previous=old)).active_invitation)
