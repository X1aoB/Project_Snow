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

    def test_in_person_actions_use_third_person_and_leave_speech_separate(self) -> None:
        service = self._service()
        blocks = service._normalize_content_blocks(
            {
                "content_blocks": [
                    {
                        "type": "speech",
                        "text": "（（我歪了歪头，眼睛亮晶晶地看着分析员。））\n分析员～你来啦！",
                    }
                ]
            },
            "in_person",
            "",
            "伊切尔",
        )

        self.assertEqual(
            blocks,
            [
                {"type": "action", "text": "伊切尔歪了歪头，眼睛亮晶晶地看着分析员。"},
                {"type": "speech", "text": "分析员～你来啦！"},
            ],
        )
        self.assertFalse(blocks[0]["text"].startswith(("我", "她")))

    def test_shared_meal_keeps_food_supplied_in_the_current_turn(self) -> None:
        service = self._service()
        message = "我拿了些西餐过来，今天先吃点工作餐，下次再带你出去吃好吗？"
        self.assertEqual(service._question_focus(message), "shared_meal")
        bad = "我还没决定吃什么。你有想吃的，就告诉我吧。"
        payload = self._model_payload(bad, [{"type": "speech", "text": bad}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "673ba6851b05",
                message,
                session_id="feedback-regression-shared-meal",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertIn("工作餐", result["answer"])
        self.assertNotIn("还没决定吃什么", result["answer"])
        self.assertIn("shared_meal_guard", result["response_adjustments"])

    def test_routine_choice_rejects_rest_training_contradiction(self) -> None:
        service = self._service()
        message = "早上在干什么呢，训练还是休息？"
        bad = "早上我更想赖床，不过既然你问了，那大概是在训练场活动筋骨吧。"
        payload = self._model_payload(bad, [{"type": "speech", "text": bad}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "25b23cb64398",
                message,
                session_id="feedback-regression-routine-logic",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertIn("没有任务", result["answer"])
        self.assertIn("训练安排", result["answer"])
        self.assertNotIn("大概是在训练场", result["answer"])
        self.assertIn("routine_activity_guard", result["response_adjustments"])

    def test_bubu_signature_trait_is_rate_limited_but_explicit_request_is_allowed(self) -> None:
        service = self._service()
        service._remember_session(
            "feedback-regression-bubu-frequency",
            "6455a5dcff6a",
            "下午好",
            "下午好呀，要不要卜卜给你算一卦，看看今天的运势？",
            mode="immersive",
            communication_channel="text",
        )
        repeated = "火锅好呀！本天师再给你算一卦，看看哪种锅底运势最旺。"
        payload = self._model_payload(repeated, [{"type": "message", "text": repeated}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "6455a5dcff6a",
                "吃火锅怎么样？",
                session_id="feedback-regression-bubu-frequency",
                mode="immersive",
                communication_channel="text",
            )

        self.assertNotIn("算卦", result["answer"])
        self.assertNotIn("运势", result["answer"])
        self.assertIn("signature_frequency_guard", result["response_adjustments"])
        self.assertEqual(
            service._signature_overuse_violations(
                "那就算一卦",
                repeated,
                {
                    "character": service.character("6455a5dcff6a"),
                    "session_context": {"turns": [{"assistant": "刚才算过一卦。"}]},
                },
            ),
            [],
        )

    def test_recently_disclosed_location_is_not_repeated_on_visit_followup(self) -> None:
        service = self._service()
        session_id = "feedback-regression-visit-followup"
        service._remember_session(
            session_id,
            "6455a5dcff6a",
            "你现在在哪呢？",
            "我在资料室呢，正翻着手边的资料。",
            mode="immersive",
            communication_channel="text",
        )
        repeated = "我在资料室呢，你要过来的话，卜卜就在这里等你。"
        payload = self._model_payload(repeated, [{"type": "message", "text": repeated}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "6455a5dcff6a",
                "那我去找你？",
                session_id=session_id,
                mode="immersive",
                communication_channel="text",
            )

        self.assertNotIn("资料室", result["answer"])
        self.assertIn("我等你", result["answer"])
        self.assertIn("visit_location_guard", result["response_adjustments"])

    def test_jointly_confirmed_room_overrides_neutral_training_scene(self) -> None:
        service = self._service()
        session_id = "feedback-regression-confirmed-room"
        service._remember_session(
            session_id,
            "daab0f4cceb4",
            "你在房间吗，我想你了",
            "我在呢，亲爱的。我也想你。",
            mode="immersive",
            communication_channel="text",
        )
        anchor = service._recent_confirmed_location(
            service._session_snapshot(session_id, "daab0f4cceb4", "immersive"),
            "daab0f4cceb4",
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["location"], "个人房间")

    def test_open_invitation_continues_intimacy_without_activity_reset(self) -> None:
        service = self._service()
        message = "不如，你亲口告诉我，你想让我做什么呢"
        self.assertEqual(service._question_focus(message), "open_invitation")
        bad = "我刚结束一轮基础训练。现在可以陪你聊会儿。"
        payload = self._model_payload(bad, [{"type": "speech", "text": bad}])
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "daab0f4cceb4",
                message,
                session_id="feedback-regression-open-invitation",
                mode="immersive",
                communication_channel="in_person",
            )

        self.assertNotIn("训练", result["answer"])
        self.assertIn("心意", result["answer"])
        self.assertIn("open_invitation_guard", result["response_adjustments"])

    def test_in_person_accepts_action_and_speech_in_the_same_turn(self) -> None:
        blocks = MVPService._normalize_analyst_content_blocks(
            "抬手理好她的发梢\n今天辛苦了。",
            [
                {"type": "action", "text": "抬手理好她的发梢"},
                {"type": "speech", "text": "今天辛苦了。"},
            ],
            "in_person",
        )
        self.assertEqual(
            blocks,
            [
                {"type": "action", "text": "抬手理好她的发梢"},
                {"type": "speech", "text": "今天辛苦了。"},
            ],
        )

    def test_feedback_families_distinguish_fixed_regressions(self) -> None:
        service = self._service()
        cases = {
            "signature_trait_repetition": {
                "category": "conversation_experience",
                "free_text": "一直在提算卦，角色特点不至于反复提及，请减少频次",
                "message_excerpt": "吃火锅怎么样",
            },
            "location_repetition": {
                "category": "conversation_experience",
                "free_text": "刚才已经提到了地点，不需要再次揭露",
                "message_excerpt": "那我去找你？",
            },
            "current_activity_choice": {
                "category": "conversation_experience",
                "free_text": "这句话回答逻辑有问题",
                "message_excerpt": "早上训练还是休息？",
            },
            "composer_action_and_speech": {
                "category": "client_function",
                "free_text": "动作和对白应该支持同时输入，文字通讯时隐藏动作按钮",
            },
            "intimacy_continuity": {
                "category": "knowledge_memory",
                "free_text": "暧昧亲密剧情碰到审核后强行重置和中断，希望保持隐晦连续",
            },
        }
        for expected, row in cases.items():
            self.assertEqual(service._feedback_issue_key(row), expected)


if __name__ == "__main__":
    unittest.main()
