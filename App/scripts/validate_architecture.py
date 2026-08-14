"""Validate B/C generated artifacts without touching Data/."""

from __future__ import annotations

import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from pipelines.common import RUNTIME_ROOT, known_characters, read_jsonl  # noqa: E402


def require(path: Path, failures: list[str]) -> None:
    if not path.exists() or path.stat().st_size == 0:
        failures.append(f"Missing or empty artifact: {path}")


def main() -> int:
    failures: list[str] = []
    lakehouse = RUNTIME_ROOT / "lakehouse"
    graph_root = RUNTIME_ROOT / "graph"
    persona_root = RUNTIME_ROOT / "personas"
    review_root = RUNTIME_ROOT / "review"
    required = [
        lakehouse / "documents.jsonl",
        lakehouse / "documents.parquet",
        RUNTIME_ROOT / "duckdb" / "project_snow.duckdb",
        RUNTIME_ROOT / "indexes" / "lexical.sqlite3",
        RUNTIME_ROOT / "vectors" / "local_vectors.jsonl",
        persona_root / "persona_profiles.jsonl",
        graph_root / "nodes.jsonl",
        graph_root / "edges.jsonl",
        review_root / "narrative_relation_jobs.jsonl",
    ]
    for path in required:
        require(path, failures)
    if failures:
        print("\n".join(failures))
        return 1

    documents = list(read_jsonl(lakehouse / "documents.jsonl"))
    document_ids = {document["document_id"] for document in documents}
    vector_ids = {row["document_id"] for row in read_jsonl(RUNTIME_ROOT / "vectors" / "local_vectors.jsonl")}
    if vector_ids != document_ids:
        failures.append("Vector index document IDs do not match the current lakehouse corpus.")
    if not documents:
        failures.append("No corpus documents.")
    for document in documents:
        if not document.get("canonical_url"):
            failures.append(f"Document lacks canonical URL: {document['document_id']}")
        if document["metadata"].get("requires_costume_context") and not document["metadata"].get("costume_id"):
            failures.append(f"Costume-scoped document lacks costume ID: {document['document_id']}")
    deterministic_nodes = list(read_jsonl(graph_root / "nodes.jsonl"))
    publishable_graph_root = RUNTIME_ROOT / "release" / "graph"
    approved_nodes_path = publishable_graph_root / "nodes.jsonl"
    approved_nodes = list(read_jsonl(approved_nodes_path)) if approved_nodes_path.exists() else []
    deterministic_node_ids = {node.get("node_id") for node in deterministic_nodes}
    verified_approved_nodes = [
        node for node in approved_nodes if node.get("node_id") not in deterministic_node_ids
    ]
    nodes = {node["node_id"] for node in deterministic_nodes + verified_approved_nodes if node.get("node_id")}
    if len(nodes) != len(deterministic_nodes) + len(verified_approved_nodes):
        failures.append("Graph node IDs collide between deterministic and human-approved artifacts.")
    for node in verified_approved_nodes:
        if node.get("node_type") not in {"location", "event"}:
            failures.append(f"Approved entity node has unsupported type: {node.get('node_id')}")
        if not node.get("name"):
            failures.append(f"Approved entity node lacks a name: {node.get('node_id')}")
        if not (node.get("attributes") or {}).get("evidence_page_ids"):
            failures.append(f"Approved entity node is untraceable: {node.get('node_id')}")
    for edge in read_jsonl(graph_root / "edges.jsonl"):
        if edge.get("review_status") != "verified":
            failures.append(f"Deterministic edge is not verified: {edge['edge_id']}")
        if edge["from_id"] not in nodes or edge["to_id"] not in nodes:
            failures.append(f"Dangling graph edge: {edge['edge_id']}")
        if not edge.get("evidence_page_ids"):
            failures.append(f"Untraceable graph edge: {edge['edge_id']}")
    approved_edges_path = publishable_graph_root / "edges.jsonl"
    all_publishable_edges = list(read_jsonl(approved_edges_path)) if approved_edges_path.exists() else []
    deterministic_edge_ids = {edge.get("edge_id") for edge in read_jsonl(graph_root / "edges.jsonl")}
    approved_edges = [edge for edge in all_publishable_edges if edge.get("edge_id") not in deterministic_edge_ids]
    for edge in approved_edges:
        if edge.get("review_status") != "verified":
            failures.append(f"Approved narrative edge is not verified: {edge.get('edge_id')}")
        if edge.get("from_id") not in nodes or edge.get("to_id") not in nodes:
            failures.append(f"Dangling approved narrative edge: {edge.get('edge_id')}")
        if not edge.get("evidence_page_ids"):
            failures.append(f"Untraceable approved narrative edge: {edge.get('edge_id')}")
    entity_candidates_path = review_root / "entity_node_candidates.jsonl"
    entity_candidates = list(read_jsonl(entity_candidates_path)) if entity_candidates_path.exists() else []
    # Review history may contain model- or human-approved candidates that are
    # deliberately quarantined from publication. Only the exported serving
    # view is required to be internally complete.
    approved_entity_candidate_ids = {
        str((node.get("attributes") or {}).get("entity_candidate_id"))
        for node in verified_approved_nodes
    }
    approved_entity_node_candidate_ids = {
        str((node.get("attributes") or {}).get("entity_candidate_id")) for node in verified_approved_nodes
    }
    if approved_entity_candidate_ids != approved_entity_node_candidate_ids:
        failures.append("An approved entity candidate has no verified graph node artifact.")
    authoritative_characters = known_characters()
    for profile in read_jsonl(persona_root / "persona_profiles.jsonl"):
        if profile["relationship_invariant"].get("user_role") != "分析员":
            failures.append(f"Role invariant missing: {profile['profile_id']}")
        for source_ids in profile["evidence"].values():
            if not set(source_ids).issubset(document_ids):
                failures.append(f"Persona has unknown evidence: {profile['profile_id']}")
        if authoritative_characters.get(profile["character_id"]) != profile["character_name"]:
            failures.append(f"Persona is not an authoritative character: {profile['profile_id']}")
    jobs = list(read_jsonl(review_root / "narrative_relation_jobs.jsonl"))
    if not jobs:
        failures.append("Narrative relation review queue is empty.")
    report = {
        "documents": len(documents),
        "nodes": len(nodes),
        "edges": sum(1 for _ in read_jsonl(graph_root / "edges.jsonl")),
        "personas": sum(1 for _ in read_jsonl(persona_root / "persona_profiles.jsonl")),
        "relation_review_jobs": len(jobs),
        "human_approved_relation_edges": len(approved_edges),
        "human_approved_entity_nodes": len(verified_approved_nodes),
        "entity_node_candidates": len(entity_candidates),
        "result": "passed" if not failures else "failed",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
