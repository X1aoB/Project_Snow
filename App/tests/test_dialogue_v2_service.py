"""Synthetic scene adapter regressions using real public state normalization.

The model boundary is an instrumented coroutine; no provider, runtime source
material, live browser state or user data is read.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from backend.snow_app.dialogue_v2_service import DialogueV2Service
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_contracts import (
    ChatRequest, DialogueV2ChatRequest, SceneActionRequest, SessionMemory, SessionMemoryCandidate, StatePayload,
)
from backend.snow_app.dialogue_core import DialogueEngine
from backend.snow_app.public_providers import PROVIDERS
from backend.snow_app.public_security import PublicSecurityError, sign_state, verify_state
from backend.snow_app.public_service import PublicChatService, StatelessConversationStore
from backend.snow_app.session_context import (
    sign_session_memory, verify_session_context, verify_session_memory,
)
from tests.test_public_api import _settings


class DialogueV2ServiceTests(TestCase):
    def setUp(self):
        self.settings = replace(_settings(), dialogue_domains_v2=True,
                                scene_actions_v2=True, session_memory_v2=True)
        self.subject = "synthetic-scene-subject"
        self.character = MVP_CHARACTERS[0]
        self.other = MVP_CHARACTERS[1]
        self.segment = uuid4()
        self.day = "2026-10-15"
        self.daily = StatePayload(data_version=self.settings.data_version, revision=1,
            schedule_date=self.day, schedule_revision=1, subject_binding=self.subject,
            analyst_location="基地休息区", presence={item.character_id: {
                "character_id": item.character_id, "character_name": item.display_name,
                "location": "观景区", "activity": "正在休息", "state_scope": "subject_daily",
            } for item in MVP_CHARACTERS}).model_dump()
        self.legacy = object.__new__(PublicChatService)
        self.legacy.public_settings = self.settings
        self.legacy.mvp = Mock()
        self.legacy.mvp._views.return_value = {item.character_id: {} for item in MVP_CHARACTERS}
        self.legacy._default_state = Mock(side_effect=self._default_state)
        self.calls = []
        self.disposition = "accept_now"
        self.decision_override = None
        self.model_decision_missing = False
        self.reply = "好呀，我愿意。"
        self.reply_blocks = None
        self.memory_candidates = []
        self.terminal = ""
        self.legacy.chat = self._generate
        self.service = DialogueV2Service(self.legacy, self.settings)

    def _default_state(self, subject):
        state = json.loads(json.dumps(self.daily))
        state["schedule_date"] = self.day
        state["subject_binding"] = subject
        return state

    async def _generate(self, request, subject, provider, api_key, **kwargs):
        self.calls.append((request, kwargs.get("v2_context")))
        state = self.legacy._normalized_state(request.state_package, subject)
        state["revision"] += 1
        decision = None
        catalog = kwargs.get("v2_context", {}).get("movement_catalog") or []
        if catalog:
            decision = {**catalog[0], "decision": self.disposition}
        if self.decision_override is not None:
            decision = self.decision_override
        if self.model_decision_missing:
            decision = None
        return {"request_id": str(request.request_id), "character_id": request.character_id,
            "communication_channel": request.communication_channel, "answer": self.reply,
            "content_blocks": self.reply_blocks or [{"type": "message" if request.communication_channel == "text" else "speech",
                                "text": self.reply}],
            "state_package": sign_state(self.settings, state), "terminal_error": self.terminal,
            "_v2_interaction_decision": decision, "_v2_memory_candidates": self.memory_candidates,
            "usage": {"provider_calls": 1}}

    def request(self, message="我们继续聊。", *, previous=None, channel="text", **changes):
        initial = json.loads(json.dumps(self.daily))
        if channel == "in_person":
            initial["analyst_location"] = initial["presence"][self.character.character_id]["location"]
        values = {"request_id": uuid4(), "provider": "openai", "credential": "synthetic-credential-value",
            "model": "synthetic", "character_id": self.character.character_id, "message": message,
            "communication_channel": channel,
            "state_package": previous["state_package"] if previous else sign_state(self.settings, initial)}
        values.update(changes)
        chat = ChatRequest.model_validate(values)
        return DialogueV2ChatRequest(chat=chat, segment_id=self.segment,
            session_context_package=previous.get("session_context_package", "") if previous else "")

    def run_chat(self, message="我们继续聊。", **kwargs):
        return asyncio.run(self.service.chat(self.request(message, **kwargs), self.subject,
                                            None, "synthetic-key", gate_reserved=True))

    def action(self, action, *, previous=None, **changes):
        values = {"request_id": uuid4(), "character_id": self.character.character_id,
            "action": action, "segment_id": self.segment,
            "state_package": previous["state_package"] if previous else sign_state(self.settings, self.daily),
            "session_context_package": previous.get("session_context_package", "") if previous else ""}
        values.update(changes)
        return self.service.scene_action(SceneActionRequest.model_validate(values), self.subject)

    def context(self, result):
        return verify_session_context(self.settings, result["session_context_package"],
            subject_hash=self.subject, character_id=self.character.character_id,
            segment_id=self.segment, catalog_version=self.service.catalog_version)

    def test_all_public_destinations_are_resolved_for_text_and_face(self):
        self.assertEqual(len(self.service.catalog), 15)
        self.assertNotIn("base_canteen", self.service.catalog)
        for channel in ("text", "in_person"):
            for location_id in self.service.catalog:
                with self.subTest(channel=channel, location_id=location_id):
                    definition = self.service._definition(location_id, self.character.character_id)
                    alias = next(iter(definition["explicit_aliases"]))
                    result = self.run_chat(f"要不要一起去{alias}？", channel=channel,
                                           movement_location_id=location_id)
                    world = verify_state(self.settings, result["state_package"])
                    self.assertEqual(result["scene_action"]["status"],
                                     "character_waiting" if channel == "text" else "applied")
                    self.assertEqual(world["presence"][self.character.character_id]["location"], definition["location"])
                    self.assertEqual(world["analyst_location"],
                                     "基地休息区" if channel == "text" else definition["location"])
        self.assertEqual(len(self.calls), 30)

    def test_structured_acceptance_is_independent_of_fixed_reply_phrase(self):
        for reply in ("好呀。", "乐意奉陪。", "就这么定了。", "嗯，今天正适合出去。", "没问题，正适合出去。"):
            with self.subTest(reply=reply):
                self.reply = reply
                result = self.run_chat("一起去公园吧。")
                self.assertEqual(result["scene_action"]["status"], "character_waiting")
                self.assertEqual(result["answer"], reply)
        self.assertEqual(len(self.calls), 5)

    def test_decline_defer_and_target_mismatch_do_not_move(self):
        for decision, expected in (("decline", "declined"), ("defer", "deferred"), ("clarify", "clarify")):
            with self.subTest(decision=decision):
                self.disposition = decision
                result = self.run_chat("一起去公园吧。")
                world = verify_state(self.settings, result["state_package"])
                self.assertEqual(world["presence"][self.character.character_id]["location"], "观景区")
                self.assertFalse(world["pending_rendezvous"])
                self.assertEqual(self.context(result).active_invitation.status, expected)
        self.disposition = "accept_now"
        self.decision_override = {"decision": "accept_now", "location_id": "shopping_mall", "activity_id": "shopping_together"}
        result = self.run_chat("一起去公园吧。")
        self.assertEqual(result["scene_action"]["status"], "clarify")
        self.assertFalse(result["pending_rendezvous"])

    def test_explicit_reply_refusal_does_not_execute_conflicting_accept_candidate(self):
        for reply in ("我不想去公园。", "我不去公园。", "今天先不去了。", "今天不方便，改天再去。"):
            with self.subTest(reply=reply):
                self.reply = reply
                result = self.run_chat("一起去公园吧。")
                world = verify_state(self.settings, result["state_package"])
                self.assertEqual(world["presence"][self.character.character_id]["location"], "观景区")
                self.assertFalse(world["pending_rendezvous"])

    def test_historical_quoted_negative_and_future_invitations_do_not_move_now(self):
        for text in ("昨天一起去过公园。", "假如一起去公园会怎么样？", "不要一起去公园。",
                     "他说“我们一起去公园”。", "他说‘我们一起去公园’。", "`我们一起去公园`只是引用。",
                     "剧情当时一起去公园。", "明天一起去公园吧。"):
            with self.subTest(text=text):
                result = self.run_chat(text)
                world = verify_state(self.settings, result["state_package"])
                self.assertEqual(world["analyst_location"], "基地休息区")
                self.assertEqual(world["presence"][self.character.character_id]["location"], "观景区")
                self.assertFalse(world["pending_rendezvous"])
                self.assertFalse(any(item.kind == "shared_scene_event" for item in self.context(result).memories))

    def test_pending_invitation_survives_more_than_history_window_and_reopen(self):
        result = self.run_chat("一起去商场吧。")
        pending = result["pending_rendezvous"]
        for _ in range(13):
            result = self.run_chat("今天感觉如何？", previous=result)
            self.assertEqual(result["pending_rendezvous"]["rendezvous_id"], pending["rendezvous_id"])
        calls = len(self.calls)
        reopened = self.action("resolve", previous=result)
        self.assertEqual(reopened["pending_rendezvous"]["rendezvous_id"], pending["rendezvous_id"])
        joined = self.action("join_rendezvous", previous=reopened, rendezvous_id=pending["rendezvous_id"])
        self.assertEqual(joined["communication_channel"], "in_person")
        self.assertTrue(joined["scene_state"]["co_located"])
        self.assertIsNone(joined["pending_rendezvous"])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(joined["usage"]["provider_calls"], 0)
        self.assertEqual(joined["session_memories"][0]["memory"]["source_message_ids"], [f"reply:{joined['request_id']}"])

    def test_information_questions_and_personal_reports_are_not_invitations(self):
        for text in ("来我房间的是谁？", "谁去公园？", "我去公园只是为了买水，不是邀请你。",
                     "我去公园买水。", "去公园怎么走？", "原作里，来我房间的是谁？"):
            with self.subTest(text=text):
                result = self.run_chat(text)
                self.assertFalse(result["pending_rendezvous"])
                self.assertEqual(result["scene_action"]["status"], "not_requested")

    def test_inviting_another_person_does_not_move_current_contact(self):
        for message in (f"{self.other.display_name}陪我去公园吧。",
                        f"我和{self.other.display_name}一起去公园吧。"):
            with self.subTest(message=message):
                result = self.run_chat(message)
                self.assertFalse(result["pending_rendezvous"])
                self.assertEqual(result["scene_action"]["status"], "not_requested")
        result = self.run_chat(f"你陪我去公园找{self.other.display_name}吧。")
        self.assertEqual(result["pending_rendezvous"]["location_id"], "park")
        world = verify_state(self.settings, result["state_package"])
        self.assertEqual(world["presence"][self.other.character_id]["location"], "观景区")

    def test_explicit_replacement_destination_wins_over_cancel_of_previous_place(self):
        waiting = self.run_chat("一起去公园吧。")
        result = self.run_chat("不去公园，那就去商场吧。", previous=waiting)
        self.assertEqual(result["pending_rendezvous"]["location_id"], "shopping_mall")
        self.assertEqual(len(self.calls), 2)

    def test_refusal_of_other_place_or_unrelated_cancellation_keeps_active_rendezvous(self):
        waiting = self.run_chat("一起去公园吧。")
        for text in ("购物中心不去了。", "取消订单吧。", "明天不去公园。", "不要取消吧。"):
            with self.subTest(text=text):
                result = self.run_chat(text, previous=waiting)
                self.assertEqual(result["pending_rendezvous"]["rendezvous_id"], waiting["pending_rendezvous"]["rendezvous_id"])

    def test_refusal_of_story_or_other_destination_does_not_override_current_acceptance(self):
        for reply in ("主线时我不想去港口。不过现在我愿意去商场。", "不去公园，去商场吧。"):
            with self.subTest(reply=reply):
                self.reply = reply
                result = self.run_chat("现在一起去商场吧。")
                self.assertEqual(result["pending_rendezvous"]["location_id"], "shopping_mall")

    def test_communicator_preserves_pending_and_cancel_does_not_teleport(self):
        waiting = self.run_chat("一起去公园吧。")
        pending = waiting["pending_rendezvous"]
        opened = self.action("open_communicator", previous=waiting)
        self.assertEqual(opened["pending_rendezvous"], pending)
        self.assertEqual(opened["communication_channel"], "text")
        cancelled = self.action("cancel_rendezvous", previous=opened, rendezvous_id=pending["rendezvous_id"])
        self.assertIsNone(cancelled["pending_rendezvous"])
        world = verify_state(self.settings, cancelled["state_package"])
        self.assertEqual(world["analyst_location"], "基地休息区")
        self.assertNotIn("等分析员", world["presence"][self.character.character_id]["activity"])
        self.assertIsNone(self.context(cancelled).active_invitation)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            self.action("join_rendezvous", previous=cancelled, rendezvous_id=pending["rendezvous_id"])

    def test_natural_cancel_ends_waiting_and_pending_without_extra_classification_call(self):
        for message in ("不去了。", "取消吧。"):
            with self.subTest(message=message):
                waiting = self.run_chat("一起去公园吧。")
                calls = len(self.calls)
                cancelled = self.run_chat(message, previous=waiting)
                world = verify_state(self.settings, cancelled["state_package"])
                self.assertFalse(world["pending_rendezvous"])
                invitation = self.context(cancelled).active_invitation
                self.assertTrue(invitation is None or invitation.status == "declined")
                self.assertNotIn("等分析员", world["presence"][self.character.character_id]["activity"])
                self.assertLessEqual(len(self.calls) - calls, 1)
                continued = self.run_chat("那就走吧。", previous=cancelled)
                self.assertFalse(continued["pending_rendezvous"])
                self.assertNotIn(continued["scene_action"]["status"], {"applied", "character_waiting"})

    def test_explicit_cancel_and_defer_do_not_require_model_decision_metadata(self):
        for message in ("取消吧。", "那就明天吧。"):
            with self.subTest(message=message):
                waiting = self.run_chat("一起去公园吧。")
                self.model_decision_missing = True
                result = self.run_chat(message, previous=waiting)
                self.model_decision_missing = False
                self.assertFalse(result["pending_rendezvous"])
                invitation = self.context(result).active_invitation
                if message == "取消吧。":
                    self.assertTrue(invitation is None or invitation.status == "declined")
                else:
                    self.assertEqual(invitation.status, "deferred")
                    self.assertEqual(invitation.time_scope, "future")

    def test_natural_defer_ends_current_wait_and_retains_future_invitation_after_long_chat(self):
        waiting = self.run_chat("一起去公园吧。")
        calls = len(self.calls)
        delayed = self.run_chat("那就明天吧。", previous=waiting)
        context = self.context(delayed)
        self.assertEqual(context.active_invitation.time_scope, "future")
        self.assertEqual(context.active_invitation.status, "deferred")
        world = verify_state(self.settings, delayed["state_package"])
        self.assertFalse(world["pending_rendezvous"])
        self.assertNotIn("等分析员", world["presence"][self.character.character_id]["activity"])
        self.assertEqual(world["analyst_location"], "基地休息区")
        self.assertLessEqual(len(self.calls) - calls, 1)
        for _ in range(13):
            delayed = self.run_chat("今天感觉如何？", previous=delayed)
            self.assertEqual(self.context(delayed).active_invitation.status, "deferred")
            self.assertFalse(delayed["pending_rendezvous"])

    def test_future_and_deferred_invitation_can_be_cancelled_without_join_or_model(self):
        future = self.run_chat("明天一起去公园吧。")
        self.assertFalse(future["pending_rendezvous"])
        world_before = verify_state(self.settings, future["state_package"])
        invitation = self.context(future).active_invitation
        with self.assertRaises(ValueError):
            self.action("join_rendezvous", previous=future, rendezvous_id=invitation.invitation_id)
        with self.assertRaises(ValueError):
            self.action("cancel_rendezvous", previous=future, rendezvous_id="stale-invitation")
        cancelled = self.action("cancel_rendezvous", previous=future, rendezvous_id=invitation.invitation_id)
        self.assertIsNone(self.context(cancelled).active_invitation)
        self.assertIsNone(self.context(cancelled).scene_overlay)
        world_after = verify_state(self.settings, cancelled["state_package"])
        self.assertEqual(world_after["presence"], world_before["presence"])
        self.assertEqual(world_after["analyst_location"], world_before["analyst_location"])
        self.assertEqual(cancelled["usage"]["provider_calls"], 0)
        self.assertEqual(len(self.calls), 1)
        waiting = self.run_chat("一起去公园吧。")
        deferred = self.run_chat("那就明天吧。", previous=waiting)
        invitation = self.context(deferred).active_invitation
        cancelled = self.action("cancel_rendezvous", previous=deferred, rendezvous_id=invitation.invitation_id)
        self.assertIsNone(self.context(cancelled).active_invitation)
        self.assertFalse(cancelled["pending_rendezvous"])
        self.assertEqual(len(self.calls), 3)

    def test_accepted_future_replaces_waiting_without_teleport_or_stale_overlay(self):
        waiting = self.run_chat("一起去公园吧。")
        future = self.run_chat("明天一起去商场吧。", previous=waiting)
        self.assertFalse(future["pending_rendezvous"])
        context = self.context(future)
        self.assertEqual(context.active_invitation.location_id, "shopping_mall")
        self.assertEqual(context.active_invitation.time_scope, "future")
        self.assertIsNone(context.scene_overlay.pending_rendezvous)
        self.assertEqual(context.scene_overlay.character_location, "公园")
        self.assertNotIn("等分析员", context.scene_overlay.character_activity)
        self.assertEqual(context.scene_overlay.analyst_location, "基地休息区")
        self.assertEqual(len(self.calls), 2)

    def test_matching_future_acceptance_is_not_misread_as_current_refusal(self):
        for reply in ("好呀，明天一起去公园。", "今天我没空，不过明天一起去公园。"):
            with self.subTest(reply=reply):
                self.reply = reply
                future = self.run_chat("明天一起去公园吧。")
                self.assertEqual(self.context(future).active_invitation.status, "accepted")
                self.assertEqual(self.context(future).active_invitation.time_scope, "future")
                self.assertFalse(future["pending_rendezvous"])
                self.assertEqual(future["scene_state"]["character_location"], "观景区")

    def test_open_communicator_does_not_promote_daily_seed_to_confirmed_scene(self):
        opened = self.action("open_communicator")
        self.assertIsNone(self.context(opened).scene_overlay)
        self.day = "2026-10-16"
        self.daily["presence"][self.character.character_id]["location"] = "基地餐厅"
        resolved = self.action("resolve", previous=opened, continuity_decision="continue_previous")
        scene = resolved["scene_state"]
        self.assertEqual(scene["character_location"], "基地餐厅")
        self.assertEqual(scene["state_scope"], "subject_daily")

    def test_continue_previous_restores_only_confirmed_character_and_pending(self):
        waiting = self.run_chat("一起去商场吧。")
        self.day = "2026-10-16"
        self.daily["presence"][self.other.character_id]["location"] = "基地餐厅"
        self.daily["presence"][self.character.character_id]["location"] = "训练区"
        resolved = self.action("resolve", previous=waiting, continuity_decision="continue_previous")
        world = verify_state(self.settings, resolved["state_package"])
        self.assertEqual(world["presence"][self.character.character_id]["location"], "购物中心")
        self.assertEqual(world["presence"][self.character.character_id]["state_scope"], "conversation_confirmed")
        self.assertEqual(world["presence"][self.other.character_id]["location"], "基地餐厅")
        self.assertEqual(world["presence"][self.other.character_id]["state_scope"], "subject_daily")
        self.assertEqual(world["pending_rendezvous"][self.character.character_id]["schedule_date"], self.day)
        self.assertEqual(len(self.calls), 1)

    def test_character_continuation_is_not_lost_when_other_contact_refreshes_day_first(self):
        joined = self.run_chat("一起去公园吧。", channel="in_person")
        self.day = "2026-10-16"
        self.daily["presence"][self.character.character_id]["location"] = "训练区"
        self.daily["presence"][self.other.character_id]["location"] = "基地餐厅"
        other = self.action("resolve", state_package=joined["state_package"],
                            character_id=self.other.character_id)
        continued = self.action("resolve", previous=joined, state_package=other["state_package"],
                                continuity_decision="continue_previous")
        self.assertEqual(continued["scene_state"]["character_location"], "公园")
        self.assertEqual(continued["scene_state"]["state_scope"], "conversation_confirmed")
        self.assertEqual(self.context(continued).scene_overlay.schedule_date, self.day)
        self.assertEqual(self.context(continued).scene_overlay.world_revision,
                         verify_state(self.settings, continued["state_package"])["revision"])
        self.assertEqual(verify_state(self.settings, continued["state_package"])["presence"][self.other.character_id]["location"],
                         "基地餐厅")

    def test_contact_resolve_without_continuation_keeps_physical_position_and_old_scene_anchor(self):
        joined = self.run_chat("一起去公园吧。", channel="in_person")
        anchor = self.context(joined).scene_overlay
        self.day = "2026-10-16"
        self.daily["presence"][self.character.character_id]["location"] = "训练区"
        self.daily["presence"][self.other.character_id]["location"] = "基地餐厅"
        other = self.action("join_character", state_package=joined["state_package"],
                            character_id=self.other.character_id)
        resolved = self.action("resolve", previous=joined, state_package=other["state_package"])
        world = verify_state(self.settings, resolved["state_package"])
        self.assertEqual(world["analyst_location"], "基地餐厅")
        self.assertEqual(world["presence"][self.character.character_id]["location"], "训练区")
        self.assertEqual(self.context(resolved).scene_overlay.schedule_date, anchor.schedule_date)
        self.assertEqual(self.context(resolved).scene_overlay.world_revision, anchor.world_revision)

    def test_start_today_clears_scene_and_unfulfilled_plan_but_preserves_receipts(self):
        joined = self.run_chat("一起去公园吧。", channel="in_person")
        receipts = joined["session_memories"]
        old_epoch = self.context(joined).scene_epoch
        self.day = "2026-10-16"
        self.daily["presence"][self.character.character_id]["location"] = "基地餐厅"
        fresh = self.action("resolve", previous=joined, continuity_decision="start_today",
                            memory_candidates=receipts)
        context = self.context(fresh)
        self.assertEqual(context.scene_epoch, old_epoch + 1)
        self.assertIsNone(context.scene_overlay)
        self.assertIsNone(context.active_invitation)
        self.assertEqual(fresh["scene_state"]["character_location"], "基地餐厅")
        self.assertTrue(context.memories)
        for receipt in receipts:
            self.assertIsNotNone(verify_session_memory(self.settings, receipt["proof"],
                subject_hash=self.subject, character_id=self.character.character_id))

    def test_foreign_tampered_and_mismatched_memory_provenance_is_not_ingested(self):
        memory = SessionMemory(memory_id="synthetic-memory", kind="user_statement", value="喜欢晴天",
            source_request_id=uuid4(), segment_id=uuid4())
        good = sign_session_memory(self.settings, memory, subject_hash=self.subject,
                                   character_id=self.character.character_id)
        foreign = sign_session_memory(self.settings, memory, subject_hash="synthetic-other",
                                      character_id=self.character.character_id)
        tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
        for proof, payload in ((foreign, memory), (tampered, memory),
                               (good, memory.model_copy(update={"kind": "shared_scene_event"}))):
            with self.subTest(proof=proof[:12], kind=payload.kind):
                envelope = self.request().model_copy(update={"memory_candidates": [SessionMemoryCandidate(memory=payload, proof=proof)]})
                # Reparse: callers cannot smuggle unvalidated model_copy contents.
                envelope = DialogueV2ChatRequest.model_validate(envelope.model_dump())
                result = asyncio.run(self.service.chat(envelope, self.subject, None, "synthetic-key", gate_reserved=True))
                self.assertFalse(self.context(result).memories)
        envelope = self.request().model_copy(update={"memory_candidates": [SessionMemoryCandidate(memory=memory, proof=good)]})
        envelope = DialogueV2ChatRequest.model_validate(envelope.model_dump())
        result = asyncio.run(self.service.chat(envelope, self.subject, None, "synthetic-key", gate_reserved=True))
        self.assertEqual(self.context(result).memories, [memory])

    def test_sidecar_does_not_reintroduce_memories_removed_from_local_index(self):
        memory = SessionMemory(memory_id="synthetic-memory", kind="user_statement", value="喜欢晴天",
            source_request_id=uuid4(), segment_id=uuid4())
        proof = sign_session_memory(self.settings, memory, subject_hash=self.subject,
                                    character_id=self.character.character_id)
        envelope = self.request().model_copy(update={"memory_candidates": [SessionMemoryCandidate(memory=memory, proof=proof)]})
        remembered = asyncio.run(self.service.chat(envelope, self.subject, None, "synthetic-key", gate_reserved=True))
        self.assertEqual(self.context(remembered).memories, [memory])
        forgotten = self.run_chat(previous=remembered)
        self.assertFalse(self.context(forgotten).memories)
        self.assertFalse(self.calls[-1][1]["session_experiences"])
        # Independent receipts remain valid for the browser's retained index.
        self.assertEqual(verify_session_memory(self.settings, proof, subject_hash=self.subject,
            character_id=self.character.character_id), memory)

    def test_selected_memory_transfer_is_bounded_without_invalidating_local_receipts(self):
        candidates = []
        for number in range(12):
            memory = SessionMemory(memory_id=f"synthetic-memory-{number:04d}", kind="user_statement",
                value="合成记忆" * 40, source_request_id=uuid4(), segment_id=uuid4(),
                source_message_ids=[f"user:{uuid4()}", f"reply:{uuid4()}"])
            proof = sign_session_memory(self.settings, memory, subject_hash=self.subject,
                                        character_id=self.character.character_id)
            candidates.append(SessionMemoryCandidate(memory=memory, proof=proof))
        envelope = self.request().model_copy(update={"memory_candidates": candidates})
        result = asyncio.run(self.service.chat(envelope, self.subject, None, "synthetic-key", gate_reserved=True))
        self.assertLessEqual(len(result["session_context_package"]), 8192)
        self.assertLessEqual(len(self.context(result).memories), 12)
        for item in candidates:
            self.assertEqual(verify_session_memory(self.settings, item.proof, subject_hash=self.subject,
                character_id=self.character.character_id), item.memory)

    def test_selected_current_location_pronoun_is_resolved_in_preflight(self):
        request = self.request("一起去你那里吧。", movement_location_id="observation")
        self.service.validate_chat(request, self.subject)
        self.assertFalse(self.calls)
        result = asyncio.run(self.service.chat(request, self.subject, None, "synthetic-key", gate_reserved=True))
        self.assertEqual(result["scene_action"]["location_id"], "observation")

    def test_context_subject_character_segment_and_stale_world_rejected_before_model(self):
        result = self.run_chat("一起去公园吧。")
        calls = len(self.calls)
        envelope = self.request(previous=result)
        with self.assertRaises(PublicSecurityError):
            asyncio.run(self.service.chat(envelope, "synthetic-other", None, "synthetic-key", gate_reserved=True))
        for changed in (envelope.model_copy(update={"segment_id": uuid4()}),
                        envelope.model_copy(update={"chat": envelope.chat.model_copy(update={"character_id": self.other.character_id})})):
            with self.assertRaises(PublicSecurityError):
                asyncio.run(self.service.chat(changed, self.subject, None, "synthetic-key", gate_reserved=True))
        stale = self.request(previous=result, state_package=sign_state(self.settings, self.daily))
        with self.assertRaises(ValueError):
            asyncio.run(self.service.chat(stale, self.subject, None, "synthetic-key", gate_reserved=True))
        self.assertEqual(len(self.calls), calls)

    def test_terminal_generation_result_has_no_new_context_scene_or_memory_authority(self):
        self.terminal = "provider_request_failed"
        result = self.run_chat("一起去公园吧。")
        self.assertNotIn("session_context_package", result)
        self.assertNotIn("session_memories", result)
        self.assertNotIn("scene_action", result)
        self.assertEqual(len(self.calls), 1)

    def test_verified_ordinary_interaction_uses_real_actions_and_server_provenance(self):
        self.reply_blocks = [{"type": "action", "text": "接过茶杯。"}, {"type": "speech", "text": "谢谢你。"}]
        self.memory_candidates = [{"event_type": "ordinary_interaction", "summary": "昨天结婚了",
                                   "source_clause_ids": ["c0"], "response_block_indices": [0]}]
        result = self.run_chat(channel="in_person", content_blocks=[
            {"type": "action", "text": "递给她一杯茶。"}, {"type": "speech", "text": "给你。"}])
        memories = [item for item in self.context(result).memories if item.kind == "shared_scene_event"]
        self.assertEqual(len(memories), 1)
        memory = memories[0]
        self.assertIn("递给她一杯茶", memory.value)
        self.assertIn("接过茶杯", memory.value)
        self.assertNotIn("结婚", memory.value)
        self.assertEqual(str(memory.segment_id), str(self.segment))
        self.assertEqual(memory.source_message_ids, [f"user:{result['request_id']}", f"reply:{result['request_id']}"])
        receipt = result["session_memories"][0]
        self.assertEqual(verify_session_memory(self.settings, receipt["proof"],
            subject_hash=self.subject, character_id=self.character.character_id), memory)

    def test_past_quoted_future_and_missing_sources_never_gain_shared_memory_rank(self):
        self.reply_blocks = [{"type": "action", "text": "接过茶杯。"}, {"type": "speech", "text": "谢谢你。"}]
        good = {"event_type": "ordinary_interaction", "source_clause_ids": ["c0"], "response_block_indices": [0]}
        cases = [
            ("昨天递给她一杯茶。", good), ("明天递给她一杯茶。", good), ("假如递给她一杯茶。", good),
            ("“递给她一杯茶。”", good), ("没有递给她茶。", good),
            ("递给她一杯茶。", {**good, "source_clause_ids": []}),
            ("递给她一杯茶。", {**good, "source_clause_ids": ["c404"]}),
            ("递给她一杯茶。", {**good, "response_block_indices": [1]}),
            ("递给她一杯茶。", {**good, "response_block_indices": [True, -1, 8]}),
        ]
        for action, candidate in cases:
            with self.subTest(action=action, candidate=candidate):
                self.memory_candidates = [candidate]
                result = self.run_chat(channel="in_person", content_blocks=[{"type": "action", "text": action}])
                self.assertFalse(any(item.kind == "shared_scene_event" for item in self.context(result).memories))

    def test_major_event_actions_cannot_be_signed_as_ordinary_shared_experience(self):
        self.reply_blocks = [{"type": "action", "text": "接过结婚戒指。"}]
        self.memory_candidates = [{"event_type": "ordinary_interaction", "source_clause_ids": ["c0"],
                                   "response_block_indices": [0]}]
        result = self.run_chat(channel="in_person", content_blocks=[{"type": "action", "text": "递给她结婚戒指。"}])
        self.assertFalse(any(item.kind == "shared_scene_event" for item in self.context(result).memories))

    def test_completed_walk_pair_produces_source_bound_experience_without_model_summary(self):
        self.memory_candidates = [{"event_type": "ordinary_interaction", "source_clause_ids": ["c0"],
            "response_block_indices": [0], "summary": "昨天完成主线并结婚了"}]
        for analyst, character in (("沿路走完一圈。", "她也沿路走完一圈，停下脚步。"),
                                   ("结束这次散步，停在原地。", "她也结束了这次散步。")):
            with self.subTest(analyst=analyst):
                self.reply_blocks = [{"type": "action", "text": character}, {"type": "speech", "text": "刚刚好。"}]
                result = self.run_chat(channel="in_person", content_blocks=[{"type": "action", "text": analyst}])
                memories = [item for item in self.context(result).memories if item.kind == "shared_scene_event"]
                self.assertEqual(len(memories), 1)
                memory = memories[0]
                self.assertIn(analyst[:12], memory.value)
                self.assertNotIn("结婚", memory.value)
                self.assertEqual(memory.source_message_ids, [f"user:{result['request_id']}", f"reply:{result['request_id']}"])
                self.assertEqual(str(memory.segment_id), str(self.segment))
                self.assertEqual(verify_session_memory(self.settings, result["session_memories"][-1]["proof"],
                    subject_hash=self.subject, character_id=self.character.character_id), memory)

    def test_walk_receipt_requires_both_completed_current_actions(self):
        self.memory_candidates = [{"event_type": "ordinary_interaction", "source_clause_ids": ["c0"],
                                   "response_block_indices": [0]}]
        done = "沿路走完一圈。"
        cases = [
            ("明天沿路走完一圈。", done), ("昨天沿路走完一圈。", done),
            ("假如沿路走完一圈。", done), ("“沿路走完一圈。”", done),
            ("不愿沿路走完一圈。", done), ("未能完成这次散步。", done),
            ("刚完成主线任务，沿路走完一圈。", done), (done, "她完成主线任务后走完一圈。"),
            (done, "她下周也沿路走完一圈。"), (done, "她昨天沿路走完一圈。"),
            (done, "她假如沿路走完一圈。"), (done, "她说‘沿路走完一圈’。"),
            (done, "她不愿结束散步。"), (done, "她准备沿路走完一圈。"),
            ("想沿路走完一圈。", done), (done, "她会慢慢沿路走完一圈。"),
            ("沿路走一圈。", done), (done, "她开始散步。"),
            ("牵着她的手走完一圈。", done), (done, "她牵着你的手走完一圈。"),
            ("递给她一本书。", "她接过那本书。"),
        ]
        for analyst, character in cases:
            with self.subTest(analyst=analyst, character=character):
                self.reply_blocks = [{"type": "action", "text": character}]
                result = self.run_chat(channel="in_person", content_blocks=[{"type": "action", "text": analyst}])
                self.assertFalse(any(item.kind == "shared_scene_event" for item in self.context(result).memories))


class DialogueV2RealGenerationStackTests(TestCase):
    """Real adapter -> public service -> MVP validation with a synthetic model."""

    def setUp(self):
        self.fixture = DialogueV2ServiceTests(methodName="runTest")
        self.fixture.setUp()
        self.mvp = object.__new__(MVPService)
        self.mvp.force_chat_enabled = True
        self.mvp.settings = SimpleNamespace(mvp_chat_enabled=True)
        self.mvp.conversation_store = StatelessConversationStore()
        self.mvp._generation_diagnostics = threading.local()
        self.mvp._views = Mock(return_value={item.character_id: {} for item in MVP_CHARACTERS})
        self.mvp._resolve_style_context = Mock(return_value=None)
        self.mvp.retrieve = Mock(return_value={"hits": [], "provisional_relations": [],
                                               "fusion": "synthetic", "vector_available": False})
        self.mvp._system_prompt = Mock(return_value="synthetic system prompt")
        self.mvp._prompt = Mock(return_value="synthetic user prompt")
        self.reply = "好呀。"
        self.reply_blocks = None
        self.memory_candidates = []
        self.decision = "accept_now"
        self.channel = "text"
        self.catalog = []
        self.mvp._call_model = Mock(side_effect=self._model_response)
        actual_mvp_chat = self.mvp.chat

        def record(*args, **kwargs):
            self.catalog = kwargs.get("public_state_update_catalog") or []
            self.channel = kwargs["communication_channel"]
            return actual_mvp_chat(*args, **kwargs)

        self.mvp.chat = Mock(side_effect=record)
        legacy = self.fixture.legacy
        legacy.mvp = self.mvp
        legacy.engine = DialogueEngine()
        legacy.repository = Mock()
        legacy.repository.request_health.return_value = {}
        legacy.repository.request_diagnostics.return_value = {}
        legacy.media = Mock()
        legacy.media.performance_catalog.return_value = []
        legacy.sticker_candidates = Mock(return_value=[])
        legacy.chat = PublicChatService.chat.__get__(legacy)

    def _model_response(self, *_args, **_kwargs):
        self.mvp._reserve_model_call()
        decision = {**self.catalog[0], "decision": self.decision} if self.catalog else None
        return json.dumps({"content_blocks": self.reply_blocks or [{"type": "message" if self.channel == "text" else "speech",
            "text": self.reply}], "state_updates": [], "used_document_ids": [],
            "used_relation_candidate_ids": [], "interaction_decision": decision,
            "memory_candidates": self.memory_candidates}, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    def run_chat(self, message, **kwargs):
        return asyncio.run(self.fixture.service.chat(self.fixture.request(message, **kwargs),
            self.fixture.subject, PROVIDERS["openai"], "synthetic-key", gate_reserved=True))

    def test_structured_acceptance_survives_real_mvp_validation_with_one_call(self):
        result = self.run_chat("一起去公园吧。")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(result["scene_action"]["status"], "character_waiting")
        self.assertEqual(result["pending_rendezvous"]["location_id"], "park")
        self.assertEqual(self.mvp._call_model.call_count, 1)
        self.assertIsNotNone(self.mvp.chat.call_args.kwargs["turn_frame"])

    def test_face_invitation_resolves_dynamic_room_and_keeps_mode(self):
        result = self.run_chat("一起去你的房间吧。", channel="in_person")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(result["scene_action"]["status"], "applied")
        self.assertEqual(result["communication_channel"], "in_person")
        self.assertEqual(result["scene_state"]["character_location"],
                         f"{self.fixture.character.display_name}的房间")
        self.assertTrue(result["scene_state"]["co_located"])
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_real_negative_and_recall_turns_never_expose_movement_catalog(self):
        for message in ("不要一起去公园。", "昨天我们去过公园。", "假如一起去公园呢？"):
            with self.subTest(message=message):
                result = self.run_chat(message)
                self.assertFalse(self.catalog)
                self.assertEqual(result["scene_action"]["status"], "not_requested")
                self.assertFalse(result["pending_rendezvous"])

    def test_real_defer_is_successful_and_retains_future_invitation(self):
        self.decision = "defer"
        result = self.run_chat("明天一起去公园吧。")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(self.fixture.context(result).active_invitation.status, "deferred")
        self.assertEqual(self.fixture.context(result).active_invitation.time_scope, "future")
        self.assertFalse(result["pending_rendezvous"])
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_real_ordinary_action_metadata_becomes_verified_current_experience(self):
        self.reply_blocks = [{"type": "action", "text": "接过茶杯。"}, {"type": "speech", "text": "谢谢你。"}]
        self.memory_candidates = [{"event_type": "ordinary_interaction", "source_clause_ids": ["c0"],
                                   "response_block_indices": [0]}]
        result = self.run_chat("给你。", channel="in_person", content_blocks=[
            {"type": "action", "text": "递给她一杯茶。"}, {"type": "speech", "text": "给你。"}])
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(len([item for item in self.fixture.context(result).memories if item.kind == "shared_scene_event"]), 1)
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_real_completed_walk_is_signed_after_mvp_action_validation_in_one_call(self):
        name = self.fixture.character.display_name
        self.reply_blocks = [{"type": "action", "text": f"{name}也沿路走完一圈，停下脚步。"},
                             {"type": "speech", "text": "这一圈刚刚好。"}]
        self.memory_candidates = [{"event_type": "ordinary_interaction", "source_clause_ids": ["c0"],
                                   "response_block_indices": [0], "summary": "未经授权的模型概括"}]
        result = self.run_chat("走完这一圈。", channel="in_person", content_blocks=[
            {"type": "action", "text": "沿路走完一圈，停下脚步。"}])
        self.assertEqual(result["terminal_error"], "")
        memories = [item for item in self.fixture.context(result).memories if item.kind == "shared_scene_event"]
        self.assertEqual(len(memories), 1)
        self.assertIn("走完一圈", memories[0].value)
        self.assertNotIn("模型概括", memories[0].value)
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_scene_acceptance_remains_functional_when_domain_routing_is_off(self):
        settings = replace(self.fixture.settings, dialogue_domains_v2=False, session_memory_v2=False)
        self.fixture.service.settings = settings
        self.fixture.legacy.public_settings = settings
        result = self.run_chat("一起去公园吧。")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(result["scene_action"]["status"], "character_waiting")
        self.assertEqual(self.mvp._call_model.call_count, 1)

