"""Bounded expression references from explicitly attributed character speech.

These references describe *how* a character expresses herself.  They are not
relationship facts, live scene state, or a new approved personality profile.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


EXPRESSION_RULE = (
    "人物形象与事实判定分开：character_expression_reference 中有明确说话人的台词用于理解表达习惯、"
    "自主意愿和互动方式，原文情境不能变成本轮事实。先回答本轮问题，再按该角色的表达习惯自然表达"
    "自己的想法、期待、玩笑或一个可由分析员拒绝的小提议；不必等待分析员逐项发问。"
    "主动性不等于每轮反问、统一变外向或擅自升级亲密关系；安静、含蓄的角色也可以表达自己的选择。"
    "不要照搬原句、强加新话题，分析员要休息、结束或只要简短答案时自然收束。"
    "提议只表示此刻的意愿，不代表双方已经同意、已经移动或已有共同经历；"
    "不得借机透露未被询问的精确位置，也不触发后台消息。"
)
EXPRESSION_SOURCES = frozenset({
    "character_voice", "character_story", "affinity_story", "random_event",
    "character_affection", "special_mail",
})
IDENTITY_SOURCES = frozenset({"character_profile", "character_profiles", "character_armor"})
_FIELD = re.compile(r"^(角色|说话人|speaker|对话|台词|voice_excerpt)\s*[=:：]\s*(.*)$")
_IDENTITY_FIELD = re.compile(r"^(性格特点|角色简介|角色介绍|装甲介绍)\s*[=:：]\s*(.+)$")
_IDENTITY_HEADINGS = {"性格特点", "角色简介", "角色介绍", "装甲介绍"}
_EXPRESSIONS = (
    ("self_initiated", re.compile(r"我(?:想|来|要|决定|准备|有个)|让我|要不要|不如|一起|快看|先答应")),
    ("playful", re.compile(r"嘻嘻|嘿嘿|哇呼|猜猜|逗你|开玩笑|哼|嘛[！!？?。]?$")),
    ("care", re.compile(r"担心|陪你|放心|累了|休息|别勉强|照顾")),
    ("affection", re.compile(r"喜欢你|喜欢分析员|想你|开心|高兴|期待")),
)


def direct_character_documents(
    character_id: str, documents: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Related-character links never establish the speaker's identity."""
    return [document for document in documents
            if str((document.get("metadata") or {}).get("character_id") or "") == character_id
            and not (document.get("metadata") or {}).get("requires_costume_context")
            and document.get("source_type") != "character_costume"]


def character_speaker_names(
    character_id: str, character_name: str, documents: Iterable[dict[str, Any]],
) -> set[str]:
    """Resolve exact armor-qualified names from this character's metadata.

    Prefix/substring matching would also admit another character, a quoted
    impersonator, or narration.  Neither unknown speakers nor costume names
    are inferred as aliases.
    """
    direct = direct_character_documents(character_id, documents)
    names = {character_name} if character_name else set()
    names.update(str((doc.get("metadata") or {}).get("character_name") or "") for doc in direct)
    names.discard("")
    armors = {str((doc.get("metadata") or {}).get("armor_name") or "") for doc in direct}
    armors.discard("")
    return names | {f"{name}{separator}{armor}" for name in names for armor in armors
                    for separator in ("", "·", "-", "—", " ")}


def attributed_lines(document: dict[str, Any], speaker_names: set[str]) -> list[tuple[str, str]]:
    """Parse normalized lakehouse records; never fall back to plot prose."""
    result: list[tuple[str, str]] = []
    speaker = ""
    for raw in str(document.get("text") or "").replace("\r", "\n").splitlines():
        line = raw.strip()
        if line.startswith(("剧情旁白文本", "剧情选项", "剧情格式文本")):
            speaker = ""
        match = _FIELD.match(line)
        if not match:
            continue
        field, value = match.groups()
        if field in {"角色", "说话人", "speaker"}:
            speaker = value.strip()
            continue
        if field == "voice_excerpt" and document.get("source_type") == "character_voice":
            speaker = str((document.get("metadata") or {}).get("character_name") or "")
            value = re.sub(r"^[^:：/]+[:：]\s*", "", value)
        if speaker not in speaker_names:
            continue
        # Keep original complete clauses, not a fabricated paraphrase.  Inner
        # monologue and markup are not examples of outward conversational voice.
        for quote in re.split(r"(?<=[。！？!?；;])", value):
            quote = quote.strip()
            if 6 <= len(quote) <= 120 and not quote.startswith(("（", "(", "<", "{{", "[[")):
                result.append((speaker, quote))
    return list(dict.fromkeys(result))


def _identity_quotes(document: dict[str, Any]) -> list[str]:
    quotes = []
    after_heading = False
    for raw in str(document.get("text") or "").splitlines():
        line = raw.strip()
        match = _IDENTITY_FIELD.match(line)
        if match:
            quotes.append(match.group(2))
        elif after_heading and line:
            if len(line) >= 12 and not re.search(r"[=<>]|^剧情", line):
                quotes.append(line)
            after_heading = False
        if line in _IDENTITY_HEADINGS:
            after_heading = True
    return [quote for quote in quotes if len(quote) <= 360]


def expression_reference(
    character_id: str, character_name: str, documents: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    direct = direct_character_documents(character_id, documents)
    speaker_names = character_speaker_names(character_id, character_name, direct)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identity = []
    seen: set[str] = set()
    source_order = {source: index for index, source in enumerate((
        "character_voice", "affinity_story", "character_story", "random_event",
        "character_affection", "special_mail",
    ))}
    for document in sorted(direct, key=lambda d: (
        source_order.get(str(d.get("source_type")), 99), str(d.get("document_id") or ""),
    )):
        source = str(document.get("source_type") or "")
        ref = {key: str(document.get(key) or "") for key in ("document_id", "page_id", "source_type")}
        if not ref["document_id"]:
            continue
        if source in IDENTITY_SOURCES:
            for quote in _identity_quotes(document):
                if quote not in seen and len(identity) < 2:
                    seen.add(quote)
                    identity.append({**ref, "quote": quote})
        if source not in EXPRESSION_SOURCES:
            continue
        for speaker, quote in attributed_lines(document, speaker_names):
            if quote in seen:
                continue
            seen.add(quote)
            # Categories balance *examples*, not permanent personality traits.
            function = next((name for name, pattern in _EXPRESSIONS if pattern.search(quote)), "response")
            groups[function].append({**ref, "speaker": speaker, "quote": quote,
                                     "expression_function": function})

    selected = []
    page_counts: dict[str, int] = defaultdict(int)
    # Round-robin functions and independent pages keep one long scene or many
    # overlapping chunks from consuming the entire expression budget.
    for page_limit in (1, 2):
        while len(selected) < 8:
            changed = False
            for items in groups.values():
                index = next((i for i, item in enumerate(items)
                              if page_counts[item["page_id"] or item["document_id"]] < page_limit), None)
                if index is None:
                    continue
                item = items.pop(index)
                page_counts[item["page_id"] or item["document_id"]] += 1
                selected.append(item)
                changed = True
                if len(selected) == 8:
                    break
            if not changed:
                break
    if not identity and not selected:
        return {}
    return {
        "schema_version": "character-expression-1",
        "character_id": character_id,
        "scope": "expression_only_not_canonical_facts_or_current_events",
        "identity_evidence": identity,
        "voice_evidence": selected,
    }
