"""Character facts are tested with synthetic evidence and no provider calls."""
from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import Mock

import pytest

from backend.snow_app.config import Settings
from backend.snow_app.generation_stream import GenerationObserver, observe_generation
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_knowledge import PublicKnowledge
from backend.snow_app.repository import RuntimeRepository
from tests.test_dialogue_domains import document, hit, retrieval_service


MIA = next(item for item in MVP_CHARACTERS if item.display_name == "米娅")
KNOWLEDGE = PublicKnowledge()


def source(text, *, character=MIA, title="合成事实材料", owner=None):
    return {"document_id": "synthetic_fact", "source_type": "character_story", "title": title,
            "text": text, "metadata": {"character_id": owner or character.character_id,
                "related_character_ids": [character.character_id], "source_priority": 1.0}}


class RelationshipFactTests(TestCase):
    def test_negated_hypothetical_quoted_and_other_person_are_not_relationship_evidence(self):
        for text in ("我们还没有立下恒约。", "如果我们结婚，我会很开心。", "我希望将来成为你的妻子。",
                     "我们已经结婚了吗？", "芬妮和分析员已经结婚。", "她念出‘台词’，又读出“我们已经结婚了”。"):
            with self.subTest(text=text):
                card = MVPService._relationship_background([source(text)], MIA.character_id)
                self.assertEqual(card["status"], "unknown")
                self.assertEqual(card["evidence"], [])

    def test_relationship_title_is_not_body_evidence(self):
        card = MVPService._relationship_background([source("这是普通的一天。", title="恒约与婚礼")], MIA.character_id)
        self.assertEqual(card["status"], "unknown")

    def test_normalized_record_headers_and_foreign_speaker_do_not_become_facts(self):
        header = "story_title：心心年年的恒约\nspeaker_names：芬妮、米娅\n剧情格式文本\n"
        for content in (header + "角色=芬妮\n对话=我爱你，分析员。",
                        "story_title：心心年年的恒约\n正文=这是普通的一天。"):
            card = MVPService._relationship_background([source(content)], MIA.character_id)
            self.assertEqual(card["status"], "unknown")
            self.assertEqual(card["evidence"], [])
        content = header + "角色=芬妮\n对话=我是你的妻子。\n角色=米娅\n对话=我爱你，分析员。"
        card = MVPService._relationship_background([source(content)], MIA.character_id)
        self.assertEqual(card["status"], "supported")
        excerpt = card["evidence"][0]["excerpt"]
        self.assertIn("我爱你", excerpt)
        for excluded in ("恒约", "妻子", "芬妮", "story_title"):
            self.assertNotIn(excluded, excerpt)

    def test_related_character_does_not_own_another_speakers_relationship(self):
        fenny = next(item for item in MVP_CHARACTERS if item.display_name == "芬妮")
        card = MVPService._relationship_background([source("我是你的妻子。", owner=fenny.character_id)], MIA.character_id)
        self.assertEqual(card["status"], "unknown")

    def test_affection_remains_supported_but_does_not_establish_marriage(self):
        card = MVPService._relationship_background([source("分析员，我爱你。")], MIA.character_id)
        self.assertEqual(card["status"], "supported")
        self.assertNotIn("伴侣", card["relationship_label"])

    def test_reviewed_formal_roster_remains_authoritative_without_search_hit(self):
        for character_id in KNOWLEDGE.formal_character_ids():
            self.assertEqual(MVPService._relationship_background([], character_id)["status"], "explicit")

    def test_every_unconfirmed_character_rejects_asserted_self_marriage_in_casual_chat(self):
        for character in MVP_CHARACTERS:
            if character.character_id in KNOWLEDGE.formal_character_ids():
                continue
            for answer in ("我们已经立下恒约了。", "我是你的妻子。", "自从恒约之后，我更喜欢和你聊天。",
                           "不是开玩笑，但我们已经结婚了。", "我们已经结婚了，以后每天都一起吃饭。",
                           "我不是在开玩笑，我们早已缔结恒约。", "我们已经恒约了，不是吗？",
                           "自从我们恒约以后，我一直很开心。", "我们已经成亲了。",
                           "你是我的丈夫。", "我们早就结过婚了。"):
                with self.subTest(character=character.display_name, answer=answer):
                    context = {"character": character, "question_focus": "general", "hits": []}
                    violations = MVPService._answer_guardrail_violations("陪我聊聊", answer, context, "immersive")
                    self.assertIn("unsupported_formal_relationship", violations)
                    self.assertTrue(MVPService._hard_guard_violation("unsupported_formal_relationship"))

    def test_denial_wishes_affection_and_other_characters_facts_are_allowed(self):
        context = {"character": MIA, "hits": []}
        for answer in ("我们还没有恒约哦。", "我们还未结婚。", "如果有一天，我们结婚了，我会很高兴。",
                       "我想和你结婚。", "我爱你，今天一起喝茶吧。", "芬妮已经和你立下恒约。",
                       "我是芬妮的朋友，她已经和分析员恒约。",
                       "我帮你筹备婚礼。", "我参加过你的婚礼。", "我听说过恒约。",
                       "你可以叫我老婆呀。", "老婆这个称呼我还不习惯。", "我们谈论恒约。",
                       "你问的是“我们已经结婚了”这句话吗？"):
            with self.subTest(answer=answer):
                self.assertNotIn("unsupported_formal_relationship",
                    MVPService._answer_guardrail_violations("还没恒约呢", answer, context, "immersive"))

    def test_known_relationship_is_not_downgraded(self):
        for character in MVP_CHARACTERS:
            if character.character_id not in KNOWLEDGE.formal_character_ids():
                continue
            self.assertNotIn("unsupported_formal_relationship", MVPService._answer_guardrail_violations(
                "陪我聊聊", "我们已经立下恒约了。", {"character": character, "hits": []}, "immersive"))

    def test_roster_rejects_every_unapproved_character_not_only_enya(self):
        answer = MVPService._relationship_roster_fallback()
        for character in MVP_CHARACTERS:
            if character.character_id in KNOWLEDGE.formal_character_ids():
                continue
            with self.subTest(character=character.display_name):
                self.assertIn(f"relationship_roster_excluded:{character.display_name}",
                    MVPService._relationship_roster_violations("恒约都有谁？", answer + f"还有{character.display_name}。"))

    def test_roster_does_not_treat_unrelated_names_or_denials_as_list_members(self):
        answer = MVPService._relationship_roster_fallback()
        for suffix in ("米娅喜欢开玩笑。", "米娅还在休息。", "米娅还没有恒约。", "恩雅听说过恒约。",
                       "我也是你的妻子。", "米娅在休息。我也是你的妻子。"):
            self.assertEqual(MVPService._relationship_roster_violations("恒约都有谁？", answer + suffix), [])

    def test_every_turn_has_fact_boundary_separate_from_relationship_recall(self):
        service = object.__new__(MVPService)
        prompt = service._system_prompt(MIA, None)
        self.assertIn("尚未确立恒约", prompt)
        self.assertIn("亲密表达不等于正式关系事实", prompt)

    def test_negative_or_question_premises_are_not_saved_as_affirmed_marriage(self):
        for message in ("不是说你是我的老婆。", "你是我的妻子吗？", "假如我们已经结婚，你会开心吗？",
                        "不要说我们是夫妻。", "你误以为我们已经恒约。", "如果我们是夫妻呢？"):
            self.assertIsNone(MVPService._user_relationship_premise(message))
        self.assertEqual(MVPService._user_relationship_premise("我们已经结婚了。"), "我们已经结婚了。")

    def test_old_false_premise_cannot_create_address_memory(self):
        character = next(item for item in MVP_CHARACTERS if item.display_name == "里芙")
        for premise in ("不要说我们是夫妻。", "如果我们已经结婚了。", "你误以为我们定下了恒约。"):
            self.assertEqual(MVPService._relationship_address_memory({"character": character,
                "relationship_background": {}, "session_context": {"premises": [premise]}}), {})

    def test_false_fact_cannot_publish_as_a_verified_stream_block(self):
        service = object.__new__(MVPService)
        observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
        with observe_generation(observer):
            service._verify_stream_blocks([{"type": "message", "text": "我们已经恒约了。"}],
                message="陪我聊聊", context={"character": MIA, "hits": [], "question_focus": "general"},
                channel="text", character_name=MIA.display_name)
        self.assertEqual(observer.published_blocks, [])


class RetrievalCandidateTests(TestCase):
    def test_full_filtered_pool_reaches_reranker_before_final_limit(self):
        documents = [document(f"synthetic_{i}", "main_story", f"合成原作剧情片段 {i}") for i in range(10)]
        service = retrieval_service(documents, MIA)
        observed = []
        def rerank(rows, *args):
            observed.extend(item["citation"]["document_id"] for item in rows)
            return [rows[-1]], ("experience",)
        service._rerank_hits = Mock(side_effect=rerank)
        result = service.retrieve(MIA.character_id, "原作发生什么事情", limit=2)
        self.assertEqual(len(observed), len(documents))
        self.assertEqual(result["hits"][0]["citation"]["document_id"], documents[-1]["document_id"])

    def test_reranker_rechecks_view_before_accepting_external_hits(self):
        service = object.__new__(MVPService)
        own = source("分析员，我爱你。")
        forbidden = source("我们已经结婚。", title="未准入材料")
        forbidden["document_id"] = "forbidden"
        service._runtime_documents_by_id = Mock(return_value={own["document_id"]: own, "forbidden": forbidden})
        results, _ = service._rerank_hits([hit(forbidden)], MIA.character_id,
            {"retrieval_document_ids": [own["document_id"]]}, None, "我们是什么关系？", 8)
        self.assertNotIn("forbidden", [row["citation"]["document_id"] for row in results])


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_real_retrieval_and_generation_correct_or_reject_false_covenant(tmp_path, monkeypatch, repair_succeeds):
    """Exercise real retrieval, prompting, guarded rewrite and final disposition."""
    monkeypatch.setattr("backend.snow_app.mvp_service._load_local_environment", lambda: None)
    settings = Settings(data_root=tmp_path, runtime_root=tmp_path, chat_enabled=True,
        embedding_model="fixture", allowed_origins=[], mvp_chat_enabled=True)
    document = source("分析员，我爱你。")
    service = MVPService(settings, RuntimeRepository(settings), force_chat_enabled=True,
        conversation_database_path=tmp_path / "synthetic.sqlite3")
    monkeypatch.setattr(service, "_views", lambda: {MIA.character_id: {
        "character_id": MIA.character_id, "retrieval_document_ids": [document["document_id"]]}})
    monkeypatch.setattr(service, "_runtime_documents_by_id", lambda: {document["document_id"]: document})
    monkeypatch.setattr(service.repository, "documents", lambda: [document])
    monkeypatch.setattr(service.repository, "hybrid_search", lambda *_args: ("lexical_only", False, [hit(document)]))
    monkeypatch.setattr(service.repository, "serving_graph_context", lambda *_args: {"status": "not_requested", "nodes": [], "edges": []})
    false = "我们已经立下恒约了。"
    corrected = "对呀，我们还没有恒约。但我很愿意陪你聊聊。"
    calls = []
    def model(system_prompt, user_prompt, **_kwargs):
        calls.append((system_prompt, user_prompt))
        answer = corrected if repair_succeeds and len(calls) > 1 else false
        return json.dumps({"content_blocks": [{"type": "message", "text": answer}]}, ensure_ascii=False), {}
    monkeypatch.setattr(service, "_call_model", model)
    try:
        result = service.chat(MIA.character_id, "我们还没恒约哦，陪我聊聊。", communication_channel="text",
            model_settings=("https://example.invalid/v1", "fixture", "fixture"),
            persist_exchange=False, remember_session=False)
    finally:
        service.close()
    assert len(calls) == 2
    assert "尚未确立恒约" in calls[0][0]
    assert "unsupported_formal_relationship" in calls[1][1]
    if repair_succeeds:
        assert result["answer"] == corrected
        assert result["validation_disposition"] == "normalized"
    else:
        assert result["validation_disposition"] == "rejected"
        assert result["guard_code"] == "unsupported_formal_relationship"
