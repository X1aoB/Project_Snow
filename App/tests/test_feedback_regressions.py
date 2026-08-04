"""Executable regression expectations derived from recent user feedback.

These tests deliberately keep provider calls mocked.  They describe the
observable contract the dialogue layer must preserve without turning a model
quality concern into a network-dependent test.

These specifications remain ordinary passing regression tests. A failure
means a fixed feedback family has reappeared and must be triaged as a genuine
regression rather than silently marked resolved.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.snow_app.config import Settings
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.repository import RuntimeRepository


class FeedbackRegressionTests(unittest.TestCase):
    """Focused coverage for the latest dialogue-quality feedback batch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._store_temp = tempfile.TemporaryDirectory()
        cls._store_environment = patch.dict(
            os.environ,
            {
                "MVP_CHAT_DATABASE_PATH": str(
                    Path(cls._store_temp.name) / "feedback-regressions.sqlite3"
                )
            },
        )
        cls._store_environment.start()
        cls.settings = Settings.from_environment()
        cls.repository = RuntimeRepository(cls.settings)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._store_environment.stop()
        cls._store_temp.cleanup()

    @staticmethod
    def _model_payload(
        answer: str,
        blocks: list[dict[str, str]],
    ) -> str:
        return json.dumps(
            {
                "answer": answer,
                "content_blocks": blocks,
                "confidence": "medium",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )

    def _service(self) -> MVPService:
        return MVPService(self.settings, self.repository)

    def test_immersive_in_person_preserves_legal_character_action_blocks(self) -> None:
        """A valid face-to-face action must survive normalisation and storage."""

        service = self._service()
        payload = self._model_payload(
            "（凯西娅稍稍抬起眼。）\n我在听。",
            [
                {"type": "action", "text": "凯西娅稍稍抬起眼。"},
                {"type": "speech", "text": "我在听。"},
            ],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "25b23cb64398",
                "凯西娅，陪我聊聊。",
                session_id="feedback-regression-action-block",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertEqual(result["communication_channel"], "in_person")
        self.assertEqual(
            result["content_blocks"],
            [
                {"type": "action", "text": "凯西娅稍稍抬起眼。"},
                {"type": "speech", "text": "我在听。"},
            ],
        )
        self.assertIn("（凯西娅稍稍抬起眼。）", result["answer"])

    def test_rendezvous_intent_exposes_character_location_but_greeting_does_not(self) -> None:
        """“去找你” is an intentional location request, unlike a greeting."""

        raw_scene = {
            "analyst_location": "基地大厅",
            "character_location": "医疗室",
            "character_activity": "整理装备",
            "co_located": False,
            "state_scope": "session_simulation",
        }

        greeting_context = MVPService._scene_state_for_prompt(
            raw_scene,
            "早安，凯西娅。",
            "general",
        )
        rendezvous_context = MVPService._scene_state_for_prompt(
            raw_scene,
            "那我去找你？",
            "general",
        )

        self.assertEqual(greeting_context["location_visibility"], "hidden_unless_asked")
        self.assertNotIn("character_location", greeting_context)
        self.assertEqual(rendezvous_context["location_visibility"], "visible_for_current_turn")
        self.assertEqual(rendezvous_context["character_location"], "医疗室")

    def test_visit_request_repairs_a_reply_that_omits_its_location(self) -> None:
        """An explicit “go find you” turn must state where the character is."""

        service = self._service()
        payload = self._model_payload(
            "好啊，我就在这里等你。",
            [{"type": "message", "text": "好啊，我就在这里等你。"}],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "98322bd505f4",
                "那我去找你？",
                session_id="feedback-regression-visit-location",
                world_session_id="feedback-regression-visit-world",
                mode="immersive",
                communication_channel="text",
            )

        location = result["scene_state"]["character_location"]
        self.assertTrue(location)
        self.assertIn(location, result["answer"])
        self.assertIn("visit_location_guard", result["response_adjustments"])

    def test_earlier_morning_question_rejects_a_current_activity_answer(self) -> None:
        """“早上” must not be silently answered with the live “刚才/现在” scene."""

        service = self._service()
        message = "早上在干什么呢，训练还是休息？"

        payload = self._model_payload(
            "我刚才在训练。",
            [{"type": "speech", "text": "我刚才在训练。"}],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "25b23cb64398",
                message,
                session_id="feedback-regression-morning-scope",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertNotIn("刚才", result["answer"])
        self.assertNotIn("现在", result["answer"])

    def test_morning_choice_rejects_unsupported_sleepy_lore(self) -> None:
        """A choice of training or rest cannot be evaded with invented history."""

        service = self._service()
        message = "早上在干什么呢，训练还是休息？"
        payload = self._model_payload(
            "我啊，早上大概还在赖床吧。恒约之后好像越来越嗜睡了。",
            [{"type": "speech", "text": "我啊，早上大概还在赖床吧。恒约之后好像越来越嗜睡了。"}],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "25b23cb64398",
                message,
                session_id="feedback-regression-morning-choice",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertTrue(any(term in result["answer"] for term in ("训练", "休息", "任务")))
        self.assertNotIn("恒约之后", result["answer"])
        self.assertIn("routine_activity_guard", result["response_adjustments"])

    def test_intimate_invitation_keeps_a_warm_non_lore_extension(self) -> None:
        """A natural invite can be warm and continue the topic without a plot recap."""

        service = self._service()
        expected = "好啊。和你一起出去走走，我很愿意。你想从哪里开始？"
        payload = self._model_payload(expected, [{"type": "speech", "text": expected}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "98322bd505f4",
                "今晚一起散散步，好吗？",
                session_id="feedback-regression-warm-invitation",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertIn(expected, result["answer"])
        self.assertEqual(result["content_blocks"][0]["type"], "action")
        self.assertEqual(result["content_blocks"][1]["text"], expected)
        self.assertNotIn("剧情", result["answer"])
        self.assertGreaterEqual(len(result["answer"]), 18)

    def test_intimate_invitation_rejects_an_unasked_plot_recap(self) -> None:
        """A model must not answer a simple invitation by forcing in chapter lore."""

        service = self._service()
        forced_recap = "好啊。想起主线第十八章那次任务后，我一直想和你散步。"
        payload = self._model_payload(
            forced_recap,
            [{"type": "speech", "text": forced_recap}],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "98322bd505f4",
                "今晚一起散散步，好吗？",
                session_id="feedback-regression-no-forced-lore",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertNotIn("主线第十八章", result["answer"])
        self.assertNotIn("那次任务", result["answer"])
        self.assertGreaterEqual(len(result["answer"]), 18)

    def test_analyst_action_blocks_are_persisted_and_text_rejects_them(self) -> None:
        """Only an explicit in-person analyst action may enter chat history."""

        service = self._service()
        analyst_blocks = [{"type": "action", "text": "我把一杯热饮递给你。"}]
        payload = self._model_payload(
            "谢谢，我收下了。",
            [{"type": "speech", "text": "谢谢，我收下了。"}],
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "25b23cb64398",
                "我把一杯热饮递给你。",
                session_id="feedback-regression-analyst-action",
                mode="immersive",
                communication_channel="in_person",
                analyst_content_blocks=analyst_blocks,
            )

        self.assertEqual(result["analyst_content_blocks"], analyst_blocks)
        history = service.conversation_history(
            "25b23cb64398", session_id="feedback-regression-analyst-action"
        )
        self.assertEqual(history["messages"][0]["content_blocks"], analyst_blocks)
        with self.assertRaisesRegex(ValueError, "文字通讯不能提交"):
            service._normalize_analyst_content_blocks(
                "我想抱抱你。",
                [{"type": "action", "text": "我抱住你。"}],
                "text",
            )

    def test_in_person_fallback_actions_are_named_and_not_repeated(self) -> None:
        """Fallback presence beats must not degrade into one fixed “她…” line."""

        service = self._service()
        first_payload = self._model_payload("我在听。", [{"type": "speech", "text": "我在听。"}])
        second_payload = self._model_payload("我明白。", [{"type": "speech", "text": "我明白。"}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(first_payload, {}), (second_payload, {})]
        ):
            first = service.chat(
                "25b23cb64398",
                "凯西娅，陪我聊聊。",
                session_id="feedback-regression-varied-actions",
                mode="immersive",
                communication_channel="in_person",
            )
            second = service.chat(
                "25b23cb64398",
                "刚才那句话，你是怎么想的？",
                session_id="feedback-regression-varied-actions",
                mode="immersive",
                communication_channel="in_person",
            )

        first_action = first["content_blocks"][0]["text"]
        second_action = second["content_blocks"][0]["text"]
        self.assertEqual(first["content_blocks"][0]["type"], "action")
        self.assertEqual(second["content_blocks"][0]["type"], "action")
        self.assertIn("凯西娅", first_action)
        self.assertIn("凯西娅", second_action)
        self.assertNotEqual(first_action, second_action)


if __name__ == "__main__":
    unittest.main()
