"""Synthetic channel ownership regressions; no runtime, database or HTTP calls."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from backend.snow_app.dialogue_core import DialogueEngine, WorldState
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPCommunicationConflict, MVPService
from backend.snow_app.public_contracts import ChatRequest, StatePayload
from backend.snow_app.public_providers import PROVIDERS
from backend.snow_app.public_security import verify_state
from backend.snow_app.public_service import PublicChatService, StatelessConversationStore

from tests.test_public_api import _settings


class PublicMeetingChannelTests(TestCase):
    def setUp(self) -> None:
        self.character = MVP_CHARACTERS[0]
        self.settings = _settings()
        self.state = StatePayload(
            data_version=self.settings.data_version,
            revision=2,
            analyst_location="基地休息区",
            presence={self.character.character_id: {
                "character_id": self.character.character_id,
                "character_name": self.character.display_name,
                "location": "观景区",
                "activity": "正在休息",
                "state_scope": "conversation_confirmed",
            }},
            subject_binding="synthetic-subject",
        ).model_dump()
        # Avoid constructors that read environment/runtime or create fact stores.
        self.mvp = object.__new__(MVPService)
        self.mvp.force_chat_enabled = True
        self.mvp.settings = SimpleNamespace(mvp_chat_enabled=True)
        self.mvp.conversation_store = StatelessConversationStore()
        self.mvp._generation_diagnostics = threading.local()
        self.mvp._views = Mock(return_value={self.character.character_id: {}})
        self.mvp._resolve_style_context = Mock(return_value=None)
        self.mvp.retrieve = Mock(side_effect=lambda *_args, **_kwargs: {
            "hits": [], "provisional_relations": [], "fusion": "synthetic",
            "vector_available": False,
        })
        self.mvp._system_prompt = Mock(return_value="synthetic system prompt")
        self.mvp._prompt = Mock(return_value="synthetic user prompt")
        self.reply_channel = "text"
        self.reply_text = "好呀，我们可以继续聊。"
        self.mvp._call_model = Mock(side_effect=self._model_response)
        self.mvp_results = []
        actual_chat = self.mvp.chat

        def record_chat(*args, **kwargs):
            result = actual_chat(*args, **kwargs)
            self.mvp_results.append(result)
            return result

        self.mvp.chat = Mock(side_effect=record_chat)
        self.service = object.__new__(PublicChatService)
        self.service.public_settings = self.settings
        self.service.mvp = self.mvp
        self.service.engine = DialogueEngine()
        self.service.repository = Mock()
        self.service.repository.request_health.return_value = {}
        self.service.repository.request_diagnostics.return_value = {}
        self.service.media = Mock()
        self.service.media.performance_catalog.return_value = []
        self.service.sticker_candidates = Mock(return_value=[])
        self.service._normalized_state = Mock(side_effect=lambda *_args: json.loads(json.dumps(self.state)))

    def _model_response(self, *_args, **_kwargs):
        self.mvp._reserve_model_call()
        block_type = "message" if self.reply_channel == "text" else "speech"
        text = self.reply_text
        return json.dumps({
            "answer": text, "content_blocks": [{"type": block_type, "text": text}],
            "state_updates": [], "used_document_ids": [],
            "used_relation_candidate_ids": [],
        }, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    def _request(self, message: str, channel: str = "text") -> ChatRequest:
        self.reply_channel = channel
        return ChatRequest(
            request_id=uuid4(), provider="openai", credential="synthetic-credential-value",
            model="synthetic", character_id=self.character.character_id,
            message=message, communication_channel=channel,
            content_blocks=[{"type": "message" if channel == "text" else "speech", "text": message}],
        )

    def _chat(self, message: str, channel: str = "text"):
        return self.service._chat_sync(
            self._request(message, channel), "synthetic-subject", PROVIDERS["openai"], "synthetic-key",
        )

    def test_text_meeting_request_generates_in_text_while_apart(self) -> None:
        result = self._chat("我们见面聊吧。")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(result["communication_channel"], "text")
        self.assertEqual(result["content_blocks"][0]["type"], "message")
        self.assertFalse(result["scene_state"]["co_located"])
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_same_location_text_meeting_does_not_silently_switch_channel(self) -> None:
        self.state["analyst_location"] = "观景区"
        result = self._chat("我们当面聊吧。")
        self.assertEqual(result["communication_channel"], "text")
        self.assertEqual(self.mvp_results[-1]["channel_transition"]["status"], "none")
        self.assertEqual(self.mvp_results[-1]["channel_transition"]["to"], "text")
        decoded = verify_state(self.settings, result["state_package"])
        self.assertEqual(decoded["recent_events"][-1]["communication_channel"], "text")

    def test_text_meeting_destination_preserves_rendezvous_waiting(self) -> None:
        self.reply_text = "好呀，我先去商场等你。"
        result = self._chat("要不要一起去商场见面聊？")
        self.assertEqual(result["terminal_error"], "")
        self.assertEqual(result["communication_channel"], "text")
        self.assertEqual(result["movement_status"]["status"], "character_waiting")
        self.assertEqual(result["pending_rendezvous"]["location_id"], "shopping_mall")
        decoded = verify_state(self.settings, result["state_package"])
        self.assertEqual(decoded["analyst_location"], "基地休息区")
        self.assertEqual(decoded["pending_rendezvous"][self.character.character_id]["location_id"], "shopping_mall")
        self.assertEqual(decoded["recent_events"][-1]["event_type"], "rendezvous_waiting")
        self.assertEqual(self.mvp._call_model.call_count, 1)

    def test_explicit_in_person_remains_in_person_despite_dialogue_switch_request(self) -> None:
        self.state["analyst_location"] = "观景区"
        result = self._chat("改用通讯器聊吧，现在感觉如何？", "in_person")
        self.assertEqual(result["communication_channel"], "in_person")
        self.assertEqual(result["content_blocks"][0]["type"], "speech")
        self.assertEqual(self.mvp_results[-1]["channel_transition"]["status"], "none")
        self.assertEqual(self.mvp_results[-1]["channel_transition"]["to"], "in_person")

    def test_real_in_person_location_conflict_still_blocks_before_provider(self) -> None:
        with self.assertRaises(MVPCommunicationConflict):
            self._chat("我们继续聊。", "in_person")
        self.mvp.retrieve.assert_not_called()
        self.mvp._call_model.assert_not_called()
        self.assertEqual(self.mvp.generation_diagnostics()["provider_http_calls"], 0)

    def test_local_default_still_requires_presence_for_text_meeting(self) -> None:
        self.mvp.reset_generation_diagnostics()
        session_id, world_id = "synthetic-local-session", "synthetic-local-world"
        with self.mvp.isolated_state(session_id, world_id, {
            "character_id": self.character.character_id,
            "communication_channel": "text", "mode": "immersive",
            "turns": [], "mode_turns": {"immersive": [], "assistant": []},
        }, WorldState({**self.state, "world_session_id": world_id, "created_at": "synthetic"})):
            with self.assertRaises(MVPCommunicationConflict) as error:
                self.mvp.chat(
                    self.character.character_id, "我们见面聊吧。", session_id=session_id,
                    world_session_id=world_id, communication_channel="text",
                    persist_exchange=False, remember_session=False,
                )
        self.assertEqual(error.exception.detail["channel_transition"]["status"], "requires_presence_choice")
        self.mvp.retrieve.assert_not_called()
        self.mvp._call_model.assert_not_called()
        self.assertEqual(self.mvp.generation_diagnostics()["provider_http_calls"], 0)
