"""Serving repository using FTS5 plus containerized embedding/Qdrant and Neo4j."""

from __future__ import annotations

import os
from pathlib import Path
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from typing import Any

import httpx

from .config import PublicSettings, Settings
from .data_loader import versioned_collection_name
from .data_release import verify_data_release
from .repository import RuntimeRepository


class PublicRuntimeRepository(RuntimeRepository):
    def __init__(self, settings: Settings, public_settings: PublicSettings):
        super().__init__(settings)
        self.public_settings = public_settings
        self.qdrant_collection = versioned_collection_name(
            public_settings.qdrant_collection, public_settings.data_version
        )
        self.data_manifest: dict[str, Any] | None = None
        configured_data_root = str(os.getenv("PUBLIC_DATA_ROOT") or "").strip()
        if configured_data_root:
            expected_root = Path(configured_data_root).resolve()
            if (
                settings.data_root.resolve() != expected_root
                or settings.runtime_root.resolve() != expected_root
            ):
                raise RuntimeError(
                    "DATA_ROOT and APP_RUNTIME must match PUBLIC_DATA_ROOT"
                )
            self.data_manifest = verify_data_release(
                expected_root, public_settings.data_version
            )
        self._health_local = threading.local()
        self._request_context: ContextVar[dict[str, Any] | None] = ContextVar(
            f"project_snow_public_repository_{id(self)}", default=None
        )
        self._http_client = httpx.Client(follow_redirects=False)
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="snow-retrieval")
        self._neo4j_driver = None
        self._neo4j_lock = threading.RLock()

    def _collector(self) -> dict[str, Any] | None:
        return self._request_context.get()

    def _record_timing(self, name: str, started_at: float) -> None:
        collector = self._collector()
        if collector is None:
            return
        elapsed = max(0, int((time.perf_counter() - started_at) * 1000))
        with collector["lock"]:
            timings = collector["timings"]
            timings[name] = int(timings.get(name, 0)) + elapsed

    def request_diagnostics(self) -> dict[str, Any]:
        collector = self._collector()
        if collector is None:
            return {"timings_ms": {}, "dependency_health": self.request_health()}
        with collector["lock"]:
            return {
                "timings_ms": dict(collector["timings"]),
                "dependency_health": dict(collector["health"]),
            }

    def _set_health(self, service: str, status: str) -> None:
        collector = self._collector()
        if collector is not None:
            with collector["lock"]:
                collector["health"][service] = status
            return
        health = dict(getattr(self._health_local, "value", {}) or {})
        health[service] = status
        self._health_local.value = health

    def request_health(self) -> dict[str, str]:
        collector = self._collector()
        if collector is not None:
            with collector["lock"]:
                return dict(collector["health"])
        return dict(getattr(self._health_local, "value", {}) or {})

    def reset_request_health(self) -> None:
        self._health_local.value = {}
        self._request_context.set(
            {"lock": threading.RLock(), "health": {}, "timings": {}}
        )

    def close(self) -> None:
        self._http_client.close()
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._neo4j_lock:
            if self._neo4j_driver is not None:
                self._neo4j_driver.close()
                self._neo4j_driver = None

    def status(self) -> dict[str, bool]:
        status = super().status()
        runtime_root = self.settings.runtime_root
        status.update(
            {
                "character_views": (runtime_root / "mvp" / "character_views.jsonl").is_file(),
                "question_bank": (runtime_root / "mvp" / "question_bank.json").is_file(),
                "dialogue_profiles": (
                    runtime_root / "personas" / "dialogue_style_profiles.jsonl"
                ).is_file(),
            }
        )
        return status

    def lexical_search(self, query: str, limit: int = 40) -> list[tuple[str, int]]:
        started_at = time.perf_counter()
        try:
            return super().lexical_search(query, limit)
        finally:
            self._record_timing("fts5", started_at)

    def hybrid_search(
        self,
        query: str,
        character_id: str | None,
        limit: int,
    ) -> tuple[str, bool, list[dict[str, Any]]]:
        """Run independent lexical/vector legs concurrently, then preserve RRF."""

        started_at = time.perf_counter()
        lexical_context = copy_context()
        vector_context = copy_context()
        lexical_future = self._executor.submit(
            lexical_context.run, self.lexical_search, query, 40
        )
        vector_future = self._executor.submit(
            vector_context.run, self.vector_search, query, 40
        )
        lexical = lexical_future.result()
        vectors = vector_future.result()
        combined: dict[str, dict[str, float | int | None]] = {}
        for document_id, rank in lexical:
            combined.setdefault(
                document_id,
                {"score": 0.0, "lexical_rank": None, "vector_rank": None},
            )
            combined[document_id]["score"] = float(combined[document_id]["score"]) + 1 / (60 + rank)
            combined[document_id]["lexical_rank"] = rank
        for document_id, rank in vectors:
            combined.setdefault(
                document_id,
                {"score": 0.0, "lexical_rank": None, "vector_rank": None},
            )
            combined[document_id]["score"] = float(combined[document_id]["score"]) + 1 / (60 + rank)
            combined[document_id]["vector_rank"] = rank
        documents = self.documents_by_id()
        results: list[dict[str, Any]] = []
        for document_id, ranking in combined.items():
            document = documents.get(document_id)
            if document is None or not self._is_allowed_context(document, character_id):
                continue
            adjusted_score = float(ranking["score"]) * float(
                document["metadata"].get("source_priority", 0.5)
            )
            results.append(
                {
                    "citation": {
                        "document_id": document_id,
                        "page_id": document["page_id"],
                        "title": document["title"],
                        "source_type": document["source_type"],
                        "canonical_url": document.get("canonical_url"),
                        "local_path": document.get("local_path"),
                        "source_license": document.get("source_license"),
                    },
                    "text": document["text"],
                    "score": round(adjusted_score, 8),
                    "lexical_rank": ranking["lexical_rank"],
                    "vector_rank": ranking["vector_rank"],
                    "metadata": document["metadata"],
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        self._record_timing("hybrid_total", started_at)
        return ("rrf" if vectors else "lexical_only"), bool(vectors), results[:limit]

    def _embed_query(self, query: str) -> list[float] | None:
        started_at = time.perf_counter()
        try:
            response = self._http_client.post(
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
        finally:
            self._record_timing("embedding", started_at)

    def vector_search(self, query: str, limit: int = 40) -> list[tuple[str, int]]:
        query_vector = self._embed_query(query)
        if not query_vector:
            self._set_health("qdrant", "degraded")
            return []
        started_at = time.perf_counter()
        try:
            response = self._http_client.post(
                f"{self.public_settings.qdrant_url}/collections/{self.qdrant_collection}/points/search",
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
        finally:
            self._record_timing("qdrant", started_at)

    def serving_graph_context(
        self,
        query: str,
        character_id: str | None,
        intents: tuple[str, ...],
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        if not self.public_settings.neo4j_password:
            self._set_health("neo4j", "degraded")
            self._record_timing("neo4j", started_at)
            return {"status": "degraded", "nodes": [], "edges": []}
        try:
            from neo4j import GraphDatabase

            names = [query]
            if character_id:
                character = self.graph_nodes().get(f"character:{character_id}")
                if character and character.get("name"):
                    names.append(str(character["name"]))
            terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,12}", " ".join(names))[:8]]
            with self._neo4j_lock:
                if self._neo4j_driver is None:
                    self._neo4j_driver = GraphDatabase.driver(
                        self.public_settings.neo4j_uri,
                        auth=(self.public_settings.neo4j_user, self.public_settings.neo4j_password),
                        connection_timeout=5,
                    )
                driver = self._neo4j_driver
            with driver.session() as session:
                rows = session.run(
                        """
                        MATCH (start:SnowEntity {dataset_version: $data_version})
                        WHERE (start.node_id = $character_node_id
                           OR any(term IN $terms WHERE start.name CONTAINS term))
                        WITH DISTINCT start LIMIT 4
                        OPTIONAL MATCH path=(start)-[rels:SNOW_RELATION*1..2]-(other:SnowEntity)
                        WHERE other.dataset_version = start.dataset_version
                        RETURN start, other, relationships(path) AS rels LIMIT 16
                        """,
                        character_node_id=f"character:{character_id}" if character_id else "",
                        terms=terms,
                        data_version=self.public_settings.data_version,
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
            self._set_health("neo4j", "ok")
            return {"status": "ok", "nodes": list(nodes.values())[:12], "edges": list(edges.values())[:16]}
        except Exception:
            self._set_health("neo4j", "degraded")
            return {"status": "degraded", "nodes": [], "edges": []}
        finally:
            self._record_timing("neo4j", started_at)

    def dependency_health(self) -> dict[str, str]:
        health: dict[str, str] = {}
        for service, url in (
            ("embedding", f"{self.public_settings.embedding_url}/health"),
            (
                "qdrant",
                f"{self.public_settings.qdrant_url}/collections/{self.qdrant_collection}",
            ),
        ):
            try:
                response = self._http_client.get(
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

                with self._neo4j_lock:
                    if self._neo4j_driver is None:
                        self._neo4j_driver = GraphDatabase.driver(
                            self.public_settings.neo4j_uri,
                            auth=(self.public_settings.neo4j_user, self.public_settings.neo4j_password),
                            connection_timeout=5,
                        )
                    driver = self._neo4j_driver
                with driver.session() as session:
                    active = session.run(
                        "MATCH (node:SnowEntity {dataset_version: $data_version}) "
                        "RETURN count(node) AS nodes",
                        data_version=self.public_settings.data_version,
                    ).single()
                health["neo4j"] = "ok" if active and int(active["nodes"]) > 0 else "degraded"
            except Exception:
                health["neo4j"] = "degraded"
        else:
            health["neo4j"] = "degraded"
        return health
