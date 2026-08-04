"""Build the portable, deterministic C-stage knowledge graph.

Only manifest-explicit relationships are marked verified. Narrative extraction is
represented as a separate review queue and cannot alter this graph automatically.
"""

from __future__ import annotations

import argparse
import collections
import json
from dataclasses import dataclass, field
from typing import Any

from .common import (
    CHARACTER_AUTHORITY_SOURCE_TYPES,
    INDEX_SOURCE_TYPES,
    MANIFEST_ROOT,
    RUNTIME_ROOT,
    as_text,
    canonical_character_identity,
    ensure_runtime,
    iter_index_records,
    known_characters,
    load_runtime_jsonl,
    normalize_character_references,
    read_jsonl,
    stable_id,
    utc_now,
    write_json,
    write_jsonl,
)


# Keep a single provider request beneath the gateway timeout seen with long
# main-story pages, while retaining every source chunk as independent evidence.
RELATION_JOB_MAX_EVIDENCE_CHARS = 4_000


def node_id(kind: str, value: Any) -> str:
    return f"{kind}:{value}"


def value_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@dataclass
class GraphAccumulator:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)

    def add_node(self, identifier: str, kind: str, name: str, **attributes: Any) -> str:
        existing = self.nodes.get(identifier)
        if existing is None:
            self.nodes[identifier] = {
                "node_id": identifier,
                "node_type": kind,
                "name": name or identifier,
                "attributes": {key: value for key, value in attributes.items() if value not in (None, "", [], {})},
                "created_at": utc_now(),
            }
        else:
            existing["attributes"].update(
                {key: value for key, value in attributes.items() if value not in (None, "", [], {})}
            )
            if name and existing["name"] == existing["node_id"]:
                existing["name"] = name
        return identifier

    def add_edge(
        self,
        from_id: str,
        relation_type: str,
        to_id: str,
        page_id: str | None,
        source_manifest: str,
        confidence: str = "high",
    ) -> None:
        key = (from_id, relation_type, to_id)
        edge = self.edges.get(key)
        if edge is None:
            edge = {
                "edge_id": stable_id(from_id, relation_type, to_id, prefix="edge_"),
                "from_id": from_id,
                "relation_type": relation_type,
                "to_id": to_id,
                "evidence_page_ids": [],
                "source_manifests": [],
                "confidence": confidence,
                "review_status": "verified",
                "created_at": utc_now(),
            }
            self.edges[key] = edge
        if page_id and page_id not in edge["evidence_page_ids"]:
            edge["evidence_page_ids"].append(page_id)
        if source_manifest not in edge["source_manifests"]:
            edge["source_manifests"].append(source_manifest)


def _record_title(record: dict[str, Any]) -> str:
    for field in (
        "canonical_story_label",
        "canonical_profile_label",
        "canonical_mail_label",
        "story_title",
        "mail_subject",
        "event_name",
        "item_name",
        "weapon_name",
        "furniture_name",
        "attachment_name",
        "enemy_name",
        "squad_name",
        "character_name",
    ):
        if as_text(record.get(field)):
            return as_text(record[field])
    return as_text(record.get("page_id")) or "未命名页面"


def _entity_reference(graph: GraphAccumulator, kind: str, entity_id: str | None, name: str | None) -> str | None:
    if not entity_id:
        return None
    identifier = node_id(kind, entity_id)
    return graph.add_node(identifier, kind, name or entity_id, source_id=entity_id)


def _known_character_maps(records: list[tuple[str, str, dict[str, Any]]]) -> tuple[dict[str, str], dict[str, str]]:
    del records
    by_id = known_characters()
    by_name = {name: identifier for identifier, name in by_id.items()}
    return by_id, by_name


def _add_related_character_edges(
    graph: GraphAccumulator,
    source_node: str,
    record: dict[str, Any],
    page_id: str,
    manifest_name: str,
    char_names: dict[str, str],
) -> None:
    # Do not consume generic item `character_ids`: in some item records these
    # are mail-page IDs with mail subjects as names. Only explicit relations
    # and owner fields are safe graph inputs.
    ids = value_list(record.get("related_character_ids") or record.get("owner_character_ids"))
    names = value_list(record.get("related_character_names") or record.get("owner_characters"))
    for index, character_id in enumerate(ids):
        if character_id not in char_names:
            continue
        character = _entity_reference(
            graph,
            "character",
            character_id,
            names[index] if index < len(names) else char_names.get(character_id),
        )
        if character:
            graph.add_edge(source_node, "RELATED_TO_CHARACTER", character, page_id, manifest_name)


def build_graph() -> dict[str, Any]:
    records = [(source_type, path.name, record) for source_type, path, record in iter_index_records()]
    char_names, character_ids_by_name = _known_character_maps(records)
    graph = GraphAccumulator()

    for source_type, manifest_name, record in records:
        record = normalize_character_references(record)
        page_id = str(record.get("page_id") or stable_id(record.get("canonical_url"), prefix="page_"))
        page = graph.add_node(
            node_id("page", page_id),
            "page",
            _record_title(record),
            source_type=source_type,
            canonical_url=record.get("canonical_url"),
            local_path=record.get("local_path"),
        )

        character = _entity_reference(graph, "character", record.get("character_id"), record.get("character_name"))
        armor = _entity_reference(graph, "armor", record.get("armor_id"), record.get("armor_name"))
        costume = _entity_reference(graph, "costume", record.get("costume_id"), record.get("costume_name"))
        if character:
            graph.add_edge(page, "PAGE_DESCRIBES_CHARACTER", character, page_id, manifest_name)
        if armor:
            graph.add_edge(page, "PAGE_DESCRIBES_ARMOR", armor, page_id, manifest_name)
        if costume:
            graph.add_edge(page, "PAGE_DESCRIBES_COSTUME", costume, page_id, manifest_name)
        if character and armor:
            graph.add_edge(character, "HAS_ARMOR", armor, page_id, manifest_name)
        if character and costume:
            graph.add_edge(character, "HAS_COSTUME", costume, page_id, manifest_name)
        if armor and costume:
            graph.add_edge(armor, "HAS_COSTUME", costume, page_id, manifest_name)

        if source_type == "character_story":
            story = _entity_reference(graph, "story", record.get("story_id") or record.get("page_id"), record.get("story_title"))
            if story:
                graph.add_edge(page, "PAGE_DESCRIBES_STORY", story, page_id, manifest_name)
                if character:
                    graph.add_edge(character, "HAS_PERSONAL_STORY", story, page_id, manifest_name)
                if armor:
                    graph.add_edge(armor, "HAS_PERSONAL_STORY", story, page_id, manifest_name)
        elif source_type == "affinity_story":
            story = _entity_reference(
                graph, "affinity_story", record.get("affinity_story_id") or record.get("page_id"), record.get("story_title")
            )
            if story:
                graph.add_edge(page, "PAGE_DESCRIBES_AFFINITY_STORY", story, page_id, manifest_name)
                if character:
                    graph.add_edge(character, "HAS_AFFINITY_STORY", story, page_id, manifest_name)
        elif source_type == "random_event":
            event = _entity_reference(graph, "random_event", record.get("random_event_id") or record.get("page_id"), record.get("event_name"))
            if event:
                graph.add_edge(page, "PAGE_DESCRIBES_RANDOM_EVENT", event, page_id, manifest_name)
                if character:
                    graph.add_edge(character, "HAS_RANDOM_EVENT", event, page_id, manifest_name)
        elif source_type == "character_voice":
            voice = _entity_reference(graph, "voice", record.get("voice_id") or record.get("page_id"), record.get("canonical_voice_label"))
            if voice:
                graph.add_edge(page, "PAGE_DESCRIBES_VOICE", voice, page_id, manifest_name)
                if character:
                    graph.add_edge(character, "HAS_VOICE", voice, page_id, manifest_name)
        elif source_type == "special_mail":
            mail = _entity_reference(graph, "mail", record.get("mail_id") or record.get("page_id"), record.get("mail_subject"))
            if mail:
                graph.add_edge(page, "PAGE_DESCRIBES_MAIL", mail, page_id, manifest_name)
                for sender_name in value_list(record.get("sender_names") or record.get("sender_name")):
                    sender_id, canonical_sender_name = canonical_character_identity(None, sender_name)
                    sender_id = sender_id if sender_id in char_names else character_ids_by_name.get(sender_name)
                    sender_name = canonical_sender_name or sender_name
                    sender = _entity_reference(graph, "character", sender_id, sender_name) if sender_id else graph.add_node(
                        node_id("sender", sender_name), "sender", sender_name
                    )
                    graph.add_edge(sender, "SENT_MAIL", mail, page_id, manifest_name)
                for item in value_list(record.get("attachments")):
                    item_name = item.get("name") if isinstance(item, dict) else as_text(item)
                    if item_name:
                        attachment = graph.add_node(node_id("item_name", item_name), "item", item_name)
                        graph.add_edge(mail, "HAS_ATTACHMENT", attachment, page_id, manifest_name)
        elif source_type == "weapon_lore":
            weapon = _entity_reference(graph, "weapon", record.get("weapon_id") or record.get("page_id"), record.get("weapon_name"))
            if weapon:
                graph.add_edge(page, "PAGE_DESCRIBES_WEAPON", weapon, page_id, manifest_name)
                for index, recommended_id in enumerate(value_list(record.get("recommended_character_ids"))):
                    recommended = _entity_reference(
                        graph,
                        "character",
                        recommended_id,
                        value_list(record.get("recommended_character_names"))[index]
                        if index < len(value_list(record.get("recommended_character_names")))
                        else char_names.get(recommended_id),
                    )
                    if recommended:
                        graph.add_edge(weapon, "RECOMMENDED_FOR_CHARACTER", recommended, page_id, manifest_name)
                for armor_id in value_list(record.get("recommended_armor_ids")):
                    recommended_armor = _entity_reference(graph, "armor", armor_id, None)
                    if recommended_armor:
                        graph.add_edge(weapon, "RECOMMENDED_FOR_ARMOR", recommended_armor, page_id, manifest_name)
        elif source_type == "weapon_attachment":
            attachment = _entity_reference(
                graph, "weapon_attachment", record.get("attachment_id") or record.get("page_id"), record.get("attachment_name")
            )
            weapon = _entity_reference(graph, "weapon", record.get("weapon_id"), record.get("weapon_name"))
            if attachment:
                graph.add_edge(page, "PAGE_DESCRIBES_WEAPON_ATTACHMENT", attachment, page_id, manifest_name)
            if attachment and weapon:
                graph.add_edge(attachment, "ATTACHES_TO_WEAPON", weapon, page_id, manifest_name)
        elif source_type == "item_lore":
            item = _entity_reference(graph, "item", record.get("item_id") or record.get("page_id"), record.get("item_name"))
            if item:
                graph.add_edge(page, "PAGE_DESCRIBES_ITEM", item, page_id, manifest_name)
                for story_id in value_list(record.get("related_story_ids")):
                    story = _entity_reference(graph, "story", story_id, None)
                    if story:
                        graph.add_edge(item, "RELATED_TO_STORY", story, page_id, manifest_name)
                for mail_id in value_list(record.get("related_mail_ids")):
                    mail = _entity_reference(graph, "mail", mail_id, None)
                    if mail:
                        graph.add_edge(item, "RELATED_TO_MAIL", mail, page_id, manifest_name)
        elif source_type == "furniture_lore":
            furniture = _entity_reference(graph, "furniture", record.get("furniture_id") or record.get("page_id"), record.get("furniture_name"))
            if furniture:
                graph.add_edge(page, "PAGE_DESCRIBES_FURNITURE", furniture, page_id, manifest_name)
                for index, owner_id in enumerate(value_list(record.get("owner_character_ids"))):
                    owner = _entity_reference(
                        graph,
                        "character",
                        owner_id,
                        value_list(record.get("owner_characters"))[index]
                        if index < len(value_list(record.get("owner_characters")))
                        else char_names.get(owner_id),
                    )
                    if owner:
                        graph.add_edge(owner, "OWNS_FURNITURE", furniture, page_id, manifest_name)
        elif source_type == "event_lore":
            event = _entity_reference(graph, "event", record.get("event_id") or record.get("page_id"), record.get("event_name"))
            if event:
                graph.add_edge(page, "PAGE_DESCRIBES_EVENT", event, page_id, manifest_name)
                for index, participant_id in enumerate(value_list(record.get("participant_ids"))):
                    participant = _entity_reference(
                        graph,
                        "character",
                        participant_id,
                        value_list(record.get("participant_names"))[index]
                        if index < len(value_list(record.get("participant_names")))
                        else char_names.get(participant_id),
                    )
                    if participant:
                        graph.add_edge(participant, "PARTICIPATES_IN_EVENT", event, page_id, manifest_name)
        elif source_type == "enemy_lore":
            enemy = _entity_reference(graph, "enemy", record.get("enemy_id") or record.get("page_id"), record.get("enemy_name"))
            if enemy:
                graph.add_edge(page, "PAGE_DESCRIBES_ENEMY", enemy, page_id, manifest_name)
        elif source_type == "logistics_lore":
            squad = _entity_reference(graph, "logistics_squad", record.get("squad_id") or record.get("page_id"), record.get("squad_name"))
            if squad:
                graph.add_edge(page, "PAGE_DESCRIBES_LOGISTICS_SQUAD", squad, page_id, manifest_name)
                for index, member_id in enumerate(value_list(record.get("member_ids"))):
                    member = _entity_reference(
                        graph,
                        "character",
                        member_id,
                        value_list(record.get("member_names"))[index]
                        if index < len(value_list(record.get("member_names")))
                        else char_names.get(member_id),
                    )
                    if member:
                        graph.add_edge(member, "MEMBER_OF_LOGISTICS_SQUAD", squad, page_id, manifest_name)

        _add_related_character_edges(graph, page, record, page_id, manifest_name, char_names)

    nodes = sorted(graph.nodes.values(), key=lambda row: (row["node_type"], row["name"], row["node_id"]))
    edges = sorted(graph.edges.values(), key=lambda row: (row["relation_type"], row["from_id"], row["to_id"]))
    output = ensure_runtime("graph")
    write_jsonl(output / "nodes.jsonl", nodes)
    write_jsonl(output / "edges.jsonl", edges)
    report = {
        "stage": "C",
        "job": "build_graph",
        "generated_at": utc_now(),
        "nodes": len(nodes),
        "edges": len(edges),
        "review_status": "All generated edges are deterministic and verified from specialized manifests.",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_graph.json", report)
    return report


def _split_relation_evidence_documents(
    documents: list[dict[str, Any]], max_evidence_chars: int = RELATION_JOB_MAX_EVIDENCE_CHARS
) -> list[list[dict[str, Any]]]:
    """Group ordered text chunks into bounded provider-ready evidence segments."""
    if max_evidence_chars < 1:
        raise ValueError("max_evidence_chars must be positive.")
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for document in documents:
        text_size = len(str(document.get("text") or ""))
        if current and current_size + text_size > max_evidence_chars:
            groups.append(current)
            current = []
            current_size = 0
        current.append(document)
        current_size += text_size
    if current:
        groups.append(current)
    return groups


def _relation_job(
    page_id: str,
    source_type: str,
    metadata: dict[str, Any],
    evidence_documents: list[dict[str, Any]],
    segment_index: int,
    segment_count: int,
    parent_job_id: str | None = None,
) -> dict[str, Any]:
    evidence_document_ids = [document["document_id"] for document in evidence_documents]
    job = {
        "job_id": stable_id("relation_extract", page_id, *evidence_document_ids, prefix="relation_job_"),
        "kind": "narrative_relation_extraction",
        "page_id": page_id,
        "source_type": source_type,
        "character_context": {
            "character_id": metadata.get("character_id"),
            "character_name": metadata.get("character_name"),
        },
        "evidence_document_ids": evidence_document_ids,
        "evidence_segment": {"index": segment_index, "count": segment_count},
        "allowed_relation_types": [
            "KNOWS",
            "ALLY_OF",
            "OPPOSES",
            "MENTIONS",
            "VISITS_LOCATION",
            "PARTICIPATES_IN_EVENT",
            "OWNS_ITEM",
            "HAS_PREFERENCE",
            "HAS_RELATIONSHIP_CONTEXT",
        ],
        "status": "queued",
        "requires_human_review": True,
        "created_at": utc_now(),
    }
    if parent_job_id:
        job["parent_job_id"] = parent_job_id
    return job


def split_pending_relation_jobs(
    max_evidence_chars: int = RELATION_JOB_MAX_EVIDENCE_CHARS, statuses: set[str] | None = None
) -> dict[str, Any]:
    """Replace only queued/failed oversized page jobs with evidence-bounded children.

    Existing completed jobs and their reviewed candidates remain untouched. Superseded
    parent jobs preserve the audit link to their new children and are ignored by the
    extractor.
    """
    jobs_path = RUNTIME_ROOT / "review" / "narrative_relation_jobs.jsonl"
    if not jobs_path.exists():
        raise FileNotFoundError("Relation job queue is missing. Run the C stage before splitting pending jobs.")
    statuses = statuses or {"queued", "failed"}
    documents = {document["document_id"]: document for document in load_runtime_jsonl("documents.jsonl")}
    existing_jobs = list(read_jsonl(jobs_path))
    parent_by_child = {
        child_id: job["job_id"]
        for job in existing_jobs
        if job.get("status") == "superseded"
        for child_id in job.get("child_job_ids", [])
    }
    rewritten: list[dict[str, Any]] = []
    parents_split = 0
    children_created = 0
    for job in existing_jobs:
        if job.get("job_id") in parent_by_child and not job.get("parent_job_id"):
            job = {**job, "parent_job_id": parent_by_child[job["job_id"]]}
        if job.get("status") not in statuses:
            rewritten.append(job)
            continue
        evidence = [documents[identifier] for identifier in job.get("evidence_document_ids", []) if identifier in documents]
        segments = _split_relation_evidence_documents(evidence, max_evidence_chars)
        if len(segments) <= 1:
            rewritten.append(job)
            continue
        children = [
            _relation_job(
                str(job["page_id"]),
                str(job["source_type"]),
                job.get("character_context") or {},
                segment,
                index,
                len(segments),
                str(job["job_id"]),
            )
            for index, segment in enumerate(segments, start=1)
        ]
        parent = {
            **job,
            "status": "superseded",
            "superseded_at": utc_now(),
            "superseded_reason": f"evidence exceeds {max_evidence_chars} characters",
            "child_job_ids": [child["job_id"] for child in children],
        }
        rewritten.extend((parent, *children))
        parents_split += 1
        children_created += len(children)
    write_jsonl(jobs_path, rewritten)
    report = {
        "stage": "C",
        "job": "split_pending_relation_jobs",
        "generated_at": utc_now(),
        "max_evidence_chars": max_evidence_chars,
        "target_statuses": sorted(statuses),
        "parents_split": parents_split,
        "children_created": children_created,
        "active_jobs": sum(job.get("status") in {"queued", "failed"} for job in rewritten),
        "policy": "Completed jobs and existing review candidates are preserved; only pending oversized jobs are segmented.",
    }
    write_json(RUNTIME_ROOT / "reports" / "split_pending_relation_jobs.json", report)
    return report


def build_relation_review_jobs() -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    relevant_types = {"main_story", "character_story", "affinity_story", "special_mail", "random_event", "event_lore"}
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for document in documents:
        if document["source_type"] in relevant_types:
            grouped[document["page_id"]].append(document)
    jobs = []
    for page_id, page_documents in sorted(grouped.items()):
        metadata = page_documents[0]["metadata"]
        segments = _split_relation_evidence_documents(page_documents)
        jobs.extend(
            _relation_job(page_id, page_documents[0]["source_type"], metadata, segment, index, len(segments))
            for index, segment in enumerate(segments, start=1)
        )
    output = ensure_runtime("review")
    write_jsonl(output / "narrative_relation_jobs.jsonl", jobs)
    write_jsonl(output / "narrative_relation_candidates.jsonl", [])
    report = {
        "stage": "C",
        "job": "build_relation_review_jobs",
        "generated_at": utc_now(),
        "queued_jobs": len(jobs),
        "policy": "Candidates remain pending_review and are excluded from graph retrieval until approved.",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_relation_review_jobs.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-only", action="store_true")
    parser.add_argument("--split-pending", action="store_true")
    parser.add_argument("--split-failed", action="store_true")
    parser.add_argument("--max-evidence-chars", type=int, default=RELATION_JOB_MAX_EVIDENCE_CHARS)
    args = parser.parse_args()
    if args.split_pending:
        result = {"relation_jobs": split_pending_relation_jobs(args.max_evidence_chars)}
    elif args.split_failed:
        result = {"relation_jobs": split_pending_relation_jobs(args.max_evidence_chars, {"failed"})}
    elif args.jobs_only:
        result = {"relation_jobs": build_relation_review_jobs()}
    else:
        result = {"graph": build_graph(), "relation_jobs": build_relation_review_jobs()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
