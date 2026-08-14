"""Build a traceable serving graph without mutating review history."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = APP_ROOT / "runtime"
SUPPORTED_REVIEW_NODE_TYPES = {"location", "event"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=RUNTIME_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    output = (args.output or runtime / "release" / "graph").resolve()
    graph_root = runtime / "graph"
    review_root = runtime / "review"

    deterministic_nodes = read_jsonl(graph_root / "nodes.jsonl")
    reviewed_nodes = read_jsonl(review_root / "approved_entity_nodes.jsonl")
    candidate_rows = read_jsonl(review_root / "entity_node_candidates.jsonl")
    candidate_by_id = {str(row.get("entity_candidate_id")): row for row in candidate_rows}
    included_nodes: list[dict] = list(deterministic_nodes)
    excluded_nodes: list[dict] = []
    for node in reviewed_nodes:
        attributes = dict(node.get("attributes") or {})
        candidate = candidate_by_id.get(str(attributes.get("entity_candidate_id") or "")) or {}
        evidence_page_ids = list(
            dict.fromkeys(
                [
                    *(attributes.get("evidence_page_ids") or []),
                    *(candidate.get("evidence_page_ids") or []),
                ]
            )
        )
        reasons = []
        if node.get("review_status") != "verified":
            reasons.append("not_verified")
        if node.get("node_type") not in SUPPORTED_REVIEW_NODE_TYPES:
            reasons.append("unsupported_node_type")
        if not node.get("name"):
            reasons.append("missing_name")
        if not evidence_page_ids:
            reasons.append("missing_traceable_evidence")
        if reasons:
            excluded_nodes.append({"node_id": node.get("node_id"), "reasons": reasons})
            continue
        included_nodes.append(
            {
                **node,
                "attributes": {
                    **attributes,
                    "evidence_page_ids": evidence_page_ids,
                    "source_types": list(
                        dict.fromkeys(
                            [
                                *(attributes.get("source_types") or []),
                                *(candidate.get("source_types") or []),
                            ]
                        )
                    ),
                },
            }
        )

    node_ids = {str(row.get("node_id")) for row in included_nodes if row.get("node_id")}
    reviewed_edges = read_jsonl(review_root / "approved_narrative_edges.jsonl")
    included_edges = list(read_jsonl(graph_root / "edges.jsonl"))
    excluded_edges: list[dict] = []
    for edge in reviewed_edges:
        reasons = []
        if edge.get("review_status") != "verified":
            reasons.append("not_verified")
        if edge.get("from_id") not in node_ids or edge.get("to_id") not in node_ids:
            reasons.append("endpoint_not_publishable")
        if not edge.get("evidence_page_ids"):
            reasons.append("missing_traceable_evidence")
        if reasons:
            excluded_edges.append({"edge_id": edge.get("edge_id"), "reasons": reasons})
        else:
            included_edges.append(edge)

    nodes_path = output / "nodes.jsonl"
    edges_path = output / "edges.jsonl"
    write_jsonl(nodes_path, sorted(included_nodes, key=lambda row: str(row.get("node_id"))))
    write_jsonl(edges_path, sorted(included_edges, key=lambda row: str(row.get("edge_id"))))
    report = {
        "schema_version": "project-snow-publishable-graph-1",
        "generated_at": datetime.now(UTC).isoformat(),
        "included_nodes": len(included_nodes),
        "included_edges": len(included_edges),
        "excluded_nodes": excluded_nodes,
        "excluded_edges": excluded_edges,
        "files": {
            "nodes.jsonl": sha256(nodes_path),
            "edges.jsonl": sha256(edges_path),
        },
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"excluded_nodes", "excluded_edges"}}, ensure_ascii=False, indent=2))
    return 1 if args.strict and (excluded_nodes or excluded_edges) else 0


if __name__ == "__main__":
    raise SystemExit(main())
