"""Versioned, source-backed style experiments for character dialogue."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .mvp_policy import MVP_CHARACTER_BY_NAME
from .repository import RuntimeRepository


@dataclass(frozen=True)
class StyleSettings:
    examples: str = "static"
    signature_guard: str = "bubu_legacy"
    signature_prompt: str = "bubu_legacy"
    register_guard: str = "fenny_legacy"
    register_prompt: str = "fenny_legacy"
    shared_facts: str = "futiya_legacy"
    situational_cues: str = "futiya_legacy"
    fallback: str = "legacy"
    copy: str = "measure"
    window: int = 6
    coefficient: float = 1.0
    example_count: int = 4
    keep_repeated_lines: bool = True

    @classmethod
    def from_environment(cls) -> StyleSettings:
        def setting(name: str, allowed: tuple[str, ...]) -> str:
            key = "PROJECT_SNOW_STYLE_" + name
            value = os.environ.get(key, allowed[0])
            if value not in allowed:
                raise ValueError(f"{key} must be one of {', '.join(allowed)}")
            return value

        window = int(os.getenv("PROJECT_SNOW_STYLE_WINDOW", "6"))
        coefficient = float(os.getenv("PROJECT_SNOW_STYLE_COEFFICIENT", "1"))
        example_count = int(os.getenv("PROJECT_SNOW_STYLE_EXAMPLE_COUNT", "4"))
        # The conversation store retains the last six assistant turns.
        if not 1 <= window <= 7 or not 0 < coefficient <= 100 or not 1 <= example_count <= 8:
            raise ValueError("Invalid style window (1-7), coefficient (0-100] or example count (1-8)")
        return cls(
            examples=setting("EXAMPLES", ("static", "retrieved")),
            signature_guard=setting("SIGNATURE_GUARD", ("bubu_legacy", "data_driven", "off")),
            signature_prompt=setting("SIGNATURE_PROMPT", ("bubu_legacy", "data_driven", "off")),
            register_guard=setting("REGISTER_GUARD", ("fenny_legacy", "data_driven", "off")),
            register_prompt=setting("REGISTER_PROMPT", ("fenny_legacy", "data_driven", "off")),
            shared_facts=setting("SHARED_FACTS", ("futiya_legacy", "data_driven", "off")),
            situational_cues=setting("SITUATIONAL_CUES", ("futiya_legacy", "reviewed_table", "off")),
            fallback=setting("FALLBACK", ("legacy", "minimal_repair")),
            copy=setting("COPY", ("measure", "rewrite")),
            window=window, coefficient=coefficient, example_count=example_count,
            keep_repeated_lines=setting("KEEP_REPEATED_LINES", ("true", "false")) == "true",
        )


def validate_style_inputs(root: Path, config: StyleSettings) -> None:
    """Catch incomplete candidate packages before accepting chat requests."""
    for table, required in (
        ("signature_terms", "data_driven" in (config.signature_guard, config.signature_prompt)),
        ("register_terms", "data_driven" in (config.register_guard, config.register_prompt)),
        ("shared_topics", config.shared_facts == "data_driven"),
        ("situational_cues", config.situational_cues == "reviewed_table"),
    ):
        if required:
            reviewed_rows(root, table, "")
    if config.examples == "retrieved":
        uri = (root / "examples.sqlite3").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("SELECT character_id, page_id, document_id, source_type, reply, terms FROM examples LIMIT 0")


LEGACY = StyleSettings()
SIGNATURE_CATEGORIES = {"self_reference", "address", "particle", "catchphrase"}


@lru_cache(maxsize=16)
def _reviewed_by_character(path: Path, modified: int, size: int) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            # One source-bound reaction already shipped in 1.3.0. Preserve its
            # provenance without recording a human review that did not happen.
            migrated = (
                path.name == "situational_cues.jsonl" and row.get("review_status") == "legacy"
                and row.get("character_id") == "a2ffc5b44d7f" and row.get("required_opening") == "干什么！"
                and row.get("co_speaker") == "妮塔" and row.get("document_id") and row.get("page_id")
            )
            if row.get("review_status") == "approved" or migrated:
                rows[row["character_id"]].append(row)
    return rows


def reviewed_rows(root: Path, table: str, character_id: str) -> list[dict]:
    path = root / (table + ".jsonl")
    stat = path.stat()
    return _reviewed_by_character(path, stat.st_mtime_ns, stat.st_size).get(character_id, [])


def signature_violations(message: str, answer: str, turns: list, rows: list[dict], config: StyleSettings) -> list[str]:
    history = [str(turn.get("assistant") or "") for turn in turns]
    history = history[max(0, len(history) - config.window + 1):]
    violations = []
    for row in rows:
        term = row["term"]
        if not term or row.get("category") not in SIGNATURE_CATEGORIES or term in message or term not in answer:
            continue
        maximum = max(1, math.ceil(float(row["rate"]) * config.window * config.coefficient))
        if (answer.count(term) >= 2 and row.get("repeat_rate", 0) < 0.1
                or 1 + sum(term in previous for previous in history) > maximum):
            violations.append("signature_overuse:" + term)
    return violations


def signature_guidance(rows: list[dict]) -> str:
    terms = [f"“{row['term']}”约每{max(1, round(1 / row['rate']))}句一次"
             for row in rows if row.get("category") in SIGNATURE_CATEGORIES and 0 < row["rate"] <= 1]
    return "\n【标志性用语频率】\n" + "；".join(terms) + "。仅在合适语境自然使用。\n" if terms else ""


_SPEAKER_LINE = re.compile(r"(?m)^(?:角色|说话人|speaker)\s*[=：:]\s*([^\r\n]+)")


def shared_excerpt(text: str, speaker: str, companions: list[str], topics: list[str]) -> str:
    """Require two directly attributed speakers and a topic within one 560-character scene."""
    speaking = [(m.start(), m.end(), m.group(1).strip()) for m in _SPEAKER_LINE.finditer(text)]
    topic_spans = []
    for line in re.finditer(r"(?m)^(?:对话|台词|voice_excerpt)\s*[=：:]\s*([^\r\n]+)", text):
        for term in topics:
            if term and (pos := line.group(1).find(term)) >= 0:
                topic_spans.append((line.start(), line.start(1) + pos + len(term)))
    for start, end, name in speaking:
        if name != speaker:
            continue
        for other_start, other_end, other in speaking:
            if other not in companions or other == name:
                continue
            left = min(start, other_start)
            right = max(end, other_end)
            for topic_start, topic_end in topic_spans:
                first = min(left, topic_start)
                last = max(right, topic_end)
                if last - first <= 560:
                    start_at = max(0, last - 560, first - 75)
                    return text[start_at:start_at + 560]
    return ""


def shared_evidence(cross: dict) -> dict | None:
    if not cross.get("active"):
        return None
    speaker = cross.get("speaking_character")
    names = [item["canonical_name"] for item in cross.get("mentioned_characters", []) if item.get("canonical_name")]
    for item in cross.get("evidence", []):
        excerpt = item.get("excerpt", "")
        if (item.get("source_type") == "main_story" and item.get("document_id") and speaker
                and shared_excerpt(excerpt, speaker, names, cross.get("topic_terms", []))):
            return item
    return None


def cue_hint(rows: list[dict], message: str, documents: dict) -> dict | None:
    for row in rows:
        if not any(term and term in message for term in row.get("triggers", [])):
            continue
        doc = documents.get(row.get("document_id")) or {}
        if not doc or not row.get("page_id") or row["page_id"] != doc.get("page_id"):
            continue
        source_text = doc.get("text", "")
        speakers = {match.group(1).strip() for match in _SPEAKER_LINE.finditer(source_text)}
        if row.get("co_speaker") and row["co_speaker"] not in speakers:
            continue
        if row.get("review_status") == "legacy" and ("芙提雅" not in speakers or "妮塔你……干什么！" not in source_text):
            continue
        hint = {"kind": "reviewed_source_cue", "guidance": row["hint"],
                "page_id": row["page_id"], "document_id": row["document_id"]}
        if row.get("review_status") == "legacy":
            hint["kind"] = "source_bound_body_teasing"
        if row.get("strength") == "required":
            hint["required_opening"] = row["required_opening"]
        return hint
    return None


def example_tokens(examples: list, profile: dict | None, *, static_has_voice: bool = False) -> int:
    """Use the audit's UTF-8 bytes / 3 estimate for the changed prompt fields."""
    payload = {"voice_evidence": examples}
    if profile is not None:
        payload["repeated_lines"] = profile.get("repeated_lines", [])
        for key in ("emotion_patterns", "analyst_interaction"):
            payload[key] = [] if static_has_voice else profile.get(key, [])
    return math.ceil(len(json.dumps(payload, ensure_ascii=False).encode()) / 3)


def _bigrams(value: str) -> set[str]:
    value = re.sub(r"\W+", "", value)
    return {value[i:i + 2] for i in range(len(value) - 1)}


def retrieved_examples(root: Path, character_id: str, message: str, count: int, max_chars: int) -> list[dict]:
    """Lexical recall on the analyst side, then maximal marginal relevance on replies."""
    query = RuntimeRepository._fts_query(message[:256])
    if query == '""':
        return []
    uri = (root / "examples.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute("""
            SELECT page_id, document_id, source_type, reply FROM examples
            WHERE examples MATCH ? AND character_id = ?
            ORDER BY bm25(examples), page_id, document_id, reply, rowid LIMIT ?
        """, (query, character_id, count * 12)).fetchall()
    pool = []
    seen = set()
    for rank, (page, document, source, reply) in enumerate(rows):
        quote = reply.strip()[:max_chars]
        if not quote or quote in seen:
            continue
        seen.add(quote)
        score = 1 / (1 + rank / 8) + (0.18 if source in {"affinity_story", "random_events", "random_event"} else 0)
        score -= min(len(reply), 200) / 1000
        score -= 0.1 * sum(name in quote for name in MVP_CHARACTER_BY_NAME if len(name) > 1)
        pool.append(({"page_id": page, "document_id": document, "source_type": source, "quote": quote},
                     _bigrams(quote), score))
    chosen = []
    while pool and len(chosen) < count:
        def relevance(item: tuple) -> float:
            row, terms, score = item
            similarity = max((len(terms & prior) / max(1, len(terms | prior))
                              + (0.18 if row["page_id"] == other["page_id"] else 0)
                              for other, prior, _ in chosen), default=0)
            return score - 0.6 * similarity
        index = max(range(len(pool)), key=lambda i: relevance(pool[i]))
        chosen.append(pool.pop(index))
    return [item[0] for item in chosen]


def minimal_repair(answer: str, terms: list[str], *, min_chars: int = 6) -> str:
    clauses = re.findall(r"[^，,；;。！？!?\n]+[，,；;。！？!?]*", answer)
    kept = "".join(part for part in clauses if not any(term in part for term in terms if term)).strip()
    if len(re.findall(r"\w", kept)) < min_chars:
        return ""
    return re.sub(r"[，,；;]$", "。", kept)


def role_terms(context: dict[str, Any]) -> tuple[str, str]:
    profile = context.get("dialogue_profile") or {}
    def first(key: str) -> str:
        return next((row["term"] for row in profile.get(key, []) if row.get("term") and row.get("evidence_document_ids")), "")
    address = (context.get("relationship_address_memory") or {}).get("preferred_address") or first("address_terms")
    return str(address) + "，" if address else "", first("self_reference_terms") or "我"


def neutral_fallback(context: dict, avoid: list[str]) -> str:
    address, subject = role_terms(context)
    if any(term in address for term in avoid):
        address = ""
    if any(term in subject for term in avoid):
        subject = "我"
    options = (f"{address}{subject}在听，你接着说。", "这件事，你想从哪里说起？", "嗯，慢慢来就好。")
    return next((value for value in options if not any(term in value for term in avoid)), "嗯。")

