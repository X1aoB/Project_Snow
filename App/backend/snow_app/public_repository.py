"""Serving repository using FTS5 plus containerized embedding/Qdrant and Neo4j."""

from __future__ import annotations

import re
import threading
from typing import Any

import httpx

from .config import PublicSettings, Settings
from .repository import RuntimeRepository


class PublicRuntimeRepository(RuntimeRepository):
    def __init__(self, settings: Settings, public_settings: PublicSettings):
        super().__init__(settings)
        self.public_settings = public_settings
        self._health_local = threading.local()

    def _set_health(self, service: str, status: str) -> None:
        health = dict(getattr(self._health_local, "value", {}) or {})
        health[service] = status
        self._health_local.value = health

    def request_health(self) -> dict[str, str]:
        return dict(getattr(self._health_local, "value", {}) or {})

    def reset_request_health(self) -> None:
        self._health_local.value = {}

    def _embed_query(self, query: str) -> list[float] | None:
        try:
            response = httpx.post(
                f"{self.public_settings.embedding_url}/embed",
                json={"inputs": [query]},
                timeout=15,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            vectors = payload.get("vectors") if isinstance(payload, dict) else payload
            vector = vectors[0] if isinstance(vectors, list) and vectors else None
            if not isinstance(vector, list) or not vector:
                raise ValueError("embedding response is empty")
            self._set_health("embedding", "ok")
            return [float(value) for value in vector]
        except Exception:
            self._set_health("embedding", "degraded")
            return None

    def vector_search(self, query: str, limit: int = 40) -> list[tuple[str, int]]:
        query_vector = self._embed_query(query)
        if not query_vector:
            self._set_health("qdrant", "degraded")
            return []
        try:
            response = httpx.post(
                f"{self.public_settings.qdrant_url}/collections/{self.public_settings.qdrant_collection}/points/search",
                json={"vector": query_vector, "limit": limit, "with_payload": True, "with_vector": False},
                headers={"api-key": self.public_settings.qdrant_api_key},
                timeout=15,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            records = (payload.get("result") if isinstance(payload, dict) else None) or []
            document_ids = [
                str((record.get("payload") or {}).get("document_id") or record.get("id") or "")
                for record in records
                if isinstance(record, dict)
            ]
            self._set_health("qdrant", "ok")
            return [(document_id, rank) for rank, document_id in enumerate(document_ids, start=1) if document_id]
        except Exception:
            self._set_health("qdrant", "degraded")
            return []

    def serving_graph_context(
        self,
        query: str,
        character_id: str | None,
        intents: tuple[str, ...],
    ) -> dict[str, Any]:
        if not self.public_settings.neo4j_password:
            self._set_health("neo4j", "degraded")
            return {"status": "degraded", "nodes": [], "edges": []}
        try:
            from neo4j import GraphDatabase

            names = [query]
            if character_id:
                character = self.graph_nodes().get(f"character:{character_id}")
                if character and character.get("name"):
                    names.append(str(character["name"]))
            terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,12}", " ".join(names))[:8]]
            driver = GraphDatabase.driver(
                self.public_settings.neo4j_uri,
                auth=(self.public_settings.neo4j_user, self.public_settings.neo4j_password),
                connection_timeout=5,
            )
            try:
                with driver.session() as session:
                    rows = session.run(
                        """
                        MATCH (pointer:SnowDatasetPointer {name: 'active'})
                        MATCH (start:SnowEntity)
                        WHERE start.dataset_version = pointer.version
                          AND (start.node_id = $character_node_id
                           OR any(term IN $terms WHERE start.name CONTAINS term))
                        WITH DISTINCT start LIMIT 4
                        OPTIONAL MATCH path=(start)-[rels:SNOW_RELATION*1..2]-(other:SnowEntity)
                        WHERE other.dataset_version = start.dataset_version
                        RETURN start, other, relationships(path) AS rels LIMIT 16
                        """,
                        character_node_id=f"character:{character_id}" if character_id else "",
                        terms=terms,
                    )
                    nodes: dict[str, dict[str, Any]] = {}
                    edges: dict[str, dict[str, Any]] = {}
                    for row in rows:
                        for raw_node in (row.get("start"), row.get("other")):
                            if raw_node is None:
                                continue
                            node = dict(raw_node)
                            node_id = str(node.get("node_id") or "")
                            if node_id:
                                nodes[node_id] = {
                                    "node_id": node_id,
                                    "node_type": node.get("node_type"),
                                    "name": node.get("name"),
                                }
                        for raw_edge in row.get("rels") or []:
                            edge = dict(raw_edge)
                            edge_id = str(edge.get("edge_id") or "")
                            if edge_id:
                                edges[edge_id] = {
                                    "edge_id": edge_id,
                                    "relation_type": edge.get("relation_type"),
                                    "evidence_page_ids_json": edge.get("evidence_page_ids_json"),
                                }
            finally:
                driver.close()
            self._set_health("neo4j", "ok")
            return {"status": "ok", "nodes": list(nodes.values())[:12], "edges": list(edges.values())[:16]}
        except Exception:
            self._set_health("neo4j", "degraded")
            return {"status": "degraded", "nodes": [], "edges": []}

    def dependency_health(self) -> dict[str, str]:
        health: dict[str, str] = {}
        for service, url in (
            ("embedding", f"{self.public_settings.embedding_url}/health"),
            (
                "qdrant",
                f"{self.public_settings.qdrant_url}/collections/{self.public_settings.qdrant_collection}",
            ),
        ):
            try:
                response = httpx.get(
                    url,
                    headers={"api-key": self.public_settings.qdrant_api_key} if service == "qdrant" else None,
                    timeout=5,
                    follow_redirects=False,
                )
                response.raise_for_status()
                if service == "qdrant":
                    points_count = int(((response.json().get("result") or {}).get("points_count") or 0))
                    health[service] = "ok" if points_count > 0 else "degraded"
                else:
                    health[service] = "ok"
            except Exception:
                health[service] = "degraded"
        if self.public_settings.neo4j_password:
            try:
                from neo4j import GraphDatabase

                driver = GraphDatabase.driver(
                    self.public_settings.neo4j_uri,
                    auth=(self.public_settings.neo4j_user, self.public_settings.neo4j_password),
                    connection_timeout=5,
                )
                try:
                    with driver.session() as session:
                        active = session.run(
                            "MATCH (pointer:SnowDatasetPointer {name: 'active'}) "
                            "MATCH (node:SnowEntity) "
                            "WHERE node.dataset_version = pointer.version "
                            "RETURN count(node) AS nodes"
                        ).single()
                    health["neo4j"] = "ok" if active and int(active["nodes"]) > 0 else "degraded"
                finally:
                    driver.close()
            except Exception:
                health["neo4j"] = "degraded"
        else:
            health["neo4j"] = "degraded"
        return health
