"""Build isolated first-pass character views for the dialogue MVP.

The view is a read-only projection of existing App/runtime artifacts.  It does
not crawl, delete, rewrite Data/, approve relations, activate traits, or alter
the canonical graph.  Re-running it is safe and deterministic apart from
timestamps in the report.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from backend.snow_app.mvp_policy import (
    FEEDBACK_OPTIONS,
    LAYER_ORDER,
    MVP_CHARACTERS,
    MVP_REGISTRY_VERSION,
    layer_policy,
    question_bank,
    source_layer,
)

from .common import RUNTIME_ROOT, ensure_runtime, load_runtime_jsonl, utc_now, write_json, write_jsonl


def _compact(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-·・,，。！？!?、:：;；'\"“”‘’()（）\[\]【】<>《》]+", "", normalized)


def _load_personas() -> dict[str, dict[str, Any]]:
    path = RUNTIME_ROOT / "personas" / "persona_profiles.jsonl"
    if not path.exists():
        return {}
    return {
        str(row.get("character_id")): row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if row.get("character_id")
    }


def _load_dialogue_profiles() -> dict[str, dict[str, Any]]:
    path = RUNTIME_ROOT / "personas" / "dialogue_style_profiles.jsonl"
    if not path.exists():
        return {}
    return {
        str(row.get("character_id")): row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if row.get("character_id")
    }


def _document_character_ids(document: dict[str, Any]) -> set[str]:
    metadata = document.get("metadata") or {}
    result = {str(metadata["character_id"])} if metadata.get("character_id") else set()
    result.update(str(value) for value in metadata.get("related_character_ids", []) or [] if value)
    result.update(
        str(item.get("character_id"))
        for item in metadata.get("logistics_relationships", []) or []
        if isinstance(item, dict) and item.get("character_id")
    )
    return result


def _document_link_kind(document: dict[str, Any], character_id: str) -> str | None:
    metadata = document.get("metadata") or {}
    if str(document.get("source_type") or "") == "logistics_lore" and any(
        str(item.get("character_id") or "") == character_id
        for item in metadata.get("logistics_relationships", []) or []
        if isinstance(item, dict)
    ):
        return "linked"
    if str(metadata.get("character_id") or "") == character_id:
        return "direct"
    if character_id in {str(value) for value in metadata.get("related_character_ids", []) or [] if value}:
        return "direct"
    return None


def _document_scope(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    return source_layer(document.get("source_type"), bool(metadata.get("requires_costume_context")))


def _character_aliases(character: Any) -> set[str]:
    return {_compact(value) for value in (character.display_name, character.source_name, *character.aliases) if value}


def _document_mentions_character(document: dict[str, Any], character: Any) -> bool:
    title = str(document.get("title") or "")
    text = str(document.get("text") or "")
    metadata = document.get("metadata") or {}
    haystack = _compact(
        " ".join((title, text, json.dumps(metadata, ensure_ascii=False)))
    )
    for alias in _character_aliases(character):
        if not alias:
            continue
        # Single-character names (for example 肴 or 晴) are too ambiguous for
        # unrestricted substring matching.  Accept an exact title/metadata
        # match or a standalone mention surrounded by non-CJK punctuation.
        if len(alias) == 1:
            if _compact(title) == alias or any(
                _compact(metadata.get(field)) == alias
                for field in ("character_name", "source_name", "owner_character_name")
            ):
                return True
            if re.search(
                rf"(?<![\u3400-\u4dbf\u4e00-\u9fff]){re.escape(alias)}"
                rf"(?![\u3400-\u4dbf\u4e00-\u9fff])",
                " ".join((title, text)),
            ):
                return True
            continue
        if alias in haystack:
            return True
    return False


# Documents without explicit metadata are still useful, but only within a
# bounded inheritance policy.  Main-story pages and enemy/world pages are
# global context; event/mail pages must explicitly mention the selected role.
_GLOBAL_CONTEXT_SOURCE_TYPES = {"main_story", "enemy_lore", "exploration_note"}
_EXPLICIT_SHARED_SOURCE_TYPES = {"event_lore", "random_event", "special_mail"}


def _shared_document_for_character(document: dict[str, Any], character: Any) -> bool:
    if _document_character_ids(document):
        return False
    source_type = str(document.get("source_type") or "")
    if source_type in _GLOBAL_CONTEXT_SOURCE_TYPES:
        return True
    return source_type in _EXPLICIT_SHARED_SOURCE_TYPES and _document_mentions_character(document, character)


def _candidate_matches(
    candidate: dict[str, Any],
    character: Any,
    direct_document_ids: set[str],
) -> bool:
    aliases = _character_aliases(character)
    entities = {_compact(candidate.get("subject")), _compact(candidate.get("object"))}
    if aliases.intersection(entities):
        return True
    evidence_ids = {str(value) for value in candidate.get("evidence_document_ids", []) or []}
    return bool(evidence_ids.intersection(direct_document_ids))


def _candidate_scope(candidate: dict[str, Any], documents_by_id: dict[str, dict[str, Any]]) -> str:
    scopes = {
        _document_scope(documents_by_id[document_id])
        for document_id in candidate.get("evidence_document_ids", []) or []
        if document_id in documents_by_id
    }
    if not scopes:
        return source_layer(candidate.get("source_type"))
    if len(scopes) == 1:
        return next(iter(scopes))
    # Keep the response conservative when a relation spans a stable source and
    # one or more contextual sources.
    if "stable" in scopes:
        return "stable"
    if "costume_specific" in scopes:
        return "costume_specific"
    if "situational" in scopes:
        return "situational"
    return "general"


def _relation_projection(
    candidate: dict[str, Any],
    scope: str,
    status: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "subject": candidate.get("subject"),
        "relation_type": candidate.get("relation_type"),
        "object": candidate.get("object"),
        "source_type": candidate.get("source_type"),
        "evidence_document_ids": list(candidate.get("evidence_document_ids") or []),
        "evidence_quote": candidate.get("evidence_quote", ""),
        "rationale": candidate.get("rationale", ""),
        "review_status": candidate.get("review_status", "pending_review"),
        "mvp_status": status,
        "narrative_scope": scope,
        "policy": "临时关系仅用于带引文的上下文提示，不能成为稳定人格事实或正式图谱边。"
        if status == "provisional"
        else "已人工批准的关系仍受其叙事范围限制。",
    }


def build_mvp_views() -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    if not documents:
        raise RuntimeError("Lakehouse documents are missing. Run python -m pipelines.build_lakehouse first.")
    documents_by_id = {str(document["document_id"]): document for document in documents if document.get("document_id")}
    personas = _load_personas()
    dialogue_profiles = _load_dialogue_profiles()
    # Review artifacts live beside the lakehouse.  Use the explicit path so
    # the pipeline remains obvious and safe rather than recursively scanning
    # runtime/.
    candidate_path = RUNTIME_ROOT / "review" / "narrative_relation_candidates.jsonl"
    candidates = (
        [json.loads(line) for line in candidate_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if candidate_path.exists()
        else []
    )

    global_documents = [
        document
        for document in documents
        if not _document_character_ids(document)
        and str(document.get("source_type") or "") in _GLOBAL_CONTEXT_SOURCE_TYPES
    ]
    global_document_ids_by_layer: dict[str, list[str]] = {layer: [] for layer in LAYER_ORDER}
    for document in global_documents:
        global_document_ids_by_layer.setdefault(_document_scope(document), []).append(document["document_id"])

    views: list[dict[str, Any]] = []
    per_character_counts: dict[str, dict[str, int]] = {}
    total_provisional = 0
    for character in MVP_CHARACTERS:
        direct_documents = [
            document for document in documents if _document_link_kind(document, character.character_id) == "direct"
        ]
        linked_documents = [
            document for document in documents if _document_link_kind(document, character.character_id) == "linked"
        ]
        shared_documents = [
            document for document in documents if _shared_document_for_character(document, character)
        ]
        direct_ids = {document["document_id"] for document in direct_documents}
        linked_ids = {document["document_id"] for document in linked_documents}
        direct_by_layer: dict[str, list[str]] = {layer: [] for layer in LAYER_ORDER}
        for document in direct_documents:
            direct_by_layer.setdefault(_document_scope(document), []).append(document["document_id"])
        linked_by_layer: dict[str, list[str]] = {layer: [] for layer in LAYER_ORDER}
        for document in linked_documents:
            linked_by_layer.setdefault(_document_scope(document), []).append(document["document_id"])
        shared_by_layer: dict[str, list[str]] = {layer: [] for layer in LAYER_ORDER}
        for document in shared_documents:
            shared_by_layer.setdefault(_document_scope(document), []).append(document["document_id"])
        retrieval_by_layer = {
            layer: list(
                dict.fromkeys(
                    direct_by_layer.get(layer, [])
                    + linked_by_layer.get(layer, [])
                    + shared_by_layer.get(layer, [])
                    + global_document_ids_by_layer.get(layer, [])
                )
            )
            for layer in LAYER_ORDER
        }
        retrieval_ids = list(dict.fromkeys(value for values in retrieval_by_layer.values() for value in values))
        document_origins = {
            document_id: "direct" for document_id in direct_ids
        }
        document_origins.update({document_id: "linked" for document_id in linked_ids})
        document_origins.update(
            {
                document_id: "shared_context"
                for document_id in (set(retrieval_ids) - set(document_origins))
            }
        )

        relation_projections: list[dict[str, Any]] = []
        for candidate in candidates:
            review_status = str(candidate.get("review_status") or "pending_review")
            if review_status == "rejected" or not _candidate_matches(candidate, character, direct_ids):
                continue
            mvp_status = "verified" if review_status == "approved" else "provisional"
            relation_projections.append(
                _relation_projection(candidate, _candidate_scope(candidate, documents_by_id), mvp_status)
            )
        relation_projections.sort(key=lambda item: str(item.get("candidate_id") or ""))
        total_provisional += sum(item["mvp_status"] == "provisional" for item in relation_projections)

        source_counts = collections.Counter(document.get("source_type") for document in direct_documents)
        layer_counts = {layer: len(retrieval_by_layer.get(layer, [])) for layer in LAYER_ORDER}
        profile_evidence = dialogue_profiles.get(character.character_id, {})
        direct_count = len(direct_documents)
        coverage_level = "limited" if direct_count < 80 else "standard" if direct_count < 180 else "full"
        coverage = {
            "level": coverage_level,
            "label": {
                "full": "资料覆盖完整",
                "standard": "资料覆盖标准",
                "limited": "资料覆盖有限",
            }[coverage_level],
            "direct_document_count": direct_count,
            "linked_document_count": len(linked_documents),
            "shared_context_document_count": len(shared_documents),
            "global_context_document_count": len(global_documents),
            "direct_source_counts": dict(sorted(source_counts.items())),
            "dialogue_line_count": int(profile_evidence.get("dialogue_line_count") or 0),
            "address_term_count": len(profile_evidence.get("address_terms") or []),
            "voice_evidence_count": len(profile_evidence.get("emotion_patterns") or [])
            + len(profile_evidence.get("catchphrases") or []),
            "relationship_evidence_count": len(profile_evidence.get("analyst_interaction") or []),
            "inheritance_policy": "direct_then_linked_then_explicit_shared_then_global_context; no cross-character persona copying",
        }
        per_character_counts[character.character_id] = {
            "direct_documents": direct_count,
            "linked_documents": len(linked_documents),
            "shared_context_documents": len(shared_documents),
            "global_context_documents": len(global_documents),
            "retrieval_documents": len(retrieval_ids),
            "provisional_relations": sum(item["mvp_status"] == "provisional" for item in relation_projections),
            "verified_relations": sum(item["mvp_status"] == "verified" for item in relation_projections),
        }
        persona = personas.get(character.character_id, {})
        dialogue_profile = dialogue_profiles.get(character.character_id, {})
        views.append(
            {
                "view_id": f"mvp_view_{character.character_id}",
                "character_id": character.character_id,
                "character_name": character.display_name,
                "source_character_name": character.source_name,
                "aliases": list(character.aliases),
                "selector_enabled": character.selector_enabled,
                "persona_profile_id": persona.get("profile_id"),
                "persona_review_status": persona.get("review_status", "missing"),
                "dialogue_style_profile_id": dialogue_profile.get("profile_id"),
                "dialogue_style_profile_status": dialogue_profile.get("review_status", "missing"),
                "active_traits": list(persona.get("active_traits") or []),
                "source_counts_direct": dict(sorted(source_counts.items())),
                "document_counts_by_layer": layer_counts,
                "direct_document_ids_by_layer": direct_by_layer,
                "linked_document_ids_by_layer": linked_by_layer,
                "shared_context_document_ids_by_layer": shared_by_layer,
                "global_context_document_ids_by_layer": global_document_ids_by_layer,
                "retrieval_document_ids_by_layer": retrieval_by_layer,
                "retrieval_document_ids": retrieval_ids,
                "document_origins": document_origins,
                "coverage": coverage,
                "provisional_relations": relation_projections,
                "data_policy": {
                    "stable_facts": "stable",
                    "situational_background": "situational",
                    "costume_context": "costume_specific",
                    "unreviewed_relations": "provisional_only",
                    "analyst_identity": "分析员",
                },
                "generated_at": utc_now(),
            }
        )

    output = ensure_runtime("mvp")
    view_path = output / "character_views.jsonl"
    question_path = output / "question_bank.json"
    write_jsonl(view_path, views)
    write_json(
        question_path,
        {
            "version": "mvp-22.1",
            "registry_version": MVP_REGISTRY_VERSION,
            "characters": [
                {
                    "character_id": character.character_id,
                    "character_name": character.display_name,
                    "source_name": character.source_name,
                    "aliases": list(character.aliases),
                    "selector_enabled": character.selector_enabled,
                }
                for character in MVP_CHARACTERS
            ],
            "questions": question_bank(),
            "feedback_options": list(FEEDBACK_OPTIONS),
            "layer_policy": layer_policy(),
            "policy": "问题库和视图是内部测试工件；不改变正式图谱、人格特质或 Data/ 原始资料。",
            "generated_at": utc_now(),
        },
    )
    report = {
        "stage": "MVP",
        "job": "build_mvp_views",
        "generated_at": utc_now(),
        "selected_characters": len(views),
        "registry_version": MVP_REGISTRY_VERSION,
        "question_count": len(question_bank()),
        "document_count": len(documents),
        "dialogue_style_profiles": len(dialogue_profiles),
        "global_context_documents": sum(len(values) for values in global_document_ids_by_layer.values()),
        "provisional_relation_count": total_provisional,
        "per_character": per_character_counts,
        "outputs": {"character_views": str(view_path), "question_bank": str(question_path)},
        "data_write_policy": "Data/、正式图谱、关系审批和 persona active_traits 均未修改。",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_mvp_views.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build_mvp_views(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
