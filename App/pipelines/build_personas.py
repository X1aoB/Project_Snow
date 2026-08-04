"""Create evidence-backed persona inventories and review jobs.

This job does not invent personality traits. It prepares reviewed-source evidence
for later AI-assisted annotation and human approval.
"""

from __future__ import annotations

import argparse
import collections
import json
from typing import Any

from .common import dialogue_characters, RUNTIME_ROOT, ensure_runtime, load_runtime_jsonl, utc_now, write_json, write_jsonl


EVIDENCE_BUCKETS = {
    "identity": {"character_profile", "character_armor", "main_story"},
    "style": {"character_voice", "character_story", "affinity_story", "special_mail", "random_event"},
    "preferences": {"character_story", "affinity_story", "special_mail", "furniture_lore", "item_lore"},
    "relationship": {"affinity_story", "special_mail", "character_story", "random_event", "character_costume"},
    "world_context": {"main_story", "exploration_note", "event_lore", "enemy_lore"},
}


def _character_mentions(document: dict[str, Any]) -> list[tuple[str, str]]:
    metadata = document.get("metadata", {})
    result: list[tuple[str, str]] = []
    if metadata.get("character_id"):
        result.append((metadata["character_id"], metadata.get("character_name") or "未知角色"))
    ids = metadata.get("related_character_ids", []) or []
    names = metadata.get("related_character_names", []) or []
    for index, character_id in enumerate(ids):
        if character_id:
            result.append((character_id, names[index] if index < len(names) else "未知角色"))
    return list(dict.fromkeys(result))


def _evidence_ids(documents: list[dict[str, Any]], allowed_types: set[str], limit: int = 32) -> list[str]:
    ranked = sorted(
        (document for document in documents if document["source_type"] in allowed_types),
        key=lambda document: document["metadata"].get("source_priority", 0),
        reverse=True,
    )
    return [document["document_id"] for document in ranked[:limit]]


def build_personas() -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    if not documents:
        raise RuntimeError("Lakehouse documents are missing. Run build_lakehouse first.")
    by_character: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    names: dict[str, str] = {}
    dialogue_registry = dialogue_characters()
    for document in documents:
        for character_id, character_name in _character_mentions(document):
            if character_id not in dialogue_registry:
                continue
            by_character[character_id].append(document)
            if character_name != "未知角色":
                names[character_id] = dialogue_registry[character_id]

    profiles: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for character_id, character_documents in sorted(by_character.items(), key=lambda item: names.get(item[0], item[0])):
        evidence = {
            bucket: _evidence_ids(character_documents, allowed_types)
            for bucket, allowed_types in EVIDENCE_BUCKETS.items()
        }
        source_counts = dict(collections.Counter(document["source_type"] for document in character_documents))
        profile_id = f"persona_{character_id}"
        profiles.append(
            {
                "profile_id": profile_id,
                "character_id": character_id,
                "character_name": names.get(character_id, "未知角色"),
                "relationship_invariant": {
                    "user_role": "分析员",
                    "policy": "聊天模式和助手模式均保持角色对分析员的既有叙事关系。",
                    "status": "verified_project_policy",
                },
                "evidence": evidence,
                "source_counts": source_counts,
                "active_traits": [],
                "review_status": "evidence_ready",
                "created_at": utc_now(),
                "policy_version": "1.0.0",
            }
        )
        candidate_sources = list(dict.fromkeys(evidence["style"] + evidence["preferences"] + evidence["relationship"]))
        if candidate_sources:
            jobs.append(
                {
                    "job_id": f"persona_extract_{character_id}",
                    "kind": "persona_trait_extraction",
                    "character_id": character_id,
                    "character_name": names.get(character_id, "未知角色"),
                    "evidence_document_ids": candidate_sources[:80],
                    "allowed_trait_types": ["speech_style", "catchphrase", "preference", "dislike", "value", "relationship_tendency"],
                    "status": "queued",
                    "requires_human_review": True,
                    "created_at": utc_now(),
                }
            )

    output = ensure_runtime("personas")
    write_jsonl(output / "persona_profiles.jsonl", profiles)
    write_jsonl(output / "persona_extraction_jobs.jsonl", jobs)
    write_jsonl(output / "persona_trait_candidates.jsonl", [])
    report = {
        "stage": "B",
        "job": "build_personas",
        "generated_at": utc_now(),
        "profiles": len(profiles),
        "queued_annotation_jobs": len(jobs),
        "policy": "No trait is active until evidence-backed human review completes.",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_personas.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build_personas(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
