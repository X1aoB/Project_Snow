from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.snow_app.data_loader import (
    _neo4j_edge,
    _neo4j_node,
    load_qdrant,
    qdrant_point_id,
    versioned_collection_name,
)
from backend.snow_app.data_release import DataReleaseError, verify_data_release
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from scripts.build_data_release import PUBLIC_CONTENT_LICENSE, build_release


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _build_fixture(root: Path, version: str = "2026.08.14.1") -> Path:
    runtime = root / "runtime"
    output_root = root / "releases"
    (runtime / "indexes").mkdir(parents=True)
    (runtime / "indexes" / "lexical.sqlite3").write_bytes(b"sqlite-fixture")
    documents = [
        {
            "document_id": "doc_a",
            "page_id": "page-a",
            "source_type": "story",
            "title": "标题 A",
            "text": "内容 A",
            "canonical_url": "https://example.test/a",
            "attribution": "Example Wiki",
            "source_license": "CC BY-NC-SA 4.0 unless page-specific notice applies",
            "source_manifest": "stories.jsonl",
            "source_content_hash": "hash-a",
            "local_path": "Data/Source/a.wikitext",
            "metadata": {"source_priority": 1.0},
        },
        {
            "document_id": "doc_b",
            "page_id": "page-b",
            "source_type": "story",
            "title": "标题 B",
            "text": "内容 B",
            "canonical_url": "https://example.test/b",
            "attribution": "Example Wiki",
            "source_license": "CC BY-NC-SA 4.0 unless page-specific notice applies",
            "source_manifest": "stories.jsonl",
            "source_content_hash": "hash-b",
            "local_path": "Data/Source/b.wikitext",
            "metadata": {"source_priority": 1.0},
        },
    ]
    _write_jsonl(runtime / "lakehouse" / "documents.jsonl", documents)
    _write_jsonl(
        runtime / "vectors" / "local_vectors.jsonl",
        [
            {"document_id": "doc_a", "vector": [0.1, 0.2]},
            {"document_id": "doc_b", "vector": [0.3, 0.4]},
        ],
    )
    _write_jsonl(
        runtime / "release" / "graph" / "nodes.jsonl",
        [
            {"node_id": "character:a", "node_type": "character", "name": "A", "attributes": {}},
            {"node_id": "event:b", "node_type": "event", "name": "B", "attributes": {}},
        ],
    )
    _write_jsonl(
        runtime / "release" / "graph" / "edges.jsonl",
        [
            {
                "edge_id": "edge-a-b",
                "from_id": "character:a",
                "to_id": "event:b",
                "relation_type": "PARTICIPATES_IN_EVENT",
                "evidence_page_ids": ["page-a"],
                "source_manifests": ["stories.jsonl"],
                "review_status": "verified",
            }
        ],
    )
    _write_jsonl(
        runtime / "personas" / "persona_profiles.jsonl",
        [{"character_id": "a", "character_name": "A"}],
    )
    _write_jsonl(
        runtime / "personas" / "dialogue_style_profiles.jsonl",
        [
            {"character_id": character.character_id, "character_name": character.display_name}
            for character in MVP_CHARACTERS
        ],
    )
    _write_jsonl(
        runtime / "mvp" / "character_views.jsonl",
        [
            {"character_id": character.character_id, "character_name": character.display_name}
            for character in MVP_CHARACTERS
        ],
    )
    (runtime / "mvp" / "question_bank.json").write_text(
        json.dumps(
            {
                "questions": [
                    {"character_id": character.character_id, "question": "测试问题"}
                    for character in MVP_CHARACTERS
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    denylist = root / "denylist.json"
    denylist.write_text("{}", encoding="utf-8")
    build_release(version, runtime, output_root, denylist, enforce_disk_guard=False)
    return output_root / version


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeQdrantClient:
    def __init__(self, alias: str):
        self.alias = alias
        self.alias_target = f"{alias}__previous"
        self.collections = {self.alias_target: 2, f"{alias}__stale": 1}
        self.alias_actions: list[dict] = []
        self.deleted: list[str] = []

    def get(self, path: str) -> FakeResponse:
        if path == "/healthz":
            return FakeResponse()
        if path == "/aliases":
            aliases = (
                [{"alias_name": self.alias, "collection_name": self.alias_target}]
                if self.alias_target
                else []
            )
            return FakeResponse(payload={"result": {"aliases": aliases}})
        if path == "/collections":
            return FakeResponse(
                payload={"result": {"collections": [{"name": name} for name in self.collections]}}
            )
        if path.startswith("/collections/"):
            name = path.rsplit("/", 1)[-1]
            if name not in self.collections:
                return FakeResponse(404)
            return FakeResponse(payload={"result": {"points_count": self.collections[name]}})
        raise AssertionError(path)

    def put(self, path: str, *, json: dict, params: dict | None = None) -> FakeResponse:
        parts = path.split("/")
        name = parts[2]
        if path.endswith("/points"):
            self.collections[name] += len(json["points"])
        else:
            self.collections[name] = 0
        return FakeResponse()

    def post(self, path: str, *, json: dict) -> FakeResponse:
        self.alias_actions = list(json["actions"])
        for action in self.alias_actions:
            if "delete_alias" in action:
                self.alias_target = ""
            if "create_alias" in action:
                self.alias_target = action["create_alias"]["collection_name"]
        return FakeResponse()

    def delete(self, path: str) -> FakeResponse:
        name = path.rsplit("/", 1)[-1]
        if name not in self.collections:
            return FakeResponse(404)
        self.deleted.append(name)
        del self.collections[name]
        return FakeResponse()


class DataReleaseTests(TestCase):
    def test_builds_exact_serving_layout_and_unspecified_license(self) -> None:
        with TemporaryDirectory() as directory:
            release = _build_fixture(Path(directory))
            manifest = verify_data_release(release, "2026.08.14.1")
            self.assertEqual(manifest["statistics"]["documents"], 2)
            self.assertEqual(manifest["statistics"]["vector_dimension"], 2)
            self.assertEqual(manifest["statistics"]["character_views"], 22)
            self.assertEqual(manifest["statistics"]["dialogue_profiles"], 22)
            for relative in (
                "indexes/lexical.sqlite3",
                "lakehouse/documents.jsonl",
                "vectors/local_vectors.jsonl",
                "graph/nodes.jsonl",
                "graph/edges.jsonl",
                "personas/persona_profiles.jsonl",
                "personas/dialogue_style_profiles.jsonl",
                "mvp/character_views.jsonl",
                "mvp/question_bank.json",
                "ATTRIBUTION.jsonl",
                "LICENSES.json",
            ):
                self.assertTrue((release / relative).is_file(), relative)
            published_document = json.loads(
                (release / "lakehouse" / "documents.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(published_document["source_license"], PUBLIC_CONTENT_LICENSE)
            self.assertNotIn("4.0", (release / "LICENSES.json").read_text(encoding="utf-8"))

    def test_verifier_rejects_tampering(self) -> None:
        with TemporaryDirectory() as directory:
            release = _build_fixture(Path(directory))
            (release / "graph" / "nodes.jsonl").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(DataReleaseError, "mismatch"):
                verify_data_release(release)

    def test_qdrant_load_switches_alias_and_keeps_previous(self) -> None:
        with TemporaryDirectory() as directory:
            version = "2026.08.14.1"
            release = _build_fixture(Path(directory), version)
            client = FakeQdrantClient("project_snow_documents")
            result = load_qdrant(
                release,
                version,
                "http://qdrant",
                "project_snow_documents",
                "test-key",
                client=client,
            )
            self.assertEqual(result["points"], 2)
            self.assertEqual(client.alias_target, result["collection"])
            self.assertIn("project_snow_documents__stale", client.deleted)
            self.assertIn("project_snow_documents__previous", client.collections)
            reused = load_qdrant(
                release,
                version,
                "http://qdrant",
                "project_snow_documents",
                "test-key",
                client=client,
            )
            self.assertTrue(reused["reused"])

    def test_versioned_ids_and_neo4j_rows_are_stable(self) -> None:
        self.assertEqual(qdrant_point_id("doc_a"), qdrant_point_id("doc_a"))
        collection = versioned_collection_name("project_snow_documents", "2026.08.14.1")
        self.assertTrue(collection.startswith("project_snow_documents__2026-08-14-1-"))
        node = _neo4j_node("v1", {"node_id": "character:a", "node_type": "character"})
        edge = _neo4j_edge("v1", {"edge_id": "edge", "from_id": "character:a", "to_id": "event:b"})
        self.assertEqual(node["dataset_key"], "v1\x1fcharacter:a")
        self.assertEqual(edge["from_key"], "v1\x1fcharacter:a")
