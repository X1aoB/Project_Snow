"""Pure content-block normalization and channel rules; no provider or storage access."""

from __future__ import annotations

import re
from typing import Any

from .mvp_policy import MVP_CHARACTERS, canonical_mvp_character
from .output_validation import _clean_renderable_text
from .text_rules import _compact, _contains_term

_TEXT_STAGE_ACTION_PATTERNS = (
    re.compile(r"[（(【\[*]\s*(?:我|她)?\s*(?:抱住|牵起|握住|抚摸|摸了摸|走到|靠近|凑到|坐到|亲了亲)"),
    re.compile(r"(?:说着|随后|接着)[，,]?\s*(?:我|她)?\s*(?:抱住|牵起|握住|抚摸|走到|靠近|凑到|坐到|亲了亲)"),
    re.compile(r"(?:我|她)(?:现在|此刻|随即)(?:抱住|牵起|握住|抚摸|走到|靠近|凑到|坐到|亲了亲)"),
)

_TEXT_UNSUPPORTED_VISUAL_TERMS = (
    "看你现在的表情",
    "看着你现在的表情",
    "看见你脸红",
    "看到你脸红",
    "你今天穿的",
    "看着你此刻",
    "看你的表情",
    "看见你的表情",
    "看到你的表情",
    "看着你的表情",
    "看见你穿着",
    "看到你穿着",
    "看见你的衣服",
    "看到你的衣服",
)
_TEXT_UNSUPPORTED_AUDIO_TERMS = (
    "通讯器里你的声音",
    "通讯器里的声音",
    "听到你的声音",
    "听见你的声音",
    "听着你的声音",
    "听到了你的声音",
)
_TEXT_COMPLETED_PHYSICAL_TERMS = (
    "我抱住你",
    "我抱住了你",
    "抱住你了",
    "我牵起你的手",
    "我牵住你的手",
    "我握住你的手",
    "我拉住你",
    "我拉住了你",
    "我亲了你",
    "我吻了你",
    "我靠近你",
    "我走到你身边",
    "我碰到了你",
)

# High-confidence stage-direction predicates used only for face-to-face
# content-block separation.  The list is deliberately concrete: a sentence is
# reclassified as ``action`` only when it starts with the active character (or
# an equivalent first/third-person subject) and contains one of these visible
# predicates.  Ambiguous prose is left for the model rewrite instead of being
# silently rewritten by the server.
_IN_PERSON_ACTION_PREDICATES = (
    "抬眼",
    "抬眸",
    "抬头",
    "低头",
    "偏头",
    "侧过脸",
    "转过身",
    "转身",
    "走近",
    "走到",
    "靠近",
    "后退",
    "坐下",
    "起身",
    "伸手",
    "收回手",
    "点头",
    "摇头",
    "眨眼",
    "闭眼",
    "睁眼",
    "皱眉",
    "挑眉",
    "扬眉",
    "微笑",
    "笑了",
    "轻笑",
    "叹气",
    "耸肩",
    "挥手",
    "抱臂",
    "握住",
    "牵起",
    "抱住",
    "松开",
    "接过",
    "递出",
    "看向",
    "看着",
    "望着",
    "凝视",
    "盯着",
    "望向",
    "注视",
    "目光",
    "神情",
    "表情",
    "唇角",
    "眉梢",
)
_IN_PERSON_ACTION_LABEL_PREFIX = re.compile(
    r"^\s*(?:〔\s*动作\s*〕|【\s*动作\s*】|\[\s*动作\s*\]|（\s*动作\s*）|\(\s*动作\s*\))\s*[:：]?\s*",
    re.IGNORECASE,
)
_IN_PERSON_SPOKEN_MARKERS = (
    "说道",
    "说着",
    "说：",
    "说:",
    "说“",
    "说「",
    "开口",
    "回答",
    "问道",
    "喊道",
    "低声说",
    "轻声说",
    "告诉",
    "解释",
)
_IN_PERSON_ANALYST_REACTION_PREDICATES = (
    "感到",
    "觉得",
    "意识到",
    "露出",
    "笑了",
    "哭了",
    "脸红",
    "害怕",
    "紧张",
    "满意",
    "开心",
    "难过",
    "生气",
    "点头",
    "摇头",
    "后退",
    "靠近",
)


def _normalize_content_blocks_with_diagnostics(
    generated: dict[str, Any],
    communication_channel: str,
    answer: str,
    character_name: str = "",
) -> tuple[list[dict[str, str]], bool]:
    allowed = {"message", "sticker"} if communication_channel == "text" else {"speech", "action"}
    blocks: list[dict[str, str]] = []
    reclassified = False
    canonical_character = canonical_mvp_character(character_name)
    action_names = sorted(
        {
            character_name,
            *(canonical_character.aliases if canonical_character else ()),
            canonical_character.source_name if canonical_character else "",
        }
        - {""},
        key=len,
        reverse=True,
    )

    def normalize_action(value: str) -> str:
        action = value.strip()
        while len(action) >= 2 and action[0] in "（(" and action[-1] in "）)":
            action = action[1:-1].strip()
        if not action:
            return ""
        if character_name:
            if action.startswith("我的"):
                action = character_name + "的" + action[2:]
            elif action.startswith(("我", "她")):
                action = character_name + action[1:]
            elif action.startswith(("少女", "女孩", "角色")):
                action = re.sub(r"^(?:少女|女孩|角色)", character_name, action, count=1)
            else:
                matched_name = next(
                    (candidate for candidate in action_names if action.startswith(candidate)),
                    "",
                )
                if matched_name:
                    action = character_name + action[len(matched_name) :]
                else:
                    action = character_name + action
        return action

    def split_leading_actions(value: str) -> tuple[list[str], str]:
        actions: list[str] = []
        remainder = value.strip()
        for _ in range(2):
            match = re.match(
                r"^[（(]+\s*([^（）()\r\n]{2,240}?)\s*[）)]+\s*(?:\r?\n+|$)",
                remainder,
            )
            if not match:
                break
            action = normalize_action(match.group(1))
            if action:
                actions.append(action)
            remainder = remainder[match.end() :].strip()
        return actions, remainder

    def split_leading_action_sentences(value: str) -> tuple[list[str], str]:
        actions: list[str] = []
        remainder = value.strip()
        subjects = tuple(action_names) + ("我", "她", "少女", "女孩", "角色")
        for _ in range(2):
            match = re.match(r"^([^。！？!?\r\n]{2,240}[。！？!?])\s*(.*)$", remainder, re.S)
            if not match:
                break
            sentence = match.group(1).strip()
            if not sentence.startswith(subjects):
                break
            if not any(predicate in sentence for predicate in _IN_PERSON_ACTION_PREDICATES):
                break
            if any(marker in sentence for marker in _IN_PERSON_SPOKEN_MARKERS):
                break
            if any(mark in sentence for mark in ("“", "”", "「", "」", "『", "』", '"')):
                break
            action = normalize_action(sentence)
            if not action:
                break
            actions.append(action)
            remainder = match.group(2).strip()
        return actions, remainder

    def split_labeled_action_paragraphs(value: str) -> tuple[list[str], str]:
        actions: list[str] = []
        speech_paragraphs: list[str] = []
        for paragraph in re.split(r"\r?\n\s*\r?\n+", value.strip()):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            match = _IN_PERSON_ACTION_LABEL_PREFIX.match(paragraph)
            if not match:
                speech_paragraphs.append(paragraph)
                continue
            candidate = paragraph[match.end() :].strip()
            # A labelled paragraph that also contains dialogue is not safe
            # to split automatically. Leave it in speech so the final
            # communication guard requests one model rewrite instead.
            if (
                not candidate
                or "\n" in candidate
                or any(marker in candidate for marker in _IN_PERSON_SPOKEN_MARKERS)
                or any(mark in candidate for mark in ("“", "”", "「", "」", "『", "』", '"'))
            ):
                speech_paragraphs.append(paragraph)
                continue
            action = normalize_action(candidate)
            if action:
                actions.append(action)
            else:
                speech_paragraphs.append(paragraph)
        return actions, "\n\n".join(speech_paragraphs)

    def append_block(block_type: str, text: str) -> None:
        nonlocal reclassified
        if communication_channel == "in_person" and block_type == "action":
            normalized = normalize_action(text)
            if normalized:
                blocks.append({"type": "action", "text": normalized})
            return
        if communication_channel == "in_person" and block_type == "speech":
            actions, speech = split_labeled_action_paragraphs(text)
            parenthesized_actions, speech = split_leading_actions(speech)
            actions.extend(parenthesized_actions)
            sentence_actions, speech = split_leading_action_sentences(speech)
            actions.extend(sentence_actions)
            if actions:
                reclassified = True
            blocks.extend({"type": "action", "text": action} for action in actions)
            if speech:
                blocks.append({"type": "speech", "text": speech})
            return
        blocks.append({"type": block_type, "text": text})

    raw_blocks = generated.get("content_blocks")
    model_sticker_seen = False
    model_sticker: dict[str, str] | None = None
    if isinstance(raw_blocks, list):
        for item in raw_blocks:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").strip().casefold()
            if communication_channel == "text" and block_type == "sticker":
                # Sticker ids are opaque values.  The public facade
                # resolves them against its signed manifest; the MVP
                # layer only preserves the shape and never trusts a URL
                # or a client supplied filename.
                asset_id = str(item.get("asset_id") or "").strip()
                if not model_sticker_seen and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}", asset_id):
                    model_sticker_seen = True
                    model_sticker = {
                        "type": "sticker",
                        "asset_id": asset_id,
                        "caption": _clean_renderable_text(item.get("caption"))[:120],
                    }
                continue
            text = _clean_renderable_text(item.get("text")) if isinstance(item.get("text"), str) else ""
            if block_type in allowed and text:
                append_block(block_type, text)
    default_type = "message" if communication_channel == "text" else "speech"
    clean_answer = _clean_renderable_text(answer) if isinstance(answer, str) else ""
    # A number of compatible providers return a useful top-level answer
    # alongside a sticker-only block list. Preserve that prose instead of
    # letting the metadata block erase it. If verbal blocks already exist,
    # they remain authoritative and the top-level duplicate is ignored.
    if clean_answer and not any(block.get("type") != "sticker" for block in blocks):
        append_block(default_type, clean_answer)
    if model_sticker is not None:
        blocks.append(model_sticker)
    return blocks, reclassified


def _normalize_content_blocks(
    generated: dict[str, Any],
    communication_channel: str,
    answer: str,
    character_name: str = "",
) -> list[dict[str, str]]:
    blocks, _reclassified = _normalize_content_blocks_with_diagnostics(
        generated,
        communication_channel,
        answer,
        character_name,
    )
    return blocks


def _render_content_blocks(blocks: list[dict[str, str]]) -> str:
    rendered = []
    for block in blocks:
        # Last-mile sanitisation is intentional: blocks may be supplied by
        # a retry/fallback path that did not pass through the envelope
        # parser (or by a persisted legacy response).
        text = _clean_renderable_text(block.get("text")) if isinstance(block.get("text"), str) else ""
        if not text:
            continue
        rendered.append(f"（{text}）" if block.get("type") == "action" else text)
    return "\n".join(rendered)


def _communication_block_violations(
    message: str,
    answer: str,
    communication_channel: str,
    content_blocks: Any,
) -> list[str]:
    violations: list[str] = []
    block_texts: list[str] = []
    normalized_blocks: list[tuple[str, str]] = []
    if isinstance(content_blocks, list):
        allowed = {"message", "sticker"} if communication_channel == "text" else {"speech", "action"}
        for item in content_blocks:
            if not isinstance(item, dict):
                violations.append("communication_block_type:invalid")
                continue
            block_type = str(item.get("type") or "").strip().casefold()
            block_text = str(item.get("text") or "").strip()
            if block_type == "sticker":
                # A sticker is metadata, not spoken text.  It is checked
                # by the public manifest boundary and must not trigger
                # the physical-action checks below.
                if communication_channel != "text":
                    violations.append("communication_block_type:in_person:sticker")
                continue
            if block_text:
                block_texts.append(block_text)
                normalized_blocks.append((block_type, block_text))
            if block_type not in allowed:
                violations.append(
                    f"communication_block_type:{communication_channel}:{block_type or 'missing'}"
                )
    if communication_channel == "in_person":
        known_names = sorted(
            {character.display_name for character in MVP_CHARACTERS}
            | {alias for character in MVP_CHARACTERS for alias in character.aliases},
            key=len,
            reverse=True,
        )
        stage_subjects = tuple(known_names) + ("我", "她", "少女", "女孩", "角色")
        for block_type, block_text in normalized_blocks:
            compact_block = _compact(block_text)
            if block_type == "speech":
                contains_action_label = any(
                    _IN_PERSON_ACTION_LABEL_PREFIX.match(paragraph)
                    for paragraph in re.split(r"\r?\n\s*\r?\n+", block_text)
                )
                if contains_action_label or re.match(r"^[（(【\[]", block_text):
                    violations.append("in_person_speech_contains_action")
                    continue
                first_sentence = re.split(r"(?<=[。！？!?])", block_text, maxsplit=1)[0]
                if first_sentence.startswith(stage_subjects) and any(
                    predicate in first_sentence for predicate in _IN_PERSON_ACTION_PREDICATES
                ):
                    violations.append("in_person_speech_contains_action")
            elif block_type == "action":
                has_quotation = any(mark in block_text for mark in ("“", "”", "「", "」", "『", "』", '"'))
                if has_quotation or any(marker in block_text for marker in _IN_PERSON_SPOKEN_MARKERS):
                    violations.append("in_person_action_contains_speech")
                for predicate in _IN_PERSON_ANALYST_REACTION_PREDICATES:
                    analyst_claim = re.search(
                        rf"(?:你|分析员)[^。！？!?\r\n]{{0,16}}{re.escape(predicate)}",
                        compact_block,
                    )
                    if analyst_claim and not _contains_term(message, predicate):
                        violations.append(f"in_person_action_invents_analyst_reaction:{predicate}")
        return list(dict.fromkeys(violations))
    if communication_channel != "text":
        return violations
    inspected_text = "\n".join([answer, *block_texts])
    if any(pattern.search(inspected_text) for pattern in _TEXT_STAGE_ACTION_PATTERNS):
        violations.append("text_channel_physical_action")
    if any(
        _contains_term(inspected_text, term) and not _contains_term(message, term)
        for term in _TEXT_COMPLETED_PHYSICAL_TERMS
    ):
        violations.append("text_channel_physical_action")
    for term in _TEXT_UNSUPPORTED_VISUAL_TERMS:
        if _contains_term(inspected_text, term) and not _contains_term(message, term):
            violations.append(f"text_channel_unseen_visual:{term}")
    # ``voice`` is intentionally not an exposed channel in the MVP.  A
    # text message may express a wish to hear the character, but the
    # character must not claim that it has literally heard the analyst's
    # voice unless the analyst explicitly supplied that premise.
    for term in _TEXT_UNSUPPORTED_AUDIO_TERMS:
        if _contains_term(inspected_text, term) and not _contains_term(message, term):
            violations.append(f"text_channel_unseen_audio:{term}")
    return violations
