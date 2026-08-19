"""Load a verified data release into versioned Qdrant and Neo4j serving stores."""

from __future__ import annotations

import argparse
from hashlib import sha256
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Iterator
from uuid import NAMESPACE_URL, uuid5

import httpx

from .config import PublicSettings
from .data_release import DataReleaseError, iter_jsonl, verify_data_release


def versioned_collection_name(alias: str, version: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", version.casefold()).strip("-_")[:40] or "release"
    return f"{alias}__{slug}-{sha256(version.encode()).hexdigest()[:8]}"


def qdrant_point_id(document_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"project-snow:{document_id}"))


def batches(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _raise_for_status(response: Any) -> Any:
    response.raise_for_status()
    return response


def _wait_for_http(client: Any, path: str, attempts: int = 30) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            _raise_for_status(client.get(path))
            return
        except Exception as exc:  # pragma: no cover - retry timing is integration-tested
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"dependency did not become ready: {path}") from last_error


def _qdrant_points(release_root: Path) -> Iterator[dict[str, Any]]:
    documents = iter_jsonl(release_root / "lakehouse" / "documents.jsonl")
    vectors = iter_jsonl(release_root / "vectors" / "local_vectors.jsonl")
    for document, vector_row in zip_longest(documents, vectors):
        if document is None or vector_row is None:
            raise DataReleaseError("document and vector release lengths do not match")
        document_id = str(document.get("document_id") or "")
        if document_id != str(vector_row.get("document_id") or ""):
            raise DataReleaseError("document and vector release order does not match")
        yield {
            "id": qdrant_point_id(document_id),
            "vector": vector_row["vector"],
            "payload": {
                "document_id": document_id,
                "page_id": document.get("page_id"),
                "source_type": document.get("source_type"),
                "title": document.get("title"),
                "canonical_url": document.get("canonical_url"),
                "metadata": document.get("metadata") or {},
            },
        }


def load_qdrant(
    release_root: Path,
    version: str,
    url: str,
    alias: str,
    api_key: str,
    *,
    client: Any | None = None,
    activate: bool = False,
) -> dict[str, Any]:
    manifest = verify_data_release(release_root, version)
    dimension = int((manifest.get("statistics") or {}).get("vector_dimension") or 0)
    expected_count = int((manifest.get("statistics") or {}).get("vectors") or 0)
    if dimension <= 0 or expected_count <= 0:
        raise DataReleaseError("release manifest has invalid vector statistics")
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"api-key": api_key},
            timeout=30,
            trust_env=False,
        )
    collection = versioned_collection_name(alias, version)
    previous_collection: str | None = None
    try:
        _wait_for_http(client, "/healthz")
        aliases_response = _raise_for_status(client.get("/aliases")).json()
        aliases = ((aliases_response.get("result") or {}).get("aliases") or [])
        previous_collection = next(
            (
                str(item.get("collection_name"))
                for item in aliases
                if item.get("alias_name") == alias and item.get("collection_name")
            ),
            None,
        )
        collection_response = client.get(f"/collections/{collection}")
        reused = collection_response.status_code == 200
        if reused:
            collection_state = _raise_for_status(collection_response).json()
            uploaded = int(
                ((collection_state.get("result") or {}).get("points_count") or 0)
            )
            if uploaded != expected_count:
                if previous_collection == collection:
                    raise RuntimeError(
                        "active Qdrant collection does not match its release manifest"
                    )
                _raise_for_status(client.delete(f"/collections/{collection}"))
                reused = False
        elif collection_response.status_code != 404:
            collection_response.raise_for_status()

        if not reused:
            _raise_for_status(
                client.put(
                    f"/collections/{collection}",
                    json={"vectors": {"size": dimension, "distance": "Cosine"}},
                )
            )
            uploaded = 0
            for point_batch in batches(_qdrant_points(release_root), 128):
                _raise_for_status(
                    client.put(
                        f"/collections/{collection}/points",
                        params={"wait": "true"},
                        json={"points": point_batch},
                    )
                )
                uploaded += len(point_batch)
            if uploaded != expected_count:
                raise DataReleaseError(
                    "Qdrant upload count does not match release manifest"
                )
            collection_state = _raise_for_status(
                client.get(f"/collections/{collection}")
            ).json()
            points_count = int(
                ((collection_state.get("result") or {}).get("points_count") or 0)
            )
            if points_count != expected_count:
                raise RuntimeError("Qdrant collection count verification failed")

        if activate and previous_collection != collection:
            actions: list[dict[str, Any]] = []
            if previous_collection:
                actions.append({"delete_alias": {"alias_name": alias}})
            actions.append(
                {"create_alias": {"collection_name": collection, "alias_name": alias}}
            )
            _raise_for_status(
                client.post("/collections/aliases", json={"actions": actions})
            )

        cleanup_failures: list[str] = []
        if activate:
            keep = {collection}
            if previous_collection:
                keep.add(previous_collection)
            collections_response = _raise_for_status(client.get("/collections")).json()
            collections = (
                (collections_response.get("result") or {}).get("collections") or []
            )
            prefix = f"{alias}__"
            for item in collections:
                name = str(item.get("name") or "")
                if name.startswith(prefix) and name not in keep:
                    try:
                        _raise_for_status(client.delete(f"/collections/{name}"))
                    except Exception:
                        cleanup_failures.append(name)
        return {
            "alias": alias,
            "collection": collection,
            "previous_collection": previous_collection,
            "active_collection": collection if activate else previous_collection,
            "points": uploaded,
            "dimension": dimension,
            "activated": activate,
            "reused": reused,
            "cleanup_failures": cleanup_failures,
        }
    finally:
        if owned_client:
            client.close()


def _neo4j_node(version: str, node: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    return {
        "dataset_key": f"{version}\x1f{node_id}",
        "dataset_version": version,
        "node_id": node_id,
        "node_type": node.get("node_type"),
        "name": node.get("name"),
        "attributes_json": json.dumps(node.get("attributes") or {}, ensure_ascii=False),
    }


def _neo4j_edge(version: str, edge: dict[str, Any]) -> dict[str, Any]:
    edge_id = str(edge.get("edge_id") or "")
    return {
        "dataset_key": f"{version}\x1f{edge_id}",
        "dataset_version": version,
        "edge_id": edge_id,
        "from_key": f"{version}\x1f{edge.get('from_id')}",
        "to_key": f"{version}\x1f{edge.get('to_id')}",
        "relation_type": edge.get("relation_type"),
        "evidence_page_ids_json": json.dumps(edge.get("evidence_page_ids") or [], ensure_ascii=False),
        "source_manifests_json": json.dumps(edge.get("source_manifests") or [], ensure_ascii=False),
        "source_types_json": json.dumps(edge.get("source_types") or [], ensure_ascii=False),
        "narrative_scope": edge.get("narrative_scope") or "unknown",
        "confidence": edge.get("confidence"),
        "review_status": edge.get("review_status"),
    }


def _activate_neo4j_dataset(
    session: Any, version: str, previous_version: str | None
) -> str:
    session.run(
        "MERGE (pointer:SnowDatasetPointer {name: 'active'}) "
        "SET pointer.version = $version, pointer.updated_at = datetime()",
        version=version,
    ).consume()
    keep_versions = [value for value in (version, previous_version) if value]
    cleanup_status = "ok"
    try:
        session.run(
            "MATCH (node:SnowEntity) WHERE NOT node.dataset_version IN $keep_versions "
            "DETACH DELETE node",
            keep_versions=keep_versions,
        ).consume()
    except Exception:
        cleanup_status = "deferred"
    return cleanup_status


def load_neo4j(
    release_root: Path,
    version: str,
    uri: str,
    user: str,
    password: str,
    *,
    driver: Any | None = None,
    activate: bool = False,
) -> dict[str, Any]:
    manifest = verify_data_release(release_root, version)
    statistics = manifest.get("statistics") or {}
    expected_nodes = int(statistics.get("graph_nodes") or 0)
    expected_edges = int(statistics.get("graph_edges") or 0)
    owned_driver = driver is None
    if driver is None:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            session.run(
                "CREATE CONSTRAINT snow_dataset_pointer_name IF NOT EXISTS "
                "FOR (pointer:SnowDatasetPointer) REQUIRE pointer.name IS UNIQUE"
            ).consume()
            session.run(
                "CREATE CONSTRAINT snow_entity_dataset_key IF NOT EXISTS "
                "FOR (node:SnowEntity) REQUIRE node.dataset_key IS UNIQUE"
            ).consume()
            previous_row = session.run(
                "MATCH (pointer:SnowDatasetPointer {name: 'active'}) RETURN pointer.version AS version"
            ).single()
            previous_version = str(previous_row["version"]) if previous_row and previous_row.get("version") else None
            node_row = session.run(
                "MATCH (node:SnowEntity {dataset_version: $version}) RETURN count(node) AS count",
                version=version,
            ).single()
            edge_row = session.run(
                "MATCH ()-[edge:SNOW_RELATION {dataset_version: $version}]->() RETURN count(edge) AS count",
                version=version,
            ).single()
            node_count = int(node_row["count"]) if node_row else 0
            edge_count = int(edge_row["count"]) if edge_row else 0
            reused = node_count == expected_nodes and edge_count == expected_edges
            if reused:
                cleanup_status = (
                    _activate_neo4j_dataset(session, version, previous_version)
                    if activate
                    else "not_requested"
                )
                return {
                    "dataset_version": version,
                    "active_version": version if activate else previous_version,
                    "previous_version": previous_version,
                    "nodes": node_count,
                    "edges": edge_count,
                    "activated": activate,
                    "cleanup_status": cleanup_status,
                    "reused": True,
                }
            if node_count or edge_count:
                if previous_version == version:
                    raise RuntimeError("active Neo4j dataset does not match its release manifest")
            session.run(
                "MATCH (node:SnowEntity {dataset_version: $version}) DETACH DELETE node",
                version=version,
            ).consume()
            node_count = 0
            for node_batch in batches((_neo4j_node(version, row) for row in iter_jsonl(release_root / "graph" / "nodes.jsonl")), 500):
                session.run(
                    """
                    UNWIND $nodes AS row
                    MERGE (node:SnowEntity {dataset_key: row.dataset_key})
                    SET node.dataset_version = row.dataset_version,
                        node.node_id = row.node_id,
                        node.node_type = row.node_type,
                        node.name = row.name,
                        node.attributes_json = row.attributes_json
                    """,
                    nodes=node_batch,
                ).consume()
                node_count += len(node_batch)
            edge_count = 0
            for edge_batch in batches((_neo4j_edge(version, row) for row in iter_jsonl(release_root / "graph" / "edges.jsonl")), 500):
                session.run(
                    """
                    UNWIND $edges AS row
                    MATCH (source:SnowEntity {dataset_key: row.from_key})
                    MATCH (target:SnowEntity {dataset_key: row.to_key})
                    MERGE (source)-[edge:SNOW_RELATION {dataset_key: row.dataset_key}]->(target)
                    SET edge.dataset_version = row.dataset_version,
                        edge.edge_id = row.edge_id,
                        edge.relation_type = row.relation_type,
                        edge.evidence_page_ids_json = row.evidence_page_ids_json,
                        edge.source_manifests_json = row.source_manifests_json,
                        edge.source_types_json = row.source_types_json,
                        edge.narrative_scope = row.narrative_scope,
                        edge.confidence = row.confidence,
                        edge.review_status = row.review_status
                    """,
                    edges=edge_batch,
                ).consume()
                edge_count += len(edge_batch)
            verified_node_row = session.run(
                "MATCH (node:SnowEntity {dataset_version: $version}) RETURN count(node) AS count",
                version=version,
            ).single()
            verified_edge_row = session.run(
                "MATCH ()-[edge:SNOW_RELATION {dataset_version: $version}]->() RETURN count(edge) AS count",
                version=version,
            ).single()
            verified_nodes = int(verified_node_row["count"]) if verified_node_row else 0
            verified_edges = int(verified_edge_row["count"]) if verified_edge_row else 0
            if verified_nodes != expected_nodes or verified_edges != expected_edges:
                raise RuntimeError("Neo4j dataset count verification failed")
            cleanup_status = (
                _activate_neo4j_dataset(session, version, previous_version)
                if activate
                else "not_requested"
            )
        return {
            "dataset_version": version,
            "active_version": version if activate else previous_version,
            "previous_version": previous_version,
            "nodes": node_count,
            "edges": edge_count,
            "activated": activate,
            "cleanup_status": cleanup_status,
            "reused": False,
        }
    finally:
        if owned_driver:
            driver.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-root",
        type=Path,
        default=Path(
            os.getenv("PUBLIC_DATA_ROOT")
            or os.getenv("APP_RUNTIME")
            or "/srv/project-snow/data/current"
        ),
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="manually switch legacy Qdrant/Neo4j serving pointers after staging",
    )
    args = parser.parse_args()
    settings = PublicSettings.from_environment()
    manifest = verify_data_release(args.release_root, settings.data_version)
    if args.verify_only:
        print(json.dumps({"status": "ok", "data_version": manifest["data_version"]}))
        return 0
    if not settings.qdrant_api_key or not settings.neo4j_password:
        raise SystemExit("Qdrant and Neo4j credentials are required for a production data load")
    qdrant = load_qdrant(
        args.release_root,
        settings.data_version,
        settings.qdrant_url,
        settings.qdrant_collection,
        settings.qdrant_api_key,
        activate=args.activate,
    )
    neo4j = load_neo4j(
        args.release_root,
        settings.data_version,
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        activate=args.activate,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "data_version": settings.data_version,
                "activation_requested": args.activate,
                "qdrant": qdrant,
                "neo4j": neo4j,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
