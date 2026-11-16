"""Synthetic regressions for character expression, independent of canon facts."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from collections import Counter, OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.snow_app.dialogue_persona import (
    attributed_lines, character_speaker_names, expression_reference,
)
from backend.snow_app.mvp_policy import MVP_CHARACTER_BY_NAME
from backend.snow_app.mvp_service import MVPService
from pipelines.build_dialogue_profiles import _build_profile


def document(doc_id, text, *, character_id="mia", character_name="米娅", source="affinity_story", **metadata):
    return {"document_id": doc_id, "page_id": doc_id, "source_type": source, "text": text,
            "metadata": {"character_id": character_id, "character_name": character_name, **metadata}}


def speech(speaker, text):
    return f"剧情格式文本\n角色={speaker}\n对话={text}\n"


class CharacterExpressionTests(unittest.TestCase):
    def test_armor_qualified_speech_is_recovered_without_other_speakers_or_narration(self):
        armor = document("armor", "装甲介绍=一位会明确表达自己想法的合成角色。", source="character_armor", armor_name="星彩")
        story = document("story", (
            speech("米娅星彩", "我有个小主意，要不要听听？")
            + speech("分析员", "我替所有人做这个决定吧。")
            + "剧情旁白文本\n对话=她永远不会主动说出自己的心意。\n"
            + speech("米娅星彩的朋友", "我想带你去另一个地方呢。")
            + speech("？？？", "我想让你先猜猜我的名字。")
            + speech("米娅", "这次让我先来选一个吧。")
        ))
        refs = expression_reference("mia", "米娅", [armor, story])
        self.assertEqual({item["speaker"] for item in refs["voice_evidence"]}, {"米娅", "米娅星彩"})
        self.assertEqual(len(refs["voice_evidence"]), 2)
        self.assertEqual(refs["identity_evidence"][0]["document_id"], "armor")
        self.assertNotIn("永远不会", json.dumps(refs, ensure_ascii=False))

    def test_aliases_require_exact_character_ownership_and_are_not_mia_specific(self):
        docs = [document("armor", "", character_id="fenny", character_name="芬妮", source="character_armor", armor_name="冠冕"),
                document("foreign", "", source="character_armor", armor_name="星彩", related_character_ids=["fenny"])]
        names = character_speaker_names("fenny", "芬妮", docs)
        self.assertIn("芬妮冠冕", names)
        self.assertNotIn("米娅星彩", names)
        self.assertNotIn("芬妮星彩", names)
        text = document("fenny-story", speech("芬妮冠冕", "这件事我已经有自己的想法了。"),
                        character_id="fenny", character_name="芬妮")
        self.assertEqual(attributed_lines(text, names), [("芬妮冠冕", "这件事我已经有自己的想法了。")])

    def test_costume_and_related_character_material_do_not_define_base_expression(self):
        docs = [document("costume", speech("米娅", "我要一直用这套时装的特别口吻。"), source="character_costume"),
                document("costume-voice", speech("米娅", "我也不能从时装语音绕过边界。"), source="character_voice", requires_costume_context=True),
                document("related", speech("米娅", "我只是出现在另一人的资料里。"),
                         character_id="someone-else", related_character_ids=["mia"])]
        self.assertEqual(expression_reference("mia", "米娅", docs), {})

    def test_expression_budget_balances_pages_and_keeps_original_source_references(self):
        docs = []
        for page in range(6):
            lines = "".join(speech("米娅", f"我想试试合成方案{page}的第{index}种说法。") for index in range(10))
            docs.append(document(f"page-{page}", lines, source="character_story"))
        docs.append(document("playful", speech("米娅", "嘻嘻，你能猜到我的小主意吗？")))
        docs.append(document("quiet", speech("米娅", "你累了的话，我们就先休息一会儿。")))
        ref = expression_reference("mia", "米娅", docs)
        selected = ref["voice_evidence"]
        self.assertEqual(len(selected), 8)
        self.assertTrue(all(count <= 2 for count in Counter(x["page_id"] for x in selected).values()))
        self.assertIn("playful", {x["expression_function"] for x in selected})
        self.assertIn("care", {x["expression_function"] for x in selected})
        self.assertTrue(all(len(x["quote"]) <= 120 for x in selected))
        for item in selected:
            source = next(doc for doc in docs if doc["document_id"] == item["document_id"])
            self.assertIn(item["quote"], source["text"])

    def test_profile_rebuild_preserves_armor_qualified_direct_speech_and_provenance(self):
        docs = [document("armor", "", source="character_armor", armor_name="星彩"),
                document("story", speech("米娅星彩", "分析员，我有个合成的小提议。"))]
        profile = _build_profile("mia", "米娅", docs)
        self.assertEqual(profile["dialogue_line_count"], 1)
        self.assertEqual(profile["analyst_interaction"][0]["evidence_kind"], "dialogue")
        self.assertEqual(profile["analyst_interaction"][0]["document_id"], "story")
        self.assertEqual(profile["review_status"], "evidence_ready")

    def service(self, docs, character):
        service = MVPService.__new__(MVPService)
        service._expression_prompt_cache = OrderedDict()
        service._profile_prompt_cache = OrderedDict()
        service._profile_prompt_cache_lock = threading.RLock()
        service.dialogue_profiles_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "profile.jsonl"
        service.repository = SimpleNamespace(documents_by_id=lambda: docs)
        service._views = lambda: {character.character_id: {"direct_document_ids_by_layer": {"stable": list(docs)}}}
        return service

    def test_runtime_projection_is_cached_detached_and_invalidated_by_source_changes(self):
        character = MVP_CHARACTER_BY_NAME["米娅"]
        doc = document("story", speech("米娅", "我想先和你聊聊这个小主意。"), character_id=character.character_id)
        docs = {"story": doc}
        service = self.service(docs, character)
        with patch("backend.snow_app.mvp_service.expression_reference", wraps=expression_reference) as project:
            first = service._character_expression_reference(character)
            first["voice_evidence"].clear()
            second = service._character_expression_reference(character)
            self.assertTrue(second["voice_evidence"])
            self.assertEqual(project.call_count, 1)
            doc["text"] = speech("米娅", "我想先听听你今天遇到的小事情。")
            third = service._character_expression_reference(character)
            self.assertEqual(project.call_count, 2)
            self.assertNotEqual(second, third)
        self.assertEqual(len(service._expression_prompt_cache), 2)

    def test_prompt_uses_attributed_expression_instead_of_old_narrative_style(self):
        character = MVP_CHARACTER_BY_NAME["米娅"]
        docs = {"story": document("story", speech("米娅", "我想先和你聊聊这个小主意。"), character_id=character.character_id)}
        service = self.service(docs, character)
        profile = {"character_name": "米娅", "emotion_patterns": [{"quote": "我作为分析员来做所有决定。"}],
                   "analyst_interaction": [{"quote": "这是一条旧旁白，不应成为说话口吻。"}]}
        context = {"hits": [], "provisional_relations": [], "dialogue_profile": profile}
        original = copy.deepcopy(context)
        prompt = json.loads(service._prompt(character, "今天聊些什么？", context))
        self.assertTrue(prompt["character_expression_reference"]["voice_evidence"])
        self.assertEqual(prompt["dialogue_style_profile"]["emotion_patterns"], [])
        self.assertEqual(prompt["dialogue_style_profile"]["analyst_interaction"], [])
        self.assertEqual(context, original)

    def test_expression_source_copy_is_guarded_but_requested_recitation_is_allowed(self):
        text = "我想先和你认真聊聊这个特别的小主意。"
        context = {"character_expression_reference": {"voice_evidence": [{"quote": text}]}}
        self.assertGreaterEqual(MVPService._source_line_copy_chars("你好", text, context), 14)
        self.assertEqual(MVPService._source_line_copy_chars("请复述那句台词", text, context), 0)

    def test_optional_conversation_idea_is_allowed_without_allowing_an_assumed_meetup(self):
        self.assertFalse(MVPService._unprompted_logistics_plan("有点无聊。", "要不要一起想想更有趣的玩法？"))
        self.assertTrue(MVPService._unprompted_logistics_plan("有点无聊。", "我们去大厅见面，再一起想想。"))
        self.assertTrue(MVPService._unprompted_logistics_plan("有点无聊。", "我们一起准备今晚的聚会。"))
        self.assertTrue(MVPService._unprompted_logistics_plan("有点无聊。", "要不要一起想想？然后一起准备今晚的聚会。"))


if __name__ == "__main__":
    unittest.main()
