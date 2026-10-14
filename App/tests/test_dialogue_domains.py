"""Source-domain regressions use synthetic repositories and zero providers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from backend.snow_app.dialogue_turn import parse_turn_frame
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPService


def document(document_id, source_type, text):
    return {"document_id": document_id, "title": "synthetic-source", "text": text,
            "source_type": source_type, "metadata": {}}


def hit(item):
    return {"citation": {"document_id": item["document_id"], "source_type": item["source_type"],
                         "title": item["title"]}, "text": item["text"], "metadata": {}, "score": 1}


def retrieval_service(documents, character=None):
    character = character or MVP_CHARACTERS[0]
    service = object.__new__(MVPService)
    service.repository = Mock()
    service.repository.hybrid_search.return_value = ([], False, [hit(item) for item in documents])
    service.repository.documents.return_value = documents
    service._runtime_documents_by_id = Mock(return_value={item["document_id"]: item for item in documents})
    service._views = Mock(return_value={character.character_id: {}})
    service._resolve_style_context = Mock(return_value={})
    service._allowed_document = Mock(return_value=True)
    service._rerank_hits = Mock(side_effect=lambda rows, *_args: (rows, ("general",)))
    service._resolve_character_mentions = Mock(return_value=[])
    service._companion_social_evidence = Mock(return_value=([], {}))
    service._cross_character_main_story_hits = Mock(return_value=([], {}))
    service._interaction_hint = Mock(return_value={})
    service._dual_persona_context = Mock(return_value={})
    service._relationship_background = Mock(return_value={})
    service._dialogue_profiles = Mock(return_value={})
    service._latest_state_hits = Mock(return_value=[])
    return service


def world(character):
    return {"analyst_location": "观景区", "presence": {character.character_id: {
        "character_id": character.character_id, "location": "观景区", "activity": "看雪",
        "state_scope": "conversation_confirmed"}}}


class DialogueDomainRetrievalTests(TestCase):
    def setUp(self):
        self.character = MVP_CHARACTERS[0]
        self.story = document("doc_synthetic_story", "main_story", "原作当时在码头完成运输任务。")
        self.persona = document("doc_synthetic_persona", "character_voice", "我听着呢。")

    def retrieve(self, service, message, **kwargs):
        return service.retrieve(self.character.character_id, message,
            world_state=world(self.character), turn_frame=parse_turn_frame(message), **kwargs)

    def test_canon_location_is_not_captured_by_current_scene(self):
        service = retrieval_service([self.story, self.persona])
        result = self.retrieve(service, "原作当时你在哪里")
        self.assertIsNone(result["live_scene"])
        self.assertEqual(result["question_focus"], "narrative_recall")
        self.assertIn(self.story["document_id"], result["retrieval_pools"]["canon"])
        self.assertIn("不能由此刻场景替代", result["response_contract"])
        self.assertEqual(service.repository.hybrid_search.call_count, 1)

    def test_canon_activity_uses_events_and_mixed_keeps_both_pools(self):
        service = retrieval_service([self.story, self.persona])
        result = self.retrieve(service, "原作当时你做了什么？现在你在哪里？")
        self.assertEqual(result["conversation_mode"], "mixed_dialogue")
        self.assertEqual(result["question_focus"], "mixed_dialogue")
        self.assertEqual(result["live_scene"]["location"], "观景区")
        self.assertIn(self.story["document_id"], result["retrieval_pools"]["canon"])
        query = service.repository.hybrid_search.call_args.args[0]
        self.assertIn("原作当时", query)
        self.assertNotIn("现在", query)
        self.assertEqual(MVPService._answer_guardrail_violations(
            "原作当时你做了什么？现在你在哪里？", "当时在码头。现在我在观景区。",
            {**result, "communication_context": {"channel": "text"}}, "immersive",
            [{"type": "message", "text": "当时在码头。现在我在观景区。"}]), [])
        self.assertEqual(MVPService._unprompted_scene_disclosures(
            "原作当时你做了什么？现在你在哪里？", "当时在码头。现在我在观景区。",
            {**result, "raw_scene_state": {"character_location": "观景区", "analyst_location": "观景区", "character_activity": "看雪"}}), [])

    def test_original_quote_supplies_retrieval_event_and_person_but_not_current_scene(self):
        event = document("doc_synthetic_quoted_event", "main_story", "原作中芙提雅在公园接到消息。")
        service = retrieval_service([event, self.persona])
        queries = []

        def hybrid(query, character_id, limit):
            queries.append(query)
            return [], False, [hit(event)] if "公园" in query and "芙提雅" in query else []

        service.repository.hybrid_search.side_effect = hybrid
        message = '原作里，她说“现在去公园找芙提雅”，当时发生什么？现在一起去商场吧。'
        result = self.retrieve(service, message)
        self.assertEqual(len(queries), 1)
        self.assertIn("公园", queries[0])
        self.assertIn("芙提雅", queries[0])
        self.assertNotIn("商场", queries[0])
        self.assertIn(event["document_id"], result["retrieval_pools"]["canon"])
        self.assertIsNone(result["live_scene"])
        self.assertEqual(result["conversation_mode"], "mixed_dialogue")

    def test_casual_without_stable_match_never_backfills_story(self):
        service = retrieval_service([self.story])
        result = self.retrieve(service, "陪我聊聊天好吗")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["retrieval_pools"]["canon"], [])
        service._cross_character_main_story_hits.assert_not_called()

    def test_current_location_does_not_delete_stable_persona(self):
        service = retrieval_service([self.story, self.persona])
        result = self.retrieve(service, "现在你在哪里")
        self.assertEqual(result["live_scene"]["location"], "观景区")
        self.assertEqual([item["citation"]["document_id"] for item in result["hits"]], [self.persona["document_id"]])
        self.assertNotIn(self.story["document_id"], result["retrieval_pools"]["canon"])

    def test_session_recall_does_not_query_canon_as_shared_history(self):
        service = retrieval_service([self.story, self.persona])
        confirmed = {"experience_id": "memory-synthetic", "summary": "一起在商场喝茶。",
            "source_message_ids": ["user:synthetic", "reply:synthetic"], "provenance": "shared_scene_event",
            "domain": "session", "status": "confirmed"}
        reported = {**confirmed, "experience_id": "reported-synthetic", "summary": "分析员说昨天去过码头。", "status": "reported"}
        invalid = {**confirmed, "experience_id": "unproven-synthetic", "source_message_ids": []}
        result = self.retrieve(service, "我们上次去哪里", session_experiences=[confirmed, reported, invalid])
        self.assertEqual(result["question_focus"], "session_recall")
        self.assertIsNone(result["live_scene"])
        self.assertEqual(result["retrieval_pools"]["canon"], [])
        self.assertEqual(result["session_experiences"], [confirmed, reported])
        self.assertEqual(result["retrieval_diagnostics"]["session_confirmed_count"], 1)

    def test_legacy_none_keeps_old_scene_routing(self):
        service = retrieval_service([self.story, self.persona])
        result = service.retrieve(self.character.character_id, "原作当时你在哪里", world_state=world(self.character))
        self.assertEqual(result["question_focus"], "location")
        self.assertEqual(result["live_scene"]["location"], "观景区")
        self.assertEqual(result["hits"], [])
        self.assertNotIn("turn_frame", result)

    def test_all_twenty_two_personas_keep_identity_in_every_domain(self):
        for character in MVP_CHARACTERS:
            for message in ("陪我聊聊", "原作当时你在哪里", "我们上次去哪里"):
                with self.subTest(character=character.character_id, message=message):
                    service = retrieval_service([self.story, self.persona], character)
                    result = service.retrieve(character.character_id, message,
                        world_state=world(character), turn_frame=parse_turn_frame(message))
                    self.assertEqual(result["character"], character)
                    self.assertEqual(service.repository.hybrid_search.call_args.args[1], character.character_id)


class DialogueSourceAndDecisionTests(TestCase):
    def test_history_summary_and_reported_claims_are_not_quote_evidence(self):
        context = {"turn_frame": parse_turn_frame("我们上次做了什么").as_dict(), "hits": [],
            "session_context": {"summary": "一起吃过火锅", "turns": [{"user": "合成用户历史", "assistant": "合成模型历史"}]},
            "session_experiences": [
                {"summary": "已确认一起喝茶", "status": "confirmed"},
                {"summary": "未确认一起吃火锅", "status": "reported"}]}
        evidence = MVPService._guardrail_evidence_texts("当前问题", context)
        self.assertIn("已确认一起喝茶", evidence)
        self.assertNotIn("合成模型历史", evidence)
        self.assertNotIn("合成用户历史", evidence)
        self.assertNotIn("未确认一起吃火锅", evidence)

    def test_recall_question_does_not_confirm_its_own_meeting_premise(self):
        message = "我们之前约好见面了吗"
        context = {"turn_frame": parse_turn_frame(message).as_dict(), "session_experiences": [],
                   "session_context": {"turns": [{"user": "我们约好一起见面", "assistant": "是的"}]}}
        self.assertTrue(MVPService._unsupported_session_meeting_premises(message, "我们之前约好见面了。", context))
        context["session_experiences"] = [{"status": "confirmed", "summary": "我们之前约好见面。"}]
        self.assertEqual(MVPService._unsupported_session_meeting_premises(message, "我们之前约好见面了。", context), [])

    def test_no_signed_experience_means_no_affirmative_shared_past(self):
        message = "我们上次去了哪里"
        context = {"turn_frame": parse_turn_frame(message).as_dict(), "question_focus": "session_recall",
            "session_experiences": [{"status": "reported", "summary": "我们上次在商场喝茶。"}]}
        self.assertTrue(MVPService._unsupported_session_meeting_premises(message, "上次我们在商场喝茶。", context))
        self.assertEqual(MVPService._unsupported_session_meeting_premises(message, "你说上次我们在商场喝茶，但我记不清细节。", context), [])
        self.assertEqual(MVPService._unsupported_session_meeting_premises(message, "你是指我们上次去了哪里？", context), [])

    def test_v2_does_not_require_waiting_catchphrase_but_text_physics_still_apply(self):
        context = {"turn_frame": parse_turn_frame("一起去商场吧").as_dict(),
            "public_state_update_catalog": [{"movement_mode": "rendezvous"}],
            "communication_context": {"channel": "text"}}
        self.assertEqual(MVPService._text_rendezvous_semantics_violations("好呀，那里很适合聊聊。", context), [])
        violations = MVPService._answer_guardrail_violations("一起去商场吧", "她握住你的手。", context,
            "immersive", [{"type": "action", "text": "她握住你的手。"}])
        self.assertTrue(any("text_channel" in item or "communication_block" in item for item in violations))
        self.assertEqual(MVPService._text_rendezvous_semantics_violations("好呀，我们一起去。",
            {"public_state_update_catalog": [{"movement_mode": "rendezvous"}]}), ["text_rendezvous_semantics"])

    def test_structured_decisions_remain_candidates_with_strict_enums(self):
        value = {"decision": "accept_now", "location_id": "shopping_mall", "activity_id": "shopping", "trusted": True}
        self.assertEqual(MVPService._interaction_decision_candidate(value),
                         {key: value[key] for key in ("decision", "location_id", "activity_id")})
        for invalid in ({**value, "decision": "teleport"}, {**value, "location_id": "https://invalid"}, {"accepted": True}):
            self.assertIsNone(MVPService._interaction_decision_candidate(invalid))

    def test_memory_candidates_reference_only_current_clauses_and_final_action_blocks(self):
        frame = parse_turn_frame("端起茶杯喝一口。昨天我们去商场。")
        blocks = [{"type": "action", "text": "她也喝了一口茶。"}, {"type": "speech", "text": "好呀。"}]
        good = {"event_type": "ordinary_interaction", "source_clause_ids": ["c0"], "response_block_indices": [0]}
        bad = [
            {**good, "source_clause_ids": ["c1"]}, {**good, "response_block_indices": [1]},
            {**good, "response_block_indices": [True]}, {**good, "response_block_indices": [9]},
            {**good, "event_type": "canon_fact"}, {**good, "source_clause_ids": ["external"]},
        ]
        self.assertEqual(MVPService._memory_event_candidates([good, {**good, "summary": "untrusted", "status": "confirmed"}], frame, blocks), [good, good])
        for item in bad:
            with self.subTest(item=item):
                self.assertEqual(MVPService._memory_event_candidates([item], frame, blocks), [])

    def test_v2_prompt_is_compact_with_metadata_before_last_content_blocks(self):
        service = object.__new__(MVPService)
        prompt = service._system_prompt(MVP_CHARACTERS[0], None, communication_channel="text", dialogue_v2=True)
        self.assertNotIn('"answer":"中文回答"', prompt)
        self.assertIn('"interaction_decision":null,"memory_candidates":[]', prompt)
        self.assertIn("content_blocks 必须是对象最后一个字段", prompt)

