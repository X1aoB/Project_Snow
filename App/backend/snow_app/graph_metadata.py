"""Pure, non-mutating metadata helpers for reviewed narrative graph edges."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


VALID_NARRATIVE_SCOPES = frozenset({"stable", "situational", "costume_specific", "unknown"})
SCENE_BOUND_SOURCE_TYPES = frozenset(
    {
        "special_mail",
        "random_event",
        "event_lore",
        "birthday_content",
        "character_costume",
        "character_costumes",
    }
)


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable):
        values = [str(item) for item in value]
    else:
        values = []
    return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def narrative_scope(relation_type: str, source_types: Iterable[str]) -> str:
    """Classify retrieval scope; this is metadata, never an approval decision."""
    types = {str(source_type or "") for source_type in source_types}
    if types & {"character_costume", "character_costumes"}:
        return "costume_specific"
    if relation_type in {"PARTICIPATES_IN_EVENT", "VISITS_LOCATION"} or types & SCENE_BOUND_SOURCE_TYPES:
        return "situational"
    return "stable"


def hydrate_human_approved_edge(
    edge: dict[str, Any],
    candidates_by_id: dict[str, dict[str, Any]],
    documents_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a display/export copy with traceable metadata for legacy review edges.

    The function intentionally never writes an artifact and does not change an
    approval, reviewer, mapping, or evidence list.  It only reconstructs fields
    added after older human decisions were already recorded.
    """
    hydrated = dict(edge)
    candidate = candidates_by_id.get(str(edge.get("candidate_id") or ""))
    source_types = _text_values(edge.get("source_types"))
    if candidate is not None:
        source_types = list(
            dict.fromkeys(
                [
                    *source_types,
                    *_text_values(candidate.get("source_type")),
                    *(
                        source_type
                        for document_id in candidate.get("evidence_document_ids", [])
                        if (document := documents_by_id.get(str(document_id))) is not None
                        for source_type in _text_values(document.get("source_type"))
                    ),
                ]
            )
        )
    hydrated["source_types"] = source_types

    existing_scope = str(edge.get("narrative_scope") or "")
    if existing_scope in VALID_NARRATIVE_SCOPES:
        hydrated["narrative_scope"] = existing_scope
    elif source_types or str(edge.get("relation_type") or "") in {"PARTICIPATES_IN_EVENT", "VISITS_LOCATION"}:
        hydrated["narrative_scope"] = narrative_scope(str(edge.get("relation_type") or ""), source_types)
    else:
        hydrated["narrative_scope"] = "unknown"
    return hydrated
