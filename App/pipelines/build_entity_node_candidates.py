"""Build a human-only queue for graph nodes missing from relation endpoints.

The relation extractor is intentionally conservative about identity mapping.  A
literal relation such as ``晴 VISITS_LOCATION 汉诺塔`` cannot be approved until
``汉诺塔`` exists as a graph node.  This pipeline discovers only endpoint types
that are unambiguous from the relation schema (locations and events), records
their source evidence, and creates *candidates* below ``App/runtime/review``.

It never creates graph nodes by itself and never changes relation-candidate
states.  A human must explicitly approve every proposed entity node through the
application review API before it becomes available for relationship mapping.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .common import RUNTIME_ROOT, read_jsonl, stable_id, utc_now, write_json, write_jsonl


ENTITY_CANDIDATE_FILENAME = "entity_node_candidates.jsonl"
APPROVED_ENTITY_NODE_FILENAME = "approved_entity_nodes.jsonl"
REPORT_FILENAME = "build_entity_node_candidates.json"

# Only these object endpoint types can be safely inferred from a relationship
# type alone.  Actor and item endpoints have multiple legitimate node types;
# they remain in the ordinary relation queue until a human has an exact mapping.
UNAMBIGUOUS_OBJECT_NODE_TYPES = {
    "VISITS_LOCATION": "location",
    "PARTICIPATES_IN_EVENT": "event",
}
ACTOR_NODE_TYPES = {"character", "sender", "enemy"}
_GENERIC_EVENT_LABELS = {
    "约会",
    "比赛",
    "训练",
    "调查",
    "公审",
    "合影",
    "过生日",
    "看电影",
    "任务",
    "行动",
    "战斗",
    "活动",
    "旅行",
    "旅程",
    "庆典",
    "派对",
    "聚会",
    "购物",
    "休息",
    "练习",
    "测试",
    "演习",
    "聊天",
    "吃饭",
    "散步",
    "护送任务",
    "最后的战斗",
    "加入海姆达尔",
}
def _read_optional_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(read_jsonl(path)) if path.exists() else []


def _normalize(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-—_·•,，。！？!?：:;；'\"“”‘’()（）\[\]【】]+", "", normalized)


_NORMALIZED_GENERIC_EVENT_LABELS = {_normalize(label) for label in _GENERIC_EVENT_LABELS}


def _node_name_index(nodes: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        normalized_name = _normalize(node.get("name"))
        if normalized_name:
            index[normalized_name].append(node)
    return index


def _candidate_evidence(
    candidate: dict[str, Any], documents: dict[str, dict[str, Any]], entity_name: str
) -> tuple[list[str], list[str]]:
    """Return only evidence documents that literally contain the entity name."""
    normalized_entity = _normalize(entity_name)
    document_ids: list[str] = []
    page_ids: list[str] = []
    for document_id in candidate.get("evidence_document_ids", []):
        document = documents.get(str(document_id))
        if document is None or normalized_entity not in _normalize(document.get("text")):
            continue
        document_ids.append(str(document_id))
        page_id = str(document.get("page_id") or "")
        if page_id:
            page_ids.append(page_id)
    return list(dict.fromkeys(document_ids)), list(dict.fromkeys(page_ids))


def _is_specific_event_label(entity_name: str) -> bool:
    """Keep generic activities out of the reusable event-node vocabulary."""
    normalized_name = _normalize(entity_name)
    if len(normalized_name) < 3 or normalized_name in _NORMALIZED_GENERIC_EVENT_LABELS:
        return False
    return not bool(re.fullmatch(r"[0-9０-９]+", normalized_name))


def _candidate_row(
    node_type: str,
    normalized_name: str,
    names: Counter[str],
    relation_candidates: list[dict[str, Any]],
    evidence_document_ids: list[str],
    evidence_page_ids: list[str],
) -> dict[str, Any]:
    entity_name = sorted(names, key=lambda value: (-names[value], value))[0]
    candidate_id = stable_id("entity_node_candidate", node_type, normalized_name, prefix="entity_node_candidate_")
    proposed_node_id = f"{node_type}:review_{stable_id(node_type, normalized_name)}"
    examples = []
    for candidate in sorted(relation_candidates, key=lambda row: str(row.get("candidate_id") or ""))[:5]:
        examples.append(
            {
                "relation_candidate_id": candidate.get("candidate_id"),
                "subject": candidate.get("subject"),
                "relation_type": candidate.get("relation_type"),
                "object": candidate.get("object"),
                "source_type": candidate.get("source_type"),
                "evidence_quote": candidate.get("evidence_quote"),
            }
        )
    return {
        "entity_candidate_id": candidate_id,
        "entity_name": entity_name,
        "normalized_name": normalized_name,
        "proposed_node_type": node_type,
        "proposed_node_id": proposed_node_id,
        "review_status": "pending_review",
        "relation_candidate_ids": sorted(
            {str(candidate.get("candidate_id")) for candidate in relation_candidates if candidate.get("candidate_id")}
        ),
        "relation_types": sorted(
            {str(candidate.get("relation_type") or "") for candidate in relation_candidates if candidate.get("relation_type")}
        ),
        "source_types": sorted(
            {str(candidate.get("source_type") or "unknown") for candidate in relation_candidates}
        ),
        "evidence_document_ids": evidence_document_ids,
        "evidence_page_ids": evidence_page_ids,
        "evidence_examples": examples,
        "origin": "deterministic_relation_endpoint_discovery",
        "policy": (
            "This is a non-binding node candidate. It cannot create a graph node, map aliases, "
            "or approve any relationship without an explicit human decision."
        ),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def discover_entity_node_candidates(
    candidates: Iterable[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    nodes: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Discover safe location/event node candidates from pending relations."""
    node_index = _node_name_index(nodes)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    skipped: Counter[str] = Counter()

    for candidate in candidates:
        if candidate.get("review_status") != "pending_review":
            skipped["not_pending_review"] += 1
            continue
        relation_type = str(candidate.get("relation_type") or "").strip().upper()
        node_type = UNAMBIGUOUS_OBJECT_NODE_TYPES.get(relation_type)
        if node_type is None:
            skipped["ambiguous_or_unsupported_endpoint_type"] += 1
            continue

        subject_matches = [
            node
            for node in node_index.get(_normalize(candidate.get("subject")), [])
            if node.get("node_type") in ACTOR_NODE_TYPES
        ]
        if len(subject_matches) != 1:
            skipped["subject_not_exact_unique"] += 1
            continue

        entity_name = str(candidate.get("object") or "").strip()
        normalized_name = _normalize(entity_name)
        if not normalized_name:
            skipped["missing_object_name"] += 1
            continue
        if node_type == "event" and not _is_specific_event_label(entity_name):
            skipped["generic_event_label"] += 1
            continue
        if any(node.get("node_type") == node_type for node in node_index.get(normalized_name, [])):
            skipped["object_node_already_exists"] += 1
            continue

        quote = str(candidate.get("evidence_quote") or "")
        if normalized_name not in _normalize(quote):
            skipped["object_not_literal_in_evidence_quote"] += 1
            continue
        evidence_document_ids, evidence_page_ids = _candidate_evidence(candidate, documents, entity_name)
        if not evidence_document_ids:
            skipped["object_not_literal_in_source_document"] += 1
            continue

        key = (node_type, normalized_name)
        group = grouped.setdefault(
            key,
            {
                "names": Counter(),
                "relation_candidates": [],
                "evidence_document_ids": [],
                "evidence_page_ids": [],
            },
        )
        group["names"][entity_name] += 1
        group["relation_candidates"].append(candidate)
        group["evidence_document_ids"].extend(evidence_document_ids)
        group["evidence_page_ids"].extend(evidence_page_ids)

    rows = [
        _candidate_row(
            node_type,
            normalized_name,
            group["names"],
            group["relation_candidates"],
            list(dict.fromkeys(group["evidence_document_ids"])),
            list(dict.fromkeys(group["evidence_page_ids"])),
        )
        for (node_type, normalized_name), group in grouped.items()
    ]
    rows.sort(key=lambda row: (-len(row["relation_candidate_ids"]), row["proposed_node_type"], row["entity_name"]))
    return rows, skipped


def build_entity_node_candidates(dry_run: bool = False) -> dict[str, Any]:
    """Refresh the non-destructive missing-entity candidate queue."""
    review_root = RUNTIME_ROOT / "review"
    candidates_path = review_root / "narrative_relation_candidates.jsonl"
    documents_path = RUNTIME_ROOT / "lakehouse" / "documents.jsonl"
    graph_nodes_path = RUNTIME_ROOT / "graph" / "nodes.jsonl"
    approved_nodes_path = review_root / APPROVED_ENTITY_NODE_FILENAME
    output_path = review_root / ENTITY_CANDIDATE_FILENAME

    if not candidates_path.exists() or not documents_path.exists() or not graph_nodes_path.exists():
        raise FileNotFoundError(
            "Missing runtime artifacts. Build the lakehouse and deterministic graph before discovering entity candidates."
        )
    relation_candidates = _read_optional_jsonl(candidates_path)
    documents = {row["document_id"]: row for row in _read_optional_jsonl(documents_path) if row.get("document_id")}
    nodes = _read_optional_jsonl(graph_nodes_path) + [
        row for row in _read_optional_jsonl(approved_nodes_path) if row.get("review_status") == "verified"
    ]
    discovered, skipped = discover_entity_node_candidates(relation_candidates, documents, nodes)
    existing = {row.get("entity_candidate_id"): row for row in _read_optional_jsonl(output_path) if row.get("entity_candidate_id")}

    rows: list[dict[str, Any]] = []
    for row in discovered:
        prior = existing.pop(row["entity_candidate_id"], None)
        if prior is not None:
            for key in (
                "review_status",
                "reviewer_id",
                "review_note",
                "reviewed_at",
                "approved_node_id",
                "created_at",
            ):
                if key in prior:
                    row[key] = prior[key]
            row["updated_at"] = utc_now()
        rows.append(row)

    # Retain reviewed history, but discard stale pending candidates when the
    # deterministic discovery policy becomes stricter or the source changes.
    # Pending records are regenerated artifacts, not human decisions.
    retired_pending = sum(row.get("review_status") == "pending_review" for row in existing.values())
    rows.extend(row for row in existing.values() if row.get("review_status") in {"approved", "rejected"})
    rows.sort(
        key=lambda row: (
            {"pending_review": 0, "approved": 1, "rejected": 2}.get(str(row.get("review_status")), 3),
            -len(row.get("relation_candidate_ids", [])),
            str(row.get("proposed_node_type") or ""),
            str(row.get("entity_name") or ""),
        )
    )

    report = {
        "stage": "C",
        "job": "build_entity_node_candidates",
        "generated_at": utc_now(),
        "relation_candidates_scanned": len(relation_candidates),
        "discoverable_candidates": len(discovered),
        "queue_rows": len(rows),
        "pending_review": sum(row.get("review_status") == "pending_review" for row in rows),
        "retired_stale_pending": retired_pending,
        "by_node_type": dict(Counter(str(row.get("proposed_node_type") or "unknown") for row in rows)),
        "skipped": dict(sorted(skipped.items())),
        "output": str(output_path),
        "policy": (
            "Only unambiguous location/event endpoints with literal source evidence are queued. "
            "The pipeline never creates a graph node or approves a relationship."
        ),
    }
    if not dry_run:
        write_jsonl(output_path, rows)
        write_json(RUNTIME_ROOT / "reports" / REPORT_FILENAME, report)
    else:
        report["policy"] += " Dry run only; no review artifact was written."
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report candidates without writing review artifacts.")
    args = parser.parse_args()
    print(json.dumps(build_entity_node_candidates(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
