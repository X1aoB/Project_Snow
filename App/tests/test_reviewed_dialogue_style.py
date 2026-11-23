"""Synthetic fixtures for the opt-in character-style experiments."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.snow_app.config import Settings
from backend.snow_app.dialogue_style import (
    LEGACY, StyleSettings, cue_hint, minimal_repair, retrieved_examples, reviewed_rows, signature_violations,
    validate_style_inputs,
)
from backend.snow_app.mvp_service import MVPService, _FENNY_HARSH_DAILY_MARKERS
from backend.snow_app.repository import RuntimeRepository
from pipelines.style_corpus import build_index


def test_frequency_window_counts_turns_and_exempts_user_and_common_repeats():
    config = replace(LEGACY, signature_guard="data_driven", window=3)
    row = {"term": "亲亲", "category": "address", "rate": .1, "repeat_rate": 0}
    history = [{"assistant": "亲亲"}, {"assistant": "来吧"}, {"assistant": "一起玩"}]
    assert signature_violations("好", "亲亲", history, [row], config) == []
    assert signature_violations("好", "亲亲", history[:2], [row], config) == ["signature_overuse:亲亲"]
    assert signature_violations("好", "亲亲亲亲", [], [row], config)
    assert not signature_violations("亲亲", "亲亲亲亲", history, [row], config)
    assert not signature_violations("好", "亲亲亲亲", [], [{**row, "repeat_rate": .2}], config)
    assert not signature_violations("好", "亲亲亲亲", [], [{**row, "category": "topic"}], config)
    assert not signature_violations("好", "亲亲", history[:1], [row], replace(config, window=1))


def test_only_reviewed_terms_are_used(tmp_path):
    path = tmp_path / "signature_terms.jsonl"
    rows = [{"character_id": "甲", "term": "用语", "review_status": status}
            for status in ("pending", "approved", "rejected", "legacy")]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    assert reviewed_rows(tmp_path, "signature_terms", "甲") == rows[1:2]
    assert reviewed_rows(tmp_path, "signature_terms", "乙") == []


@pytest.mark.parametrize("term", ["本天师", "卦象", "算卦", "运势"])
def test_bubu_legacy_group_vs_reviewed_individual_frequency(term):
    context = {"character": SimpleNamespace(character_id="6455a5dcff6a"),
               "session_context": {"turns": [{"assistant": "运势" if term != "运势" else "卦象"}]}}
    assert MVPService._signature_overuse_violations("聊天", term, context) == ["character_signature_overuse"]
    context.update(style_settings=replace(LEGACY, signature_guard="data_driven"),
                   signature_terms=[{"term": term, "category": "catchphrase", "rate": .05}])
    assert MVPService._signature_overuse_violations("聊天", term, context) == []


@pytest.mark.parametrize("term", _FENNY_HARSH_DAILY_MARKERS)
def test_register_only_enforces_reviewed_daily_exclusions(term):
    context = {"character": SimpleNamespace(character_id="1b0a6b35719a")}
    assert MVPService._fenny_voice_violations("你好", term, context)
    context.update(style_settings=replace(LEGACY, register_guard="data_driven"), register_terms=[])
    assert not MVPService._fenny_voice_violations("你好", term, context)
    context["register_terms"] = [{"term": term}]
    assert MVPService._fenny_voice_violations("你好", term, context) == ["register_mismatch:" + term]
    assert not MVPService._fenny_voice_violations("有危险", term, context)
    context["live_scene"] = {"status": "active", "activity": "紧急撤退"}
    assert not MVPService._fenny_voice_violations("你好", term, context)


def test_shared_facts_match_both_people_and_topic_in_the_same_excerpt():
    context = {"style_settings": replace(LEGACY, shared_facts="data_driven"),
               "cross_character_story_context": {"active": True, "speaking_character": "小甲",
                   "mentioned_characters": [{"canonical_name": "小乙"}], "topic_terms": ["研发"],
                   "evidence": [{"source_type": "main_story", "document_id": "d", "title": "相见",
                                 "excerpt": "角色=小甲\n对话=记得那次研发吗？\n角色=小乙\n对话=记得。"}]}}
    assert MVPService._cross_character_fact_violations("我不知道。", context) == ["cross_character_fact_denial"]
    kept = MVPService._style_repair(["cross_character_fact_denial"], {}, "不知道！", context, "text")
    assert "相见" in kept["answer"] and kept["used_document_ids"] == ["d"]
    for excerpt in ("角色=小甲\n对话=小乙负责研发。", "小乙负责研发。", "小甲和小乙一起看书。",
                    "标题=共同研发\n角色=小甲\n对话=你好。\n角色=小乙\n对话=好啊。"):
        context["cross_character_story_context"]["evidence"][0]["excerpt"] = excerpt
        assert not MVPService._cross_character_fact_violations("不知道。", context)


def test_only_required_cue_sets_opening_and_checks_source():
    row = {"document_id": "d", "page_id": "p", "hint": "自然回应", "triggers": ["玩笑"],
           "required_opening": "等等！", "co_speaker": "乙"}
    docs = {"d": {"page_id": "p", "text": "角色=甲\n对话=真的吗？\n角色=乙\n对话=当然。"}}
    optional = cue_hint([row], "开个玩笑", docs)
    assert optional and "required_opening" not in optional
    assert not MVPService._interaction_hint_violations("你好。", {"interaction_hint": optional})
    required = cue_hint([{**row, "strength": "required"}], "开个玩笑", docs)
    assert MVPService._interaction_hint_violations("你好。", {"interaction_hint": required})
    assert not cue_hint([row], "开个玩笑", {"d": {"page_id": "other", "text": "乙"}})


def test_fts_diversity_determinism_and_copy_threshold(tmp_path):
    base = {"spoken": True, "analyst": "晚饭吃什么", "character_id": "甲", "source_type": "affinity_story",
            "split": "train", "reply": "一起准备一桌热乎乎的晚饭吧。"}
    pairs = [{**base, "page_id": str(i), "document_id": str(i), **delta}
             for i, delta in enumerate([{}, {"reply": "先一起去挑些新鲜蔬菜，再研究一下怎么做晚饭吧。"},
                                        {"split": "holdout", "reply": "留出原句"}, {"character_id": "乙"}])]
    build_index(tmp_path / "examples.sqlite3", pairs)
    examples = retrieved_examples(tmp_path, "甲", "晚饭", 4, 80)
    assert examples == retrieved_examples(tmp_path, "甲", "晚饭", 4, 80)
    assert {item["page_id"] for item in examples} == {"0", "1"}
    answer = pairs[1]["reply"]
    context = {"character_expression_reference": {"voice_evidence": examples}}
    assert MVPService._source_line_copy_chars("晚饭", answer, context) >= 14
    assert "source_line_copy" not in MVPService._answer_guardrail_violations("晚饭", answer, context, "immersive")
    context["style_settings"] = replace(LEGACY, copy="rewrite")
    assert "source_line_copy" in MVPService._answer_guardrail_violations("晚饭", answer, context, "immersive")


def test_minimal_repair_templates_keep_only_supported_preferences_and_address():
    assert minimal_repair("闭嘴！今天我们一起看书吧。", ["闭嘴"]) == "今天我们一起看书吧。"
    assert minimal_repair("闭嘴！好。", ["闭嘴"]) == ""
    assert minimal_repair("闭嘴，！？", ["闭嘴"]) == ""
    row = {"statement": "我偏好休息。", "evidence_document_ids": ["d"]}
    context = {"style_settings": replace(LEGACY, fallback="minimal_repair"),
               "user_message": "训练还是休息？", "relationship_address_memory": {"preferred_address": "队长"},
               "dialogue_profile": {"self_reference_terms": [{"term": "本人", "evidence_document_ids": ["d"]}],
                                    "supported_preferences": [row]}}
    assert MVPService._routine_activity_fallback(context) == "队长，没有紧急安排时，本人想先休息。"
    row["evidence_document_ids"] = []
    assert "本人会先去训练" in MVPService._routine_activity_fallback(context)


def test_chat_applies_prompt_guard_and_local_fallback_independently(tmp_path, monkeypatch):
    char = "6455a5dcff6a"
    monkeypatch.setenv("PROJECT_SNOW_STYLE_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_SNOW_STYLE_SIGNATURE_GUARD", "data_driven")
    monkeypatch.setenv("PROJECT_SNOW_STYLE_SIGNATURE_PROMPT", "off")
    monkeypatch.setenv("PROJECT_SNOW_STYLE_EXAMPLES", "retrieved")
    monkeypatch.setenv("MVP_CHAT_DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    row = {"term": "夏花", "category": "catchphrase", "rate": .06, "character_id": char, "review_status": "approved"}
    (tmp_path / "signature_terms.jsonl").write_text(json.dumps(row), encoding="utf-8")
    build_index(tmp_path / "examples.sqlite3", [{"character_id": char, "page_id": "page", "document_id": "doc",
        "source_type": "affinity_story", "analyst": "说说看书", "reply": "下次想去看看那家书店。", "split": "train", "spoken": True}])
    (tmp_path / "mvp").mkdir()
    (tmp_path / "mvp" / "character_views.jsonl").write_text(json.dumps({"character_id": char}), encoding="utf-8")
    settings = Settings(data_root=tmp_path, runtime_root=tmp_path, chat_enabled=True,
                        embedding_model="", allowed_origins=[])
    service = MVPService(settings, RuntimeRepository(settings))
    answer = "夏花夏花！今天我想读一本有趣的书。"
    body = json.dumps({"answer": answer, "content_blocks": [{"type": "message", "text": answer}]})
    try:
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(body, {})
        ) as call:
            result = service.chat(char, "说说看书。", session_id="style-test", mode="immersive", communication_channel="text")
        assert call.call_count == 2
        system, user = call.call_args_list[0].args[:2]
        assert "卜卜的标志性表达频率" not in system
        assert json.loads(user)["character_expression_reference"]["voice_evidence"][0]["page_id"] == "page"
        assert result["answer"] == "今天我想读一本有趣的书。"
        assert "signature_frequency_guard" in result["response_adjustments"]
        assert "style_example_token_delta_estimate" in service.generation_diagnostics()
    finally:
        service.close()


def test_disabled_rules_never_appear_on_unrelated_rewrite():
    cfg = replace(LEGACY, signature_guard="off", register_guard="off", situational_cues="off")
    rewrite = MVPService._guarded_rewrite_prompt("{}", "合成回复", ["mechanical_dialogue"], cfg)
    assert all(term not in rewrite for term in ("算卦", "芬妮", "干什么！"))
    legacy = MVPService._guarded_rewrite_prompt("{}", "合成回复", ["mechanical_dialogue"])
    assert "算卦" in legacy and "芬妮" in legacy


def test_source_pack_absence_fails_at_startup_only_for_enabled_modes(tmp_path):
    validate_style_inputs(tmp_path, LEGACY)
    with pytest.raises(FileNotFoundError):
        validate_style_inputs(tmp_path, replace(LEGACY, signature_prompt="data_driven"))
    # A present table with no approvals is a valid experimental condition.
    table = tmp_path / "signature_terms.jsonl"
    table.write_text('{"character_id":"demo","term":"demo","review_status":"pending"}\n', encoding="utf8")
    cfg = replace(LEGACY, signature_guard="data_driven", signature_prompt="data_driven")
    validate_style_inputs(tmp_path, cfg)
    rows = reviewed_rows(tmp_path, "signature_terms", "demo")
    assert rows == [] and signature_violations("你好", "demodemo", [], rows, cfg) == []


def test_minimal_repair_does_not_restore_violating_role_term():
    context = {"style_settings": replace(LEGACY, signature_guard="data_driven"),
               "dialogue_profile": {"self_reference_terms": [{"term": "本天师", "evidence_document_ids": ["d"]}]}}
    fixed = MVPService._style_repair(["signature_overuse:本天师"], {}, "本天师！", context, "text")
    assert fixed["answer"] and "本天师" not in fixed["answer"]


def test_legacy_cue_requires_cited_utterance_and_attributed_companion():
    row = {"character_id": "a2ffc5b44d7f", "page_id": "p", "document_id": "d", "review_status": "legacy",
           "required_opening": "干什么！", "strength": "required", "triggers": ["身材"], "co_speaker": "妮塔",
           "hint": "回应当下的话。"}
    doc = {"page_id": "p", "text": "角色=芙提雅\n对话=妮塔你……干什么！"}
    assert not cue_hint([row], "身材", {"d": doc})
    doc["text"] += "\n角色=妮塔\n对话=我只是问问。"
    assert cue_hint([row], "身材", {"d": doc})["kind"] == "source_bound_body_teasing"


def test_flag_validation(monkeypatch):
    monkeypatch.setenv("PROJECT_SNOW_STYLE_WINDOW", "8")
    with pytest.raises(ValueError):
        StyleSettings.from_environment()

