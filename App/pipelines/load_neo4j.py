"""Project portable graph artifacts into a Neo4j serving database."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from backend.snow_app.graph_metadata import hydrate_human_approved_edge

from .common import RUNTIME_ROOT, utc_now, write_json


def load_neo4j(clear: bool = False) -> dict[str, Any]:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("neo4j driver is required. Install App/requirements.txt.") from exc
    nodes_path = RUNTIME_ROOT / "graph" / "nodes.jsonl"
    edges_path = RUNTIME_ROOT / "graph" / "edges.jsonl"
    if not nodes_path.exists() or not edges_path.exists():
        raise RuntimeError("Graph artifacts are missing. Run python -m pipelines.build_graph first.")
    deterministic_nodes = [json.loads(line) for line in nodes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    deterministic_edges = [json.loads(line) for line in edges_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    review_root = RUNTIME_ROOT / "review"
    approved_nodes_path = review_root / "approved_entity_nodes.jsonl"
    approved_edges_path = review_root / "approved_narrative_edges.jsonl"
    relation_candidates_path = review_root / "narrative_relation_candidates.jsonl"
    documents_path = RUNTIME_ROOT / "lakehouse" / "documents.jsonl"
    approved_nodes = (
        [json.loads(line) for line in approved_nodes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if approved_nodes_path.exists()
        else []
    )
    approved_edges_raw = (
        [json.loads(line) for line in approved_edges_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if approved_edges_path.exists()
        else []
    )
    candidates_by_id = (
        {
            str(candidate.get("candidate_id")): candidate
            for candidate in (
                json.loads(line)
                for line in relation_candidates_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if candidate.get("candidate_id")
        }
        if relation_candidates_path.exists()
        else {}
    )
    documents_by_id = (
        {
            str(document.get("document_id")): document
            for document in (
                json.loads(line)
                for line in documents_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if document.get("document_id")
        }
        if documents_path.exists()
        else {}
    )
    approved_edges = [
        hydrate_human_approved_edge(edge, candidates_by_id, documents_by_id) for edge in approved_edges_raw
    ]
    nodes = list(
        {
            node["node_id"]: node
            for node in deterministic_nodes + [node for node in approved_nodes if node.get("review_status") == "verified"]
            if node.get("node_id")
        }.values()
    )
    edges = list(
        {
            edge["edge_id"]: edge
            for edge in deterministic_edges + [edge for edge in approved_edges if edge.get("review_status") == "verified"]
            if edge.get("edge_id")
        }.values()
    )
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD must be configured before loading the graph.")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            session.run("CREATE CONSTRAINT graph_node_id IF NOT EXISTS FOR (n:SnowEntity) REQUIRE n.node_id IS UNIQUE")
            if clear:
                session.run("MATCH (n:SnowEntity) DETACH DELETE n")
            session.run(
                """
                UNWIND $nodes AS row
                MERGE (n:SnowEntity {node_id: row.node_id})
                SET n.node_type = row.node_type, n.name = row.name,
                    n.attributes_json = row.attributes_json, n.updated_at = row.updated_at
                """,
                nodes=[
                    {
                        "node_id": node["node_id"],
                        "node_type": node["node_type"],
                        "name": node["name"],
                        "attributes_json": json.dumps(node.get("attributes", {}), ensure_ascii=False),
                        "updated_at": utc_now(),
                    }
                    for node in nodes
                ],
            )
            session.run(
                """
                UNWIND $edges AS row
                MATCH (from:SnowEntity {node_id: row.from_id})
                MATCH (to:SnowEntity {node_id: row.to_id})
                MERGE (from)-[edge:SNOW_RELATION {edge_id: row.edge_id}]->(to)
                SET edge.relation_type = row.relation_type,
                    edge.evidence_page_ids_json = row.evidence_page_ids_json,
                    edge.source_manifests_json = row.source_manifests_json,
                    edge.source_types_json = row.source_types_json,
                    edge.narrative_scope = row.narrative_scope,
                    edge.confidence = row.confidence,
                    edge.review_status = row.review_status
                """,
                edges=[
                    {
                        "edge_id": edge["edge_id"],
                        "from_id": edge["from_id"],
                        "to_id": edge["to_id"],
                        "relation_type": edge["relation_type"],
                        "evidence_page_ids_json": json.dumps(edge["evidence_page_ids"], ensure_ascii=False),
                        "source_manifests_json": json.dumps(edge["source_manifests"], ensure_ascii=False),
                        "source_types_json": json.dumps(edge.get("source_types", []), ensure_ascii=False),
                        "narrative_scope": edge.get("narrative_scope", "unknown"),
                        "confidence": edge["confidence"],
                        "review_status": edge["review_status"],
                    }
                    for edge in edges
                    if edge["review_status"] == "verified"
                ],
            )
    finally:
        driver.close()
    report = {
        "stage": "C",
        "job": "load_neo4j",
        "generated_at": utc_now(),
        "nodes": len(nodes),
        "verified_edges": sum(1 for edge in edges if edge["review_status"] == "verified"),
        "human_approved_nodes": sum(1 for node in approved_nodes if node.get("review_status") == "verified"),
        "human_approved_edges": sum(1 for edge in approved_edges if edge.get("review_status") == "verified"),
        "uri": uri,
    }
    write_json(RUNTIME_ROOT / "reports" / "load_neo4j.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    print(json.dumps(load_neo4j(args.clear), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
