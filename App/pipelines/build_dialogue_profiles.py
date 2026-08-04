"""Build evidence-backed, latest-state dialogue profiles.

The profile is a deterministic projection of the existing lakehouse.  It is
not a new source of canon and it does not activate ``persona_profiles`` traits
or modify the graph.  Every behavioural observation keeps the document ID and
the original quote that supports it.  This makes the artifact useful to the
dialogue prompt while keeping unsupported model guesses out of the character
persona.

The project intentionally uses one current profile per character.  Older
events are retained as dated/temporal evidence in ``narrative_evolution``;
they are never exposed as a separate personality snapshot.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .common import RUNTIME_ROOT, dialogue_characters, ensure_runtime, load_runtime_jsonl, utc_now, write_json
from .common import write_jsonl


SOURCE_PRIORITY_ORDER = (
    "character_voice",
    "character_profile",
    "character_story",
    "affinity_story",
    "special_mail",
    "random_event",
    "furniture_lore",
    "character_affection",
    "main_story",
)

STYLE_SOURCE_TYPES = {"character_voice", "character_profile", "character_story", "affinity_story", "special_mail", "random_event"}
PREFERENCE_SOURCE_TYPES = STYLE_SOURCE_TYPES | {"furniture_lore", "character_affection", "item_lore"}
EMOTION_SOURCE_TYPES = STYLE_SOURCE_TYPES | {"furniture_lore", "character_affection", "main_story"}
EVOLUTION_SOURCE_TYPES = EMOTION_SOURCE_TYPES | {"event_lore"}

# These are deliberately lexical cues, not a personality classifier.  A claim
# is kept as a quote and is labelled observed/supported; no new fact is written
# from the cue alone.
PREFERENCE_CUES: tuple[tuple[str, str], ...] = (
    ("不喜欢", "dislike"),
    ("不爱", "dislike"),
    ("讨厌", "dislike"),
    ("厌恶", "dislike"),
    ("不愿意", "dislike"),
    ("拒绝", "boundary"),
    ("喜欢", "preference"),
    ("爱好", "preference"),
    ("偏爱", "preference"),
    ("爱吃", "preference"),
    ("想吃", "preference"),
    ("在意", "value"),
    ("重视", "value"),
    ("关心", "value"),
    ("珍惜", "value"),
    ("希望", "value"),
    ("习惯", "habit"),
    ("害怕", "fear"),
    ("担心", "value"),
)

EMOTION_CUES: dict[str, tuple[str, ...]] = {
    "care_or_concern": ("担心", "放心不下", "心疼", "关心", "照顾", "在意"),
    "trust_or_reliance": ("相信", "信任", "依靠", "交给你", "拜托你"),
    "affection": ("喜欢你", "爱你", "深爱", "亲爱的", "想你", "心意"),
    "self_blame_or_regret": ("自责", "后悔", "对不起", "抱歉", "弥补", "失误"),
    "anger_or_rejection": ("生气", "愤怒", "讨厌", "滚开", "别靠近", "拒绝"),
    "relief_or_joy": ("开心", "高兴", "幸福", "安心", "放心", "笑"),
    "fear_or_vulnerability": ("害怕", "恐惧", "不安", "孤独", "寂寞", "痛苦"),
}

TEMPORAL_MARKERS: dict[str, tuple[str, ...]] = {
    "past": ("过去", "曾经", "以前", "那时", "当时", "往日", "从前", "记得"),
    # “已经/后来/终于” occur in ordinary scene narration very frequently;
    # they are intentionally not sufficient to assert a current-state change.
    "current": ("现在", "如今", "此刻", "目前", "如今的我"),
    "transition": (
        "不再",
        "变得",
        "逐渐",
        "从此",
        "重新",
        "复原",
        "治愈",
        "成长",
        "学会",
        "改变",
        "转变",
    ),
}

ADDRESS_TERMS = ("分析员", "指挥官", "队长", "长官", "先生", "小姐", "老师", "博士", "主人", "大人")
SELF_REFERENCE_TERMS = ("本小姐", "本大爷", "本喵", "咱", "吾", "在下", "俺", "老娘", "我")
STYLE_PARTICLES = ("喵", "啦", "哼", "呢", "吧", "哦", "呀", "呐", "诶", "欸", "嘛", "哎", "唉")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\n|;；")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]


def _field_value(line: str, field: str) -> str:
    match = re.match(rf"^{re.escape(field)}\s*[=:：]\s*(.*)$", line)
    return _clean(match.group(1)) if match else ""


def _sentences(text: str) -> list[str]:
    # Keeping the source punctuation makes an evidence quote auditable and
    # avoids turning one long wikitext block into an invented paraphrase.
    parts = re.split(r"(?<=[。！？!?；;])|\n+", str(text or ""))
    result: list[str] = []
    for part in parts:
        value = _clean(part)
        if 8 <= len(value) <= 360 and not value.startswith(("资料类型", "story_", "source_")):
            result.append(value)
    return result


# Narrative fields are copied from the crawler's structured preamble or from
# the corresponding wikitext field.  Everything else containing ``=`` is
# usually a navigation/mechanics/template field and must not become a
# preference or emotion quote by accident.
NARRATIVE_FIELDS = {
    "story_summary",
    "description",
    "costume_description",
    "gift_dialogue",
    "mail_body",
    "mail_excerpt",
    "voice_excerpt",
    "对话",
    "台词",
    "性格特点",
    "角色简介",
    "角色介绍",
    "剧情简介",
    "信息简报",
    "心意寄语",
    "少女的心意",
    "生日庆祝",
    "生日寄语",
    "简介",
    "描述",
    "内容",
    "纪念内容",
}

BOILERPLATE_MARKERS = (
    "资料类型",
    "story_title",
    "story_subtitle",
    "speaker_names",
    "voice_lines",
    "section:",
    "trigger:",
    "ordinal:",
    "获取方式",
    "价格",
    "兑换",
    "活动等级",
    "版本",
    "稀有度",
    "攻击力",
    "防御力",
    "生命值",
    "技能描述",
    "角色名",
    "皮肤名",
    "适配装甲",
    "编辑",
    "页面贡献",
    "最新编辑",
    "MediaWiki",
)

NON_NARRATIVE_QUOTE_MARKERS = (
    "抵制不良游戏",
    "拒绝盗版游戏",
    "注意自我保护",
    "谨防受骗",
    "请勿传播",
    "推荐角色",
    "获取方式",
    "兑换",
    "活动等级",
)

# Character profile pages are authored in two different forms.  Some pages
# use ``字段：值`` while the older, hand-written pages put the field heading
# on one line and its value (or prose block) on the following lines.  Keep a
# broad stop-list so the second form can be read without swallowing the next
# section, table heading or a mechanics field into the identity evidence.
PROFILE_SECTION_HEADINGS = {
    "角色名",
    "角色名称",
    "代号",
    "神格",
    "生日",
    "性格特点",
    "角色简介",
    "角色介绍",
    "剧情简介",
    "信息简报",
    "心意寄语",
    "少女的心意",
    "生日庆祝",
    "生日寄语",
    "装甲介绍",
    "角色时装",
    "主线剧情",
    "个人故事",
    "基地剧情",
    "好感故事",
    "随机事件",
    "角色语音",
    "剧情相关",
    "登场时间",
    "角色邮件",
    "主题曲",
    "补充",
    "背景",
    "势力",
    "阵营",
    "所属",
    "类型",
    "稀有度",
    "版本",
    "获取方式",
    "适配装甲",
    "皮肤简介",
    "互动类型",
    "描述",
    "内容",
}


def _narrative_sentences(document: dict[str, Any]) -> list[str]:
    """Return narrative prose and attributed dialogue, excluding page noise."""
    result: list[str] = []
    for line in _lines(document.get("text", "")):
        matched_field = None
        value = ""
        # Match the longest field first because fields such as mail_excerpt
        # may themselves contain punctuation.
        for field in sorted(NARRATIVE_FIELDS, key=len, reverse=True):
            candidate = _field_value(line, field)
            if candidate:
                matched_field = field
                value = candidate
                break
        if matched_field:
            if any(marker in value for marker in NON_NARRATIVE_QUOTE_MARKERS):
                continue
            result.extend(_sentences(value))
            continue
        if any(marker in line for marker in BOILERPLATE_MARKERS):
            continue
        if any(marker in line for marker in NON_NARRATIVE_QUOTE_MARKERS):
            continue
        if "=" in line or "：" in line or ":" in line:
            # Dialogue blocks are handled by _speaker_dialogues.  An
            # unlabelled key/value line is not safe evidence for a trait.
            continue
        if line.startswith(("<", "[[", "{{", "--", "==", "1.", "2.", "3.")):
            continue
        result.extend(_sentences(line))
    return list(dict.fromkeys(result))


def _normalise_heading(value: str) -> str:
    """Normalise a standalone Wiki heading without changing its content."""

    value = _clean(value)
    value = re.sub(r"^[#=*]+", "", value)
    return value.strip(" ：:=-|；;")


def _looks_like_section_heading(value: str) -> bool:
    normalised = _normalise_heading(value)
    if not normalised:
        return True
    if normalised in PROFILE_SECTION_HEADINGS:
        return True
    # Table-of-contents ordinals (2, 2.1, 9.2.1) are not field values.
    if re.fullmatch(r"\d+(?:\.\d+)*", normalised):
        return True
    # Dialogue templates and their keys are not identity prose.
    if normalised in {"剧情格式文本", "剧情旁白文本", "剧情选项", "邮件内容", "图鉴信息"}:
        return True
    return False


def _heading_value(lines: list[str], index: int, field: str) -> str:
    """Read the value following a standalone field heading.

    The lakehouse keeps source line breaks, so this is intentionally bounded
    and stops at the next known section heading.  It is only used for explicit
    profile fields; arbitrary key/value and navigation lines remain excluded.
    """

    chunks: list[str] = []
    total_length = 0
    for candidate_line in lines[index + 1 : index + 40]:
        candidate = _clean(candidate_line)
        if not candidate:
            if chunks:
                break
            continue
        if _looks_like_section_heading(candidate):
            break
        if any(marker in candidate for marker in BOILERPLATE_MARKERS):
            break
        if candidate.startswith(("<", "[[", "{{", "--", "==")):
            break
        chunks.append(candidate)
        total_length += len(candidate)
        if total_length >= 900:
            break
    return _clean(" ".join(chunks))


def _speaker_dialogues(document: dict[str, Any], character_names: set[str]) -> list[str]:
    """Extract only lines explicitly attributed to the selected character."""
    lines = _lines(document.get("text", ""))
    result: list[str] = []
    speaker = ""
    for line in lines:
        if line.startswith(("剧情旁白文本", "剧情选项", "剧情格式文本")):
            speaker = ""
        speaker_value = ""
        for field in ("角色", "说话人", "speaker"):
            speaker_value = _field_value(line, field)
            if speaker_value:
                break
        if speaker_value:
            speaker = speaker_value.strip()
            continue
        dialogue_value = ""
        for field in ("对话", "台词"):
            dialogue_value = _field_value(line, field)
            if dialogue_value:
                break
        if dialogue_value and speaker in character_names:
            result.extend(_sentences(dialogue_value))

    # Voice pages use a compact ``trigger: line`` field rather than a role
    # template.  The preamble is still an original source quote.
    if document.get("source_type") == "character_voice":
        for line in lines:
            value = _field_value(line, "voice_excerpt")
            if value:
                for fragment in re.split(r"\n+", value):
                    fragment = re.sub(r"^[^:：/]+\s*[:：]\s*", "", fragment)
                    if fragment:
                        result.extend(_sentences(fragment))
    return list(dict.fromkeys(result))


def _evidence(
    document: dict[str, Any],
    quote: str,
    *,
    evidence_kind: str,
    matched_terms: Iterable[str] = (),
    interpretation: str | None = None,
) -> dict[str, Any]:
    item = {
        "document_id": str(document.get("document_id")),
        "evidence_document_ids": [str(document.get("document_id"))],
        "page_id": document.get("page_id"),
        "source_type": document.get("source_type"),
        "title": document.get("title"),
        "quote": _clean(quote),
        "evidence_kind": evidence_kind,
        "matched_terms": list(dict.fromkeys(str(term) for term in matched_terms if term)),
    }
    if interpretation:
        item["interpretation"] = interpretation
        item["interpretation_status"] = "inferred"
    return item


def _dedupe_evidence(items: Iterable[dict[str, Any]], limit: int = 16) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        quote = _clean(item.get("quote"))
        document_id = str(item.get("document_id") or "")
        key = (document_id, quote)
        if not quote or not document_id or key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item["quote"] = quote[:360]
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _support_level(items: list[dict[str, Any]], explicit: bool = False) -> str:
    pages = {str(item.get("page_id") or item.get("document_id")) for item in items}
    if explicit or len(pages) >= 2:
        return "supported"
    return "observed"


def _claim_items(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in items:
        key = _clean(item.get("quote"))
        if key:
            grouped[key].append(item)
    result: list[dict[str, Any]] = []
    for quote, evidence in grouped.items():
        refs = _dedupe_evidence(evidence, limit=4)
        direct_quote = any(item.get("evidence_kind") in {"dialogue", "structured_field"} for item in refs)
        result.append(
            {
                "kind": kind,
                "statement": f"原文中出现：{quote}",
                "support_level": _support_level(refs),
                "usage_rule": (
                    "可作为角色自述的直接证据；仍需结合语境回答。"
                    if direct_quote
                    else "仅作为叙事背景引用；不要直接改写成角色永久的第一人称偏好。"
                ),
                "evidence": refs,
                "evidence_document_ids": [str(item["document_id"]) for item in refs],
            }
        )
    result.sort(key=lambda item: (0 if item["support_level"] == "supported" else 1, item["statement"]))
    return result


def _direct_documents(documents: list[dict[str, Any]], character_id: str) -> list[dict[str, Any]]:
    selected = []
    for document in documents:
        metadata = document.get("metadata") or {}
        ids = {str(metadata.get("character_id"))} if metadata.get("character_id") else set()
        ids.update(str(value) for value in metadata.get("related_character_ids", []) or [] if value)
        if character_id in ids:
            selected.append(document)
    order = {source: index for index, source in enumerate(SOURCE_PRIORITY_ORDER)}
    return sorted(
        selected,
        key=lambda doc: (order.get(str(doc.get("source_type")), 99), str(doc.get("document_id") or "")),
    )


def _profile_fields(documents: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for document in documents:
        raw_lines = str(document.get("text", "") or "").replace("\r", "\n").split("\n")
        for index, raw_line in enumerate(raw_lines):
            line = _clean(raw_line)
            if not line:
                continue
            for field in fields:
                value = _field_value(line, field)
                if not value and _normalise_heading(line) == field:
                    value = _heading_value(raw_lines, index, field)
                if value and len(value) >= 4:
                    result.append(_evidence(document, value, evidence_kind="structured_field", matched_terms=(field,)))
    return _dedupe_evidence(result, limit=24)


def _preference_evidence(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    positives: list[dict[str, Any]] = []
    dislikes: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    for document in documents:
        if document.get("source_type") not in PREFERENCE_SOURCE_TYPES:
            continue
        for sentence in _narrative_sentences(document):
            matches = [cue for cue, _ in PREFERENCE_CUES if cue in sentence]
            if not matches:
                continue
            kinds = {kind for cue, kind in PREFERENCE_CUES if cue in sentence}
            item = _evidence(document, sentence, evidence_kind="narrative_text", matched_terms=matches)
            if "dislike" in kinds:
                dislikes.append(item)
            elif kinds.intersection({"value", "habit"}):
                values.append(item)
            elif kinds.intersection({"boundary", "fear"}):
                boundaries.append(item)
            else:
                positives.append(item)
    return (
        _dedupe_evidence(positives, 24),
        _dedupe_evidence(dislikes, 16),
        _dedupe_evidence(values, 24),
        _dedupe_evidence(boundaries, 16),
    )


def _emotion_evidence(documents: list[dict[str, Any]], dialogues_by_doc: dict[str, list[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for document in documents:
        if document.get("source_type") not in EMOTION_SOURCE_TYPES:
            continue
        doc_id = str(document.get("document_id"))
        candidates = dialogues_by_doc.get(doc_id) or _narrative_sentences(document)
        for sentence in candidates:
            for pattern, cues in EMOTION_CUES.items():
                matches = [cue for cue in cues if cue in sentence]
                if matches:
                    result.append(
                        _evidence(
                            document,
                            sentence,
                            evidence_kind="dialogue" if doc_id in dialogues_by_doc else "narrative_text",
                            matched_terms=matches,
                            interpretation=pattern,
                        )
                    )
    # Keep several independent examples per pattern, not the first repeated
    # chunk of the same page.
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in result:
        grouped[str(item.get("interpretation"))].append(item)
    output: list[dict[str, Any]] = []
    for pattern, items in grouped.items():
        output.extend(_dedupe_evidence(items, 5))
    return output[:32]


def _analyst_interactions(documents: list[dict[str, Any]], dialogues_by_doc: dict[str, list[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for document in documents:
        if document.get("source_type") not in EMOTION_SOURCE_TYPES:
            continue
        doc_id = str(document.get("document_id"))
        candidates = dialogues_by_doc.get(doc_id) or _narrative_sentences(document)
        for sentence in candidates:
            if "分析员" not in sentence and "指挥官" not in sentence:
                continue
            cues = [cue for cue in ("谢谢", "担心", "相信", "喜欢", "爱", "拜托", "别离开", "夸", "调侃", "喵", "哼") if cue in sentence]
            result.append(
                _evidence(
                    document,
                    sentence,
                    evidence_kind="dialogue" if doc_id in dialogues_by_doc else "narrative_text",
                    matched_terms=("分析员", *cues),
                    interpretation="direct_address_or_interaction",
                )
            )
    return _dedupe_evidence(result, 24)


def _narrative_evolution(documents: list[dict[str, Any]], dialogues_by_doc: dict[str, list[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for document in documents:
        if document.get("source_type") not in EVOLUTION_SOURCE_TYPES:
            continue
        doc_id = str(document.get("document_id"))
        candidates = dialogues_by_doc.get(doc_id) or _narrative_sentences(document)
        for sentence in candidates:
            matches = [marker for marker, terms in TEMPORAL_MARKERS.items() if any(term in sentence for term in terms)]
            if not matches:
                continue
            result.append(
                _evidence(
                    document,
                    sentence,
                    evidence_kind="dialogue" if doc_id in dialogues_by_doc else "narrative_text",
                    matched_terms=matches,
                    interpretation=";".join(matches),
                )
            )
    # Prefer explicit transition/current statements, then past references.
    result.sort(key=lambda item: (0 if "transition" in item.get("matched_terms", []) else 1, str(item.get("document_id"))))
    return _dedupe_evidence(result, 24)


def _evolution_buckets(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split temporal evidence without creating separate personality states."""

    buckets: dict[str, list[dict[str, Any]]] = {
        "past": [],
        "current": [],
        "transition": [],
    }
    for item in items:
        markers = set(item.get("matched_terms") or [])
        for bucket in buckets:
            if bucket in markers:
                buckets[bucket].append(item)
    return {name: _dedupe_evidence(values, 12) for name, values in buckets.items()}


def _dialogue_style(dialogues: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    lines = [quote for _, quote in dialogues if quote]
    if not lines:
        return {
            "status": "inferred",
            "observations": [],
            "evidence_document_ids": [],
            "note": "没有找到明确归属于该角色的对话行；不要凭空生成口癖。",
        }
    all_text = "\n".join(lines)
    punctuation = {
        "question_rate": round(sum(ch in "？?" for ch in all_text) / max(1, len(lines)), 3),
        "exclamation_rate": round(sum(ch in "！!" for ch in all_text) / max(1, len(lines)), 3),
        "ellipsis_rate": round(sum(ch in "…" for ch in all_text) / max(1, len(lines)), 3),
    }
    particle_counts = collections.Counter(
        particle for particle in STYLE_PARTICLES if particle in all_text
    )
    # Count occurrences rather than treating a single line as a permanent
    # verbal tic.  These are style signals, not hard facts.
    particle_counts = collections.Counter()
    for line in lines:
        for particle in STYLE_PARTICLES:
            particle_counts[particle] += line.count(particle)
    particles = [
        {"term": term, "count": count, "status": "inferred"}
        for term, count in particle_counts.most_common()
        if count >= 2
    ][:8]
    doc_ids = list(dict.fromkeys(str(doc.get("document_id")) for doc, _ in dialogues))[:32]
    observations = [
        {"name": "dialogue_line_count", "value": len(lines), "status": "observed"},
        {"name": "average_line_length", "value": round(sum(len(line) for line in lines) / len(lines), 1), "status": "inferred"},
        {"name": "punctuation", "value": punctuation, "status": "inferred"},
        {"name": "habitual_particles", "value": particles, "status": "inferred"},
    ]
    return {
        "status": "inferred",
        "observations": observations,
        "evidence_document_ids": doc_ids,
        "note": "句式统计只作为生成提示，不能单独证明角色永久口癖。",
    }


def _build_profile(character_id: str, character_name: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    direct = _direct_documents(documents, character_id)
    aliases = {character_name}
    for document in direct:
        metadata = document.get("metadata") or {}
        if metadata.get("character_name"):
            aliases.add(str(metadata["character_name"]))
    dialogues_by_doc: dict[str, list[str]] = {}
    dialogue_pairs: list[tuple[dict[str, Any], str]] = []
    for document in direct:
        lines = _speaker_dialogues(document, aliases)
        if lines:
            dialogues_by_doc[str(document.get("document_id"))] = lines
            dialogue_pairs.extend((document, line) for line in lines)
    dialogue_lines = [line for _, line in dialogue_pairs]

    address_items: list[dict[str, Any]] = []
    self_items: list[dict[str, Any]] = []
    catchphrase_counter: collections.Counter[str] = collections.Counter()
    catchphrase_evidence: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for document, line in dialogue_pairs:
        address_matches = [term for term in ADDRESS_TERMS if term in line]
        for term in address_matches:
            address_items.append(_evidence(document, line, evidence_kind="dialogue", matched_terms=(term,)))
        self_matches = [term for term in SELF_REFERENCE_TERMS if term in line]
        for term in self_matches:
            # Generic 我 is useful only as a count; special forms are more
            # informative and will be surfaced first.
            if term != "我" or len(line) <= 80:
                self_items.append(_evidence(document, line, evidence_kind="dialogue", matched_terms=(term,)))
        normalized_line = re.sub(r"\s+", "", line)
        if 8 <= len(normalized_line) <= 160:
            catchphrase_counter[normalized_line] += 1
            catchphrase_evidence[normalized_line].append(_evidence(document, line, evidence_kind="dialogue"))

    catchphrases = []
    for phrase, count in catchphrase_counter.most_common():
        if count < 2:
            continue
        refs = _dedupe_evidence(catchphrase_evidence[phrase], 4)
        catchphrases.append(
            {
                "phrase": phrase,
                "count": count,
                "support_level": _support_level(refs),
                "evidence": refs,
                "evidence_document_ids": [str(item["document_id"]) for item in refs],
            }
        )
        if len(catchphrases) >= 8:
            break

    preferences, dislikes, values, boundaries = _preference_evidence(direct)
    identity_fields = _profile_fields(direct, ("性格特点", "角色简介", "角色介绍", "装甲介绍"))
    address_items = _dedupe_evidence(address_items, 12)
    self_items = _dedupe_evidence(self_items, 12)
    all_evidence = []
    for collection in (identity_fields, address_items, self_items, preferences, dislikes, values, boundaries):
        all_evidence.extend(collection)
    all_evidence.extend(_emotion_evidence(direct, dialogues_by_doc))
    all_evidence.extend(_analyst_interactions(direct, dialogues_by_doc))
    evolution_evidence = _narrative_evolution(direct, dialogues_by_doc)
    evolution_buckets = _evolution_buckets(evolution_evidence)
    all_evidence.extend(evolution_evidence)
    all_evidence = _dedupe_evidence(all_evidence, 120)

    profile = {
        "profile_id": f"dialogue_style_{character_id}",
        "character_id": character_id,
        "character_name": character_name,
        "state_policy": "latest_available",
        "state_policy_note": "所有已入库剧情视为该角色已经经历过的背景；过去表现只作为时间顺序证据，不切换人格版本。",
        "identity_evidence": [
            {
                "statement": f"角色资料中关于{character_name}的原文字段",
                "support_level": _support_level(identity_fields, explicit=True),
                "evidence": identity_fields,
                "evidence_document_ids": [str(item["document_id"]) for item in identity_fields],
            }
        ] if identity_fields else [],
        "address_terms": [
            {
                "term": term,
                "support_level": _support_level([item for item in address_items if term in item.get("matched_terms", [])]),
                "evidence": _dedupe_evidence([item for item in address_items if term in item.get("matched_terms", [])], 4),
                "evidence_document_ids": list(dict.fromkeys(item["document_id"] for item in address_items if term in item.get("matched_terms", [])))[:4],
            }
            for term in dict.fromkeys(term for item in address_items for term in item.get("matched_terms", []))
        ],
        "self_reference_terms": [
            {
                "term": term,
                "support_level": _support_level([item for item in self_items if term in item.get("matched_terms", [])]),
                "evidence": _dedupe_evidence([item for item in self_items if term in item.get("matched_terms", [])], 4),
                "evidence_document_ids": list(dict.fromkeys(item["document_id"] for item in self_items if term in item.get("matched_terms", [])))[:4],
            }
            for term in dict.fromkeys(term for item in self_items for term in item.get("matched_terms", []))
        ],
        "catchphrases": catchphrases,
        "sentence_style": _dialogue_style(dialogue_pairs),
        "emotion_patterns": _emotion_evidence(direct, dialogues_by_doc),
        "supported_preferences": _claim_items(preferences, "preference_or_value"),
        "supported_dislikes": _claim_items(dislikes, "dislike_or_boundary"),
        "supported_values": _claim_items(values, "value_or_habit"),
        "supported_boundaries": _claim_items(boundaries, "boundary_or_fear"),
        "analyst_interaction": _analyst_interactions(direct, dialogues_by_doc),
        "narrative_evolution": {
            "policy": "latest_available",
            "evidence": evolution_evidence,
            "past": evolution_buckets["past"],
            "current": evolution_buckets["current"],
            "transitions": evolution_buckets["transition"],
            "latest_state_evidence": _dedupe_evidence(
                evolution_buckets["current"] + evolution_buckets["transition"],
                16,
            ),
            "note": (
                "只有带有过去/现在/转变标记的原文才进入此栏；past 只用于解释来历，"
                "current/transition 用于最新状态提示；不自动生成性格阶段快照。"
            ),
        },
        "evidence_document_ids": list(dict.fromkeys(str(item.get("document_id")) for item in all_evidence if item.get("document_id"))),
        "evidence_items": all_evidence,
        "source_counts": dict(collections.Counter(str(document.get("source_type")) for document in direct)),
        "dialogue_line_count": len(dialogue_lines),
        "review_status": "evidence_ready",
        "trait_activation_policy": "This artifact supplies evidence context only; persona_profiles.active_traits remains unchanged.",
        "generated_at": utc_now(),
        "schema_version": "dialogue-profile-1.1",
    }
    return profile


def build_dialogue_profiles() -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    if not documents:
        raise RuntimeError("Lakehouse documents are missing. Run python -m pipelines.build_lakehouse first.")
    profiles = [
        _build_profile(character_id, character_name, documents)
        for character_id, character_name in sorted(dialogue_characters().items(), key=lambda item: item[1])
    ]
    output = ensure_runtime("personas")
    profile_path = output / "dialogue_style_profiles.jsonl"
    write_jsonl(profile_path, profiles)
    report = {
        "stage": "B",
        "job": "build_dialogue_profiles",
        "generated_at": utc_now(),
        "profiles": len(profiles),
        "schema_version": "dialogue-profile-1.1",
        "documents_read": len(documents),
        "dialogue_lines": sum(int(profile.get("dialogue_line_count", 0)) for profile in profiles),
        "profiles_with_identity_evidence": sum(bool(profile.get("identity_evidence")) for profile in profiles),
        "profiles_with_preferences": sum(bool(profile.get("supported_preferences")) for profile in profiles),
        "profiles_with_dislikes": sum(bool(profile.get("supported_dislikes")) for profile in profiles),
        "profiles_with_temporal_evidence": sum(bool((profile.get("narrative_evolution") or {}).get("evidence")) for profile in profiles),
        "profiles_with_current_evidence": sum(bool((profile.get("narrative_evolution") or {}).get("current")) for profile in profiles),
        "profiles_with_transition_evidence": sum(bool((profile.get("narrative_evolution") or {}).get("transitions")) for profile in profiles),
        "output": str(profile_path),
        "policy": "Latest-state, evidence-only projection; Data/, graph, relation reviews and active_traits were not modified.",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_dialogue_profiles.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build_dialogue_profiles(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
