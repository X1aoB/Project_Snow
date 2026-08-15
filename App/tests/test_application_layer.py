from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.snow_app.repository as snow_repository
from backend.snow_app.chat_store import ConversationStore
from backend.snow_app.config import Settings
from backend.snow_app.contracts import MVPChatRequest
from backend.snow_app.main import app, get_repository, repository
from backend.snow_app.mvp_policy import MVP_CHARACTERS, question_bank
from backend.snow_app.mvp_service import (
    MVPProviderError,
    MVPRequestInProgress,
    MVPService,
    _parse_model_json,
)
from backend.snow_app.repository import RuntimeRepository
from pipelines.benchmark_relation_extraction import _balanced_sample, _completed_before
from pipelines.build_entity_node_candidates import discover_entity_node_candidates
from pipelines.build_graph import _relation_job, _split_relation_evidence_documents
from pipelines.build_lexical_index import search_terms
from pipelines.common import NON_DIALOGUE_CHARACTER_IDS, dialogue_characters
import pipelines.common as common
import pipelines.extract_relation_candidates as relation_extraction
import pipelines.review_relation_candidates as independent_relation_review
from pipelines.extract_relation_candidates import (
    ProviderCallFailure,
    _call_provider_with_retry,
    _parse_relation_payload,
    _provider_timeout_seconds,
    load_relation_environment,
    resolve_no_eligible_relation,
    validate_relation_candidate,
)
from pipelines.review_relation_candidates import validate_review_response


class ApplicationLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._mvp_store_temp = tempfile.TemporaryDirectory()
        cls._mvp_store_environment = patch.dict(
            os.environ,
            {
                "MVP_CHAT_DATABASE_PATH": str(
                    Path(cls._mvp_store_temp.name) / "conversations.sqlite3"
                )
            },
        )
        cls._mvp_store_environment.start()
        cls.settings = Settings.from_environment()
        cls.repository = RuntimeRepository(cls.settings)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._mvp_store_environment.stop()
        cls._mvp_store_temp.cleanup()

    def _review_repository(
        self,
        runtime_root: Path,
        candidates: list[dict[str, object]],
        documents: list[dict[str, object]],
        nodes: list[dict[str, object]],
    ) -> RuntimeRepository:
        for directory in ("lakehouse", "graph", "review"):
            (runtime_root / directory).mkdir(parents=True, exist_ok=True)
        (runtime_root / "lakehouse" / "documents.jsonl").write_text(
            "".join(json.dumps(document) + "\n" for document in documents), encoding="utf-8"
        )
        (runtime_root / "graph" / "nodes.jsonl").write_text(
            "".join(json.dumps(node) + "\n" for node in nodes), encoding="utf-8"
        )
        (runtime_root / "graph" / "edges.jsonl").write_text("", encoding="utf-8")
        (runtime_root / "review" / "narrative_relation_candidates.jsonl").write_text(
            "".join(json.dumps(candidate) + "\n" for candidate in candidates), encoding="utf-8"
        )
        (runtime_root / "review" / "narrative_relation_jobs.jsonl").write_text("", encoding="utf-8")
        settings = Settings(
            data_root=self.settings.data_root,
            runtime_root=runtime_root,
            chat_enabled=False,
            embedding_model=self.settings.embedding_model,
            allowed_origins=self.settings.allowed_origins,
        )
        return RuntimeRepository(settings)

    def test_query_embedding_falls_back_without_network_when_model_is_not_local(self) -> None:
        """A missing optional embedding model must never stall chat retrieval."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            vectors_path = runtime_root / "vectors" / "local_vectors.jsonl"
            vectors_path.parent.mkdir(parents=True, exist_ok=True)
            vectors_path.write_text('{"document_id":"doc","vector":[0.1]}\n', encoding="utf-8")
            settings = Settings(
                data_root=self.settings.data_root,
                runtime_root=runtime_root,
                chat_enabled=False,
                embedding_model="missing-local-model",
                allowed_origins=self.settings.allowed_origins,
            )
            repository = RuntimeRepository(settings)
            attempts: list[dict[str, object]] = []

            class MissingLocalModel:
                def __init__(self, _model_name: str, **kwargs: object) -> None:
                    attempts.append(kwargs)
                    raise OSError("model is not installed locally")

            fake_sentence_transformers = ModuleType("sentence_transformers")
            fake_sentence_transformers.SentenceTransformer = MissingLocalModel
            with patch.dict(sys.modules, {"sentence_transformers": fake_sentence_transformers}):
                self.assertIsNone(repository._embed_query("hello"))
                self.assertIsNone(repository._embed_query("again"))
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0], {"local_files_only": True})

                repository.clear_caches()
                self.assertIsNone(repository._embed_query("after reload"))
                self.assertEqual(len(attempts), 2)

    def test_relation_review_groups_preserve_all_evidence_without_alias_merging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_one",
                    "page_id": "page_one",
                    "title": "First evidence",
                    "source_type": "character_story",
                    "text": "Alice explicitly trusts Bob.",
                },
                {
                    "document_id": "doc_two",
                    "page_id": "page_two",
                    "title": "Second evidence",
                    "source_type": "affinity_story",
                    "text": "Alice says Bob is a reliable partner.",
                },
                {
                    "document_id": "doc_three",
                    "page_id": "page_three",
                    "title": "Different literal entity",
                    "source_type": "character_story",
                    "text": "Alicia explicitly trusts Bob.",
                },
            ]
            candidates = [
                {
                    "candidate_id": "candidate_one",
                    "subject": "Alice",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "Bob",
                    "confidence": 0.9,
                    "evidence_quote": "Alice explicitly trusts Bob.",
                    "evidence_document_ids": ["doc_one"],
                    "source_type": "character_story",
                    "review_status": "pending_review",
                },
                {
                    "candidate_id": "candidate_two",
                    "subject": " Alice ",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "Bob!",
                    "confidence": 0.8,
                    "evidence_quote": "Alice says Bob is a reliable partner.",
                    "evidence_document_ids": ["doc_two"],
                    "source_type": "affinity_story",
                    "review_status": "pending_review",
                },
                {
                    "candidate_id": "candidate_three",
                    "subject": "Alicia",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "Bob",
                    "confidence": 0.9,
                    "evidence_quote": "Alicia explicitly trusts Bob.",
                    "evidence_document_ids": ["doc_three"],
                    "source_type": "character_story",
                    "review_status": "pending_review",
                },
            ]
            nodes = [
                {"node_id": "character:alice", "node_type": "character", "name": "Alice"},
                {"node_id": "character:alicia", "node_type": "character", "name": "Alicia"},
                {"node_id": "character:bob", "node_type": "character", "name": "Bob"},
                {"node_id": "page:alice", "node_type": "page", "name": "Alice"},
            ]
            repository = self._review_repository(runtime_root, candidates, documents, nodes)

            result = repository.relation_review_groups(limit=10)
            self.assertEqual(result["total"], 2)
            grouped = next(group for group in result["groups"] if group["candidate_count"] == 2)
            self.assertEqual(grouped["evidence_document_count"], 2)
            self.assertEqual(grouped["evidence_page_count"], 2)
            self.assertEqual(grouped["mapping_status"], "exact_unique_match_available")
            self.assertEqual(grouped["mapping_suggestions"]["subject"][0]["node_id"], "character:alice")
            self.assertEqual(len(grouped["mapping_suggestions"]["subject"]), 1)

            detail = repository.relation_review_group_detail(grouped["review_group_id"])
            self.assertIsNotNone(detail)
            self.assertEqual(len(detail["candidates"]), 2)
            self.assertEqual({item["document_id"] for candidate in detail["candidates"] for item in candidate["evidence"]}, {"doc_one", "doc_two"})
            paged_detail = repository.relation_review_group_detail(grouped["review_group_id"], candidate_limit=1)
            self.assertEqual(paged_detail["candidate_total"], 2)
            self.assertEqual(len(paged_detail["candidates"]), 1)
            self.assertEqual(
                repository.relation_review_group_detail(grouped["review_group_id"], candidate_limit=1, candidate_offset=1)["candidate_offset"],
                1,
            )

    def test_entity_node_discovery_requires_literal_unambiguous_endpoint_evidence(self) -> None:
        documents = {
            "doc_location": {
                "document_id": "doc_location",
                "page_id": "page_location",
                "title": "Location evidence",
                "source_type": "main_story",
                "text": "Alice arrives at the Tower to meet Bob.",
            },
            "doc_inferred": {
                "document_id": "doc_inferred",
                "page_id": "page_inferred",
                "title": "Inferred event",
                "source_type": "main_story",
                "text": "Alice asks for help.",
            },
            "doc_generic_event": {
                "document_id": "doc_generic_event",
                "page_id": "page_generic_event",
                "title": "Generic activity",
                "source_type": "main_story",
                "text": "Alice participates in a 约会.",
            },
        }
        candidates = [
            {
                "candidate_id": "candidate_location",
                "subject": "Alice",
                "relation_type": "VISITS_LOCATION",
                "object": "Tower",
                "evidence_quote": "Alice arrives at the Tower to meet Bob.",
                "evidence_document_ids": ["doc_location"],
                "source_type": "main_story",
                "review_status": "pending_review",
            },
            {
                "candidate_id": "candidate_inferred_event",
                "subject": "Alice",
                "relation_type": "PARTICIPATES_IN_EVENT",
                "object": "Rescue mission",
                "evidence_quote": "Alice asks for help.",
                "evidence_document_ids": ["doc_inferred"],
                "source_type": "main_story",
                "review_status": "pending_review",
            },
            {
                "candidate_id": "candidate_generic_event",
                "subject": "Alice",
                "relation_type": "PARTICIPATES_IN_EVENT",
                "object": "约会",
                "evidence_quote": "Alice participates in a 约会.",
                "evidence_document_ids": ["doc_generic_event"],
                "source_type": "main_story",
                "review_status": "pending_review",
            },
        ]
        nodes = [{"node_id": "character:alice", "node_type": "character", "name": "Alice"}]

        entity_candidates, skipped = discover_entity_node_candidates(candidates, documents, nodes)

        self.assertEqual(len(entity_candidates), 1)
        self.assertEqual(entity_candidates[0]["entity_name"], "Tower")
        self.assertEqual(entity_candidates[0]["proposed_node_type"], "location")
        self.assertEqual(entity_candidates[0]["relation_candidate_ids"], ["candidate_location"])
        self.assertEqual(skipped["object_not_literal_in_evidence_quote"], 1)
        self.assertEqual(skipped["generic_event_label"], 1)

    def test_relation_review_exposes_advisory_machine_report_without_auto_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_machine",
                    "page_id": "page_machine",
                    "title": "Machine evidence",
                    "source_type": "main_story",
                    "text": "Alice explicitly calls Bob her trusted partner.",
                }
            ]
            candidates = [
                {
                    "candidate_id": "candidate_machine",
                    "subject": "Alice",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "Bob",
                    "confidence": 0.9,
                    "evidence_quote": "Alice explicitly calls Bob her trusted partner.",
                    "evidence_document_ids": ["doc_machine"],
                    "source_type": "main_story",
                    "review_status": "pending_review",
                }
            ]
            nodes = [
                {"node_id": "character:alice", "node_type": "character", "name": "Alice"},
                {"node_id": "character:bob", "node_type": "character", "name": "Bob"},
            ]
            repository = self._review_repository(runtime_root, candidates, documents, nodes)
            report = {
                "candidate_id": "candidate_machine",
                "review_status": "completed",
                "verdict": "recommend_approve",
                "audit_eligible": True,
                "reviewed_at": "2026-07-26T12:00:00+00:00",
                "model_reviewer": {"provider": "openai-compatible", "model": "independent-model"},
            }
            (runtime_root / "review" / "relation_model_review_reports.jsonl").write_text(
                json.dumps(report) + "\n", encoding="utf-8"
            )

            groups = repository.relation_review_groups(limit=10)
            self.assertEqual(groups["groups"][0]["machine_review"]["group_verdict"], "recommend_approve")
            detail = repository.relation_review_group_detail(groups["groups"][0]["review_group_id"])
            self.assertEqual(detail["candidates"][0]["machine_review"]["verdict"], "recommend_approve")
            summary = repository.relation_machine_review_summary()
            self.assertEqual(summary["completed_candidate_count"], 1)
            self.assertEqual(summary["audit_eligible_candidate_count"], 1)
            stored_candidate = json.loads(
                (runtime_root / "review" / "narrative_relation_candidates.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(stored_candidate["review_status"], "pending_review")

    def test_relation_review_triage_is_deterministic_and_mentions_are_low_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_high",
                    "page_id": "page_high",
                    "title": "High",
                    "source_type": "main_story",
                    "text": "Alice and Bob are trusted allies.",
                },
                {
                    "document_id": "doc_normal",
                    "page_id": "page_normal",
                    "title": "Normal",
                    "source_type": "special_mail",
                    "text": "Alice arrived at Harbor.",
                },
                {
                    "document_id": "doc_low",
                    "page_id": "page_low",
                    "title": "Low",
                    "source_type": "main_story",
                    "text": "Alice mentioned Harbor.",
                },
            ]
            candidates = [
                {
                    "candidate_id": "high",
                    "subject": "Alice",
                    "relation_type": "ALLY_OF",
                    "object": "Bob",
                    "confidence": 0.9,
                    "evidence_quote": "Alice and Bob are trusted allies.",
                    "evidence_document_ids": ["doc_high"],
                    "source_type": "main_story",
                    "review_status": "pending_review",
                },
                {
                    "candidate_id": "normal",
                    "subject": "Alice",
                    "relation_type": "VISITS_LOCATION",
                    "object": "Harbor",
                    "confidence": 0.9,
                    "evidence_quote": "Alice arrived at Harbor.",
                    "evidence_document_ids": ["doc_normal"],
                    "source_type": "special_mail",
                    "review_status": "pending_review",
                },
                {
                    "candidate_id": "low",
                    "subject": "Alice",
                    "relation_type": "MENTIONS",
                    "object": "Harbor",
                    "confidence": 0.9,
                    "evidence_quote": "Alice mentioned Harbor.",
                    "evidence_document_ids": ["doc_low"],
                    "source_type": "main_story",
                    "review_status": "pending_review",
                },
            ]
            nodes = [
                {"node_id": "character:alice", "node_type": "character", "name": "Alice"},
                {"node_id": "character:bob", "node_type": "character", "name": "Bob"},
                {"node_id": "location:harbor", "node_type": "location", "name": "Harbor"},
            ]
            repository = self._review_repository(runtime_root, candidates, documents, nodes)

            first = repository.relation_review_groups(limit=10)
            second = repository.relation_review_groups(limit=10)
            self.assertEqual(
                [group["review_group_id"] for group in first["groups"]],
                [group["review_group_id"] for group in second["groups"]],
            )
            self.assertEqual([group["priority_tier"] for group in first["groups"]], ["high", "normal", "low"])
            self.assertEqual(first["groups"][-1]["relation_type"], "MENTIONS")

            sample_one = repository.relation_review_audit_sample(size=3, seed="stable-audit")
            sample_two = repository.relation_review_audit_sample(size=3, seed="stable-audit")
            self.assertEqual(
                [group["review_group_id"] for group in sample_one["groups"]],
                [group["review_group_id"] for group in sample_two["groups"]],
            )
            self.assertEqual({group["priority_tier"] for group in sample_one["groups"]}, {"high", "normal", "low"})

    def test_relation_review_group_and_sample_endpoints_do_not_mutate_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_api",
                    "page_id": "page_api",
                    "title": "API evidence",
                    "source_type": "main_story",
                    "text": "Alice and Bob are allies.",
                }
            ]
            candidates = [
                {
                    "candidate_id": "candidate_api",
                    "subject": "Alice",
                    "relation_type": "ALLY_OF",
                    "object": "Bob",
                    "confidence": 0.9,
                    "evidence_quote": "Alice and Bob are allies.",
                    "evidence_document_ids": ["doc_api"],
                    "source_type": "main_story",
                    "review_status": "pending_review",
                }
            ]
            nodes = [
                {"node_id": "character:alice", "node_type": "character", "name": "Alice"},
                {"node_id": "character:bob", "node_type": "character", "name": "Bob"},
            ]
            local_repository = self._review_repository(runtime_root, candidates, documents, nodes)
            app.dependency_overrides[get_repository] = lambda: local_repository
            try:
                client = TestClient(app)
                groups = client.get("/api/v1/review/relations/groups?limit=10")
                unreviewed_groups = client.get("/api/v1/review/relations/groups?limit=10&machine_verdict=unreviewed")
                sample = client.get("/api/v1/review/relations/audit-sample?size=1&seed=api-test")
                invalid_tier = client.get("/api/v1/review/relations/groups?tier=unsafe")
                invalid_machine_verdict = client.get("/api/v1/review/relations/groups?machine_verdict=unsafe")
            finally:
                app.dependency_overrides.clear()

            self.assertEqual(groups.status_code, 200)
            self.assertEqual(groups.json()["total"], 1)
            self.assertEqual(unreviewed_groups.status_code, 200)
            self.assertEqual(unreviewed_groups.json()["total"], 1)
            self.assertEqual(sample.status_code, 200)
            self.assertEqual(sample.json()["sample_size"], 1)
            self.assertEqual(invalid_tier.status_code, 422)
            self.assertEqual(invalid_machine_verdict.status_code, 422)
            stored = [
                json.loads(line)
                for line in (runtime_root / "review" / "narrative_relation_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(stored[0]["review_status"], "pending_review")

    def test_cjk_terms_include_bigrams(self) -> None:
        self.assertIn("琴诺", search_terms("琴诺对分析员的称呼"))
        self.assertIn("分析", search_terms("琴诺对分析员的称呼"))

    def test_benchmark_sample_is_balanced_and_non_destructive(self) -> None:
        jobs = [
            {"job_id": "story_a", "source_type": "character_story"},
            {"job_id": "story_b", "source_type": "character_story"},
            {"job_id": "mail_a", "source_type": "special_mail"},
            {"job_id": "mail_b", "source_type": "special_mail"},
        ]
        selected = _balanced_sample(jobs, 4)
        self.assertEqual([job["source_type"] for job in selected], ["character_story", "special_mail", "character_story", "special_mail"])
        self.assertEqual(len(jobs), 4)

    def test_benchmark_completed_before_filter_excludes_post_cutover_jobs(self) -> None:
        jobs = [
            {"job_id": "before", "status": "completed", "completed_at": "2026-07-26T03:00:00+00:00"},
            {"job_id": "after", "status": "completed", "completed_at": "2026-07-26T04:00:00+00:00"},
            {"job_id": "failed", "status": "failed", "completed_at": "2026-07-26T02:00:00+00:00"},
        ]
        selected = _completed_before(jobs, "2026-07-26T03:21:28+00:00")
        self.assertEqual([job["job_id"] for job in selected], ["before"])

    def test_runtime_artifact_write_retries_temporary_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "artifact.jsonl"
            real_replace = os.replace
            calls: list[int] = []

            def locked_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                calls.append(1)
                if len(calls) < 3:
                    raise PermissionError("temporary reader lock")
                real_replace(source, destination)

            with patch("pipelines.common.os.replace", side_effect=locked_replace), patch("pipelines.common.time.sleep") as sleep:
                common.write_jsonl(path, [{"id": "row"}])
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"id": "row"})

    def test_review_artifact_write_retries_temporary_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "review.jsonl"
            real_replace = os.replace
            calls: list[int] = []

            def locked_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError("temporary reader lock")
                real_replace(source, destination)

            with patch("backend.snow_app.repository.os.replace", side_effect=locked_replace), patch(
                "backend.snow_app.repository.time.sleep"
            ) as sleep:
                snow_repository._write_jsonl(path, [{"id": "review-row"}])
            self.assertEqual(len(calls), 2)
            self.assertEqual(sleep.call_count, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"id": "review-row"})

    def test_no_relation_resolution_creates_auditable_non_graph_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            review_root = runtime_root / "review"
            review_root.mkdir()
            (review_root / "narrative_relation_jobs.jsonl").write_text(
                json.dumps(
                    {
                        "job_id": "job_no_relation",
                        "page_id": "page_no_relation",
                        "source_type": "random_event",
                        "evidence_document_ids": ["doc_no_relation"],
                        "status": "failed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(relation_extraction, "RUNTIME_ROOT", runtime_root):
                result = resolve_no_eligible_relation("job_no_relation", "test-reviewer", "No explicit durable relation.")
            job = json.loads((review_root / "narrative_relation_jobs.jsonl").read_text(encoding="utf-8"))
            resolution = json.loads((review_root / "relation_job_resolutions.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(result["job_id"], "job_no_relation")
            self.assertEqual(job["status"], "completed_no_relation")
            self.assertEqual(resolution["resolution"], "human_verified_no_eligible_relation")

    def test_relation_payload_parser_accepts_json_fences_and_diagnoses_empty_content(self) -> None:
        self.assertEqual(_parse_relation_payload("```json\n{\"relations\": []}\n```", "stop", ""), {"relations": []})
        with self.assertRaisesRegex(ValueError, "empty assistant content"):
            _parse_relation_payload("", "stop", "reasoning text")

    def test_relation_environment_loader_uses_untracked_file_without_overriding_process_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_path = Path(temporary_directory) / ".env"
            env_path.write_text(
                "OPENAI_COMPATIBLE_BASE_URL=https://example.invalid/v1\n"
                "OPENAI_COMPATIBLE_API_KEY=private-value\n"
                "DASHSCOPE_API_KEY=official-private-value\n"
                "RELATION_REVIEW_API_KEY=independent-private-value\n"
                "UNRELATED_VALUE=ignored\n",
                encoding="utf-8",
            )
            environment = {"OPENAI_COMPATIBLE_BASE_URL": "https://process.example/v1"}
            load_relation_environment(env_path, environment)
            self.assertEqual(environment["OPENAI_COMPATIBLE_BASE_URL"], "https://process.example/v1")
            self.assertEqual(environment["OPENAI_COMPATIBLE_API_KEY"], "private-value")
            self.assertEqual(environment["DASHSCOPE_API_KEY"], "official-private-value")
            self.assertEqual(environment["RELATION_REVIEW_API_KEY"], "independent-private-value")
            self.assertNotIn("UNRELATED_VALUE", environment)

    def test_independent_review_response_requires_literal_evidence(self) -> None:
        candidate = {
            "candidate_id": "candidate_independent_review",
            "subject": "Alice",
            "relation_type": "HAS_RELATIONSHIP_CONTEXT",
            "object": "Bob",
            "evidence_document_ids": ["doc_evidence"],
        }
        documents = {
            "doc_evidence": {
                "document_id": "doc_evidence",
                "text": "Alice explicitly calls Bob her trusted partner.",
            }
        }
        valid = validate_review_response(
            {
                "candidate_id": "candidate_independent_review",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Alice explicitly calls Bob her trusted partner.",
                "verdict_rationale": "The direct statement names both parties and their relationship.",
            },
            candidate,
            documents,
        )
        self.assertEqual(valid["verdict"], "recommend_approve")
        self.assertTrue(valid["audit_eligible"])

        mail_candidate = {**candidate, "source_type": "special_mail"}
        mail_context = validate_review_response(
            {
                "candidate_id": "candidate_independent_review",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Alice explicitly calls Bob her trusted partner.",
                "verdict_rationale": "The literal wording is direct but comes from a scene-bound mail.",
            },
            mail_candidate,
            documents,
        )
        self.assertEqual(mail_context["verdict"], "abstain")
        self.assertIn("context_sensitive_source_requires_human_review", mail_context["validation_flags"])
        self.assertFalse(mail_context["audit_eligible"])

        invalid_quote = validate_review_response(
            {
                "candidate_id": "candidate_independent_review",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Alice and Bob are lifelong friends.",
                "verdict_rationale": "Unsupported quote should be rejected locally.",
            },
            candidate,
            documents,
        )
        self.assertEqual(invalid_quote["verdict"], "abstain")
        self.assertIn("approve_quote_not_found", invalid_quote["validation_flags"])
        self.assertFalse(invalid_quote["audit_eligible"])

        pronoun_or_context_subject = validate_review_response(
            {
                "candidate_id": "candidate_independent_review",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Bob her trusted partner.",
                "verdict_rationale": "The subject is only available from surrounding context.",
            },
            candidate,
            documents,
        )
        self.assertEqual(pronoun_or_context_subject["verdict"], "abstain")
        self.assertIn("approve_subject_not_in_quote", pronoun_or_context_subject["validation_flags"])
        self.assertFalse(pronoun_or_context_subject["audit_eligible"])

        name_only_candidate = {**candidate, "evidence_document_ids": ["doc_name_only"]}
        name_only_documents = {
            "doc_name_only": {
                "document_id": "doc_name_only",
                "text": "Alice greets Bob before the meeting.",
            }
        }
        name_only = validate_review_response(
            {
                "candidate_id": "candidate_independent_review",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Alice greets Bob before the meeting.",
                "verdict_rationale": "The names co-occur but the relationship itself is not stated.",
            },
            name_only_candidate,
            name_only_documents,
        )
        self.assertEqual(name_only["verdict"], "abstain")
        self.assertIn("approve_relation_predicate_not_in_quote", name_only["validation_flags"])
        self.assertFalse(name_only["audit_eligible"])

    def test_independent_review_checkpoints_without_mutating_candidate_or_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            review_root = runtime_root / "review"
            review_root.mkdir(parents=True)
            candidate = {
                "candidate_id": "candidate_checkpoint",
                "subject": "Alice",
                "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                "object": "Bob",
                "source_type": "character_story",
                "evidence_quote": "Alice explicitly calls Bob her trusted partner.",
                "evidence_document_ids": ["doc_evidence"],
                "review_status": "pending_review",
            }
            candidate_path = review_root / "narrative_relation_candidates.jsonl"
            candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
            document = {
                "document_id": "doc_evidence",
                "page_id": "page_evidence",
                "title": "Evidence",
                "source_type": "character_story",
                "text": "Alice explicitly calls Bob her trusted partner.",
                "metadata": {},
            }
            response = {
                "candidate_id": "candidate_checkpoint",
                "verdict": "recommend_approve",
                "evidence_sufficiency": "direct",
                "relation_type_valid": True,
                "identity_mapping_confidence": "exact_literal",
                "temporal_scope": "stable",
                "risk_flags": [],
                "supporting_quote": "Alice explicitly calls Bob her trusted partner.",
                "verdict_rationale": "Direct evidence supports the literal relationship.",
            }
            environment = {
                "RELATION_REVIEW_PROVIDER": "openai-compatible",
                "RELATION_REVIEW_BASE_URL": "https://example.invalid/v1",
                "RELATION_REVIEW_API_KEY": "private-value",
                "RELATION_REVIEW_MODEL": "independent-model",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(independent_relation_review, "RUNTIME_ROOT", runtime_root),
                patch.object(independent_relation_review, "load_runtime_jsonl", return_value=[document]),
                patch.object(
                    independent_relation_review,
                    "_call_reviewer_with_retry",
                    return_value=(response, {"total_tokens": 11}, {"attempts": 1, "retries": 0}),
                ) as provider_call,
            ):
                first = independent_relation_review.review(limit=1, run_name="test-review")
                second = independent_relation_review.review(limit=1, run_name="test-review")

            self.assertEqual(first["processed"], 1)
            self.assertEqual(first["failed"], 0)
            self.assertEqual(second["attempted"], 0)
            self.assertEqual(provider_call.call_count, 1)
            saved_candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_candidate["review_status"], "pending_review")
            reports = [
                json.loads(line)
                for line in (review_root / "relation_model_review_reports.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["verdict"], "recommend_approve")
            self.assertTrue(reports[0]["audit_eligible"])

    def test_relation_provider_retries_transient_failure(self) -> None:
        calls: list[int] = []
        delays: list[float] = []

        def flaky_provider(*_: str) -> dict[str, object]:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("temporary network timeout")
            return {"relations": []}

        with patch.dict(
            os.environ,
            {"RELATION_CANDIDATE_MAX_ATTEMPTS": "3", "RELATION_CANDIDATE_RETRY_BACKOFF_SECONDS": "0.5"},
            clear=False,
        ):
            response, metadata = _call_provider_with_retry(
                "https://example.invalid/v1", "private-value", "model", "{}", provider_call=flaky_provider, sleep=delays.append
            )
        self.assertEqual(response, {"relations": []})
        self.assertEqual(metadata, {"attempts": 2, "retries": 1})
        self.assertEqual(delays, [0.5])

    def test_relation_provider_retries_gateway_timeout(self) -> None:
        calls: list[int] = []

        def flaky_provider(*_: str) -> dict[str, object]:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Provider HTTP 524; gateway timeout")
            return {"relations": []}

        response, metadata = _call_provider_with_retry(
            "https://example.invalid/v1", "private-value", "model", "{}", provider_call=flaky_provider, sleep=lambda _: None
        )
        self.assertEqual(response, {"relations": []})
        self.assertEqual(metadata, {"attempts": 2, "retries": 1})

    def test_relation_evidence_is_split_without_losing_order(self) -> None:
        documents = [
            {"document_id": "doc_a", "text": "a" * 1_800},
            {"document_id": "doc_b", "text": "b" * 1_800},
            {"document_id": "doc_c", "text": "c" * 900},
        ]
        segments = _split_relation_evidence_documents(documents, max_evidence_chars=4_000)
        self.assertEqual([[item["document_id"] for item in segment] for segment in segments], [["doc_a", "doc_b"], ["doc_c"]])

    def test_relation_segment_records_its_parent_job(self) -> None:
        job = _relation_job(
            "page_a", "main_story", {}, [{"document_id": "doc_a", "text": "evidence"}], 1, 2, "relation_job_parent"
        )
        self.assertEqual(job["parent_job_id"], "relation_job_parent")

    def test_relation_provider_uses_longer_timeout_for_large_evidence(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RELATION_CANDIDATE_TIMEOUT_SECONDS": "90",
                "RELATION_CANDIDATE_LONG_PROMPT_TIMEOUT_SECONDS": "180",
                "RELATION_CANDIDATE_LONG_PROMPT_CHARS": "6000",
            },
            clear=False,
        ):
            self.assertEqual(_provider_timeout_seconds("x" * 5999), 90.0)
            self.assertEqual(_provider_timeout_seconds("x" * 6000), 180.0)

    def test_relation_extraction_checkpoints_completed_and_failed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            review_root = runtime_root / "review"
            review_root.mkdir()
            jobs = [
                {
                    "job_id": "job_success",
                    "page_id": "page_success",
                    "source_type": "character_story",
                    "status": "queued",
                    "allowed_relation_types": ["HAS_RELATIONSHIP_CONTEXT"],
                    "character_context": {},
                    "evidence_document_ids": ["doc_evidence"],
                },
                {
                    "job_id": "job_failure",
                    "page_id": "page_failure",
                    "source_type": "character_story",
                    "status": "queued",
                    "allowed_relation_types": ["HAS_RELATIONSHIP_CONTEXT"],
                    "character_context": {},
                    "evidence_document_ids": ["doc_evidence"],
                },
            ]
            (review_root / "narrative_relation_jobs.jsonl").write_text(
                "".join(json.dumps(job) + "\n" for job in jobs), encoding="utf-8"
            )
            (review_root / "narrative_relation_candidates.jsonl").write_text("", encoding="utf-8")
            document = {
                "document_id": "doc_evidence",
                "title": "Evidence",
                "source_type": "character_story",
                "text": "Alice trusts Bob completely.",
                "metadata": {"character_name": "Alice"},
            }
            response = {
                "relations": [
                    {
                        "subject": "Alice",
                        "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                        "object": "Bob",
                        "confidence": 0.9,
                        "rationale": "Alice trusts Bob.",
                        "evidence_quote": "Alice trusts Bob completely.",
                        "evidence_document_ids": ["doc_evidence"],
                    }
                ]
            }
            environment = {
                "RELATION_CANDIDATE_PROVIDER": "openai-compatible",
                "OPENAI_COMPATIBLE_BASE_URL": "https://example.invalid/v1",
                "OPENAI_COMPATIBLE_API_KEY": "private-value",
                "OPENAI_COMPATIBLE_MODEL": "model",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(relation_extraction, "RUNTIME_ROOT", runtime_root),
                patch.object(relation_extraction, "load_runtime_jsonl", return_value=[document]),
                patch.object(
                    relation_extraction,
                    "_call_provider_with_retry",
                    side_effect=[
                        (response, {"attempts": 1, "retries": 0}),
                        ProviderCallFailure("network failed", attempts=3, retriable=True),
                    ],
                ),
            ):
                first_result = relation_extraction.extract(limit=2)

            self.assertEqual(first_result["processed"], 1)
            self.assertEqual(first_result["failed"], 1)
            saved_jobs = [json.loads(line) for line in (review_root / "narrative_relation_jobs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([job["status"] for job in saved_jobs], ["completed", "failed"])
            self.assertEqual(len((review_root / "narrative_relation_candidates.jsonl").read_text(encoding="utf-8").splitlines()), 1)

            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(relation_extraction, "RUNTIME_ROOT", runtime_root),
                patch.object(relation_extraction, "load_runtime_jsonl", return_value=[document]),
                patch.object(relation_extraction, "_call_provider_with_retry", return_value=(response, {"attempts": 2, "retries": 1})),
            ):
                second_result = relation_extraction.extract(limit=2)

            self.assertEqual(second_result["processed"], 1)
            self.assertEqual(second_result["failed"], 0)
            saved_jobs = [json.loads(line) for line in (review_root / "narrative_relation_jobs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([job["status"] for job in saved_jobs], ["completed", "completed"])
            self.assertEqual(len((review_root / "narrative_relation_candidates.jsonl").read_text(encoding="utf-8").splitlines()), 2)

    def test_relation_candidate_validation_requires_narrative_evidence(self) -> None:
        job = {
            "source_type": "event_lore",
            "allowed_relation_types": ["HAS_PREFERENCE", "PARTICIPATES_IN_EVENT", "HAS_RELATIONSHIP_CONTEXT"],
            "evidence_document_ids": ["doc_story"],
        }
        documents = {
            "doc_story": {
                "text": "分析员带队探索第五研究所。猫汐尔称赞分析员靠得住。武器获取概率提升。",
                "metadata": {"character_name": "猫汐尔"},
            }
        }
        mechanics_candidate, mechanics_reason = validate_relation_candidate(
            {
                "subject": "普赛克16",
                "relation_type": "PARTICIPATES_IN_EVENT",
                "object": "武器共鸣",
                "rationale": "获取概率提升",
                "evidence_quote": "武器获取概率提升",
                "evidence_document_ids": ["doc_story"],
            },
            job,
            documents,
            {"猫汐尔", "分析员"},
        )
        self.assertIsNone(mechanics_candidate)
        self.assertEqual(mechanics_reason, "mechanics_or_operations_content")

        preference_candidate, preference_reason = validate_relation_candidate(
            {
                "subject": "猫汐尔",
                "relation_type": "HAS_PREFERENCE",
                "object": "分析员",
                "rationale": "称赞分析员靠得住",
                "evidence_quote": "猫汐尔称赞分析员靠得住",
                "evidence_document_ids": ["doc_story"],
            },
            job,
            documents,
            {"猫汐尔", "分析员"},
        )
        self.assertIsNone(preference_candidate)
        self.assertEqual(preference_reason, "preference_target_is_character")

        valid_candidate, valid_reason = validate_relation_candidate(
            {
                "subject": "猫汐尔",
                "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                "object": "分析员",
                "rationale": "称赞分析员靠得住",
                "evidence_quote": "猫汐尔称赞分析员靠得住",
                "evidence_document_ids": ["doc_story"],
            },
            job,
            documents,
            {"猫汐尔", "分析员"},
        )
        self.assertIsNotNone(valid_candidate)
        self.assertIsNone(valid_reason)
        self.assertEqual(valid_candidate["evidence_quote"], "猫汐尔称赞分析员靠得住")

    def test_relation_candidate_validation_rejects_inference_and_low_value_edges(self) -> None:
        job = {
            "source_type": "character_story",
            "allowed_relation_types": [
                "HAS_PREFERENCE",
                "HAS_RELATIONSHIP_CONTEXT",
                "KNOWS",
                "OWNS_ITEM",
                "PARTICIPATES_IN_EVENT",
                "VISITS_LOCATION",
            ],
            "evidence_document_ids": ["doc_story"],
        }
        documents = {
            "doc_story": {
                "text": "猫汐尔正在撰写绘本原稿。辰星恬静地坐在轮椅上。芙提雅在自己的房间休息。猫汐尔称呼分析员一起行动。你的惊喜不会指的是夜袭吧？愿将与郎君的相守比作无限展开的卷轴古籍。",
                "metadata": {"character_name": "猫汐尔"},
            }
        }
        cases = [
            (
                {
                    "subject": "猫汐尔",
                    "relation_type": "HAS_PREFERENCE",
                    "object": "撰写绘本",
                    "rationale": "正在撰写绘本",
                    "evidence_quote": "猫汐尔正在撰写绘本原稿。",
                    "evidence_document_ids": ["doc_story"],
                },
                "preference_not_explicit",
            ),
            (
                {
                    "subject": "辰星",
                    "relation_type": "OWNS_ITEM",
                    "object": "轮椅",
                    "rationale": "坐在轮椅上",
                    "evidence_quote": "辰星恬静地坐在轮椅上。",
                    "evidence_document_ids": ["doc_story"],
                },
                "ownership_not_explicit",
            ),
            (
                {
                    "subject": "芙提雅",
                    "relation_type": "VISITS_LOCATION",
                    "object": "世界树公司员工宿舍",
                    "rationale": "在自己的房间休息",
                    "evidence_quote": "芙提雅在自己的房间休息。",
                    "evidence_document_ids": ["doc_story"],
                },
                "object_not_in_evidence_quote",
            ),
            (
                {
                    "subject": "猫汐尔",
                    "relation_type": "KNOWS",
                    "object": "分析员",
                    "rationale": "称呼分析员",
                    "evidence_quote": "猫汐尔称呼分析员一起行动。",
                    "evidence_document_ids": ["doc_story"],
                },
                "low_value_relation_type",
            ),
            (
                {
                    "subject": "米娅",
                    "relation_type": "PARTICIPATES_IN_EVENT",
                    "object": "夜袭",
                    "rationale": "猜测夜袭是惊喜",
                    "evidence_quote": "你的惊喜不会指的是夜袭吧？",
                    "evidence_document_ids": ["doc_story"],
                },
                "event_not_asserted_as_fact",
            ),
            (
                {
                    "subject": "辰星",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "郎君",
                    "rationale": "表达相守愿望",
                    "evidence_quote": "愿将与郎君的相守比作无限展开的卷轴古籍。",
                    "evidence_document_ids": ["doc_story"],
                },
                "generic_relationship_target",
            ),
        ]
        for relation, expected_reason in cases:
            with self.subTest(relation=relation["relation_type"]):
                candidate, reason = validate_relation_candidate(relation, job, documents, {"猫汐尔", "分析员"})
                self.assertIsNone(candidate)
                self.assertEqual(reason, expected_reason)

    @pytest.mark.runtime_data
    def test_runtime_artifacts_are_available(self) -> None:
        status = self.repository.status()
        self.assertTrue(status["lakehouse"])
        self.assertTrue(status["lexical_index"])
        self.assertTrue(status["personas"])
        self.assertTrue(status["graph"])

    def test_explicit_relationship_guard_does_not_accept_a_battle_only_answer(self) -> None:
        background = {"status": "explicit"}
        answer, repaired = MVPService._repair_explicit_relationship_answer(
            "里芙，我们是什么关系？",
            "我们是并肩作战、彼此信任的战友。",
            background,
        )
        self.assertTrue(repaired)
        self.assertIn("恒约", answer)
        self.assertIn("妻子", answer)

    def test_explicit_relationship_guard_leaves_natural_confirmations_alone(self) -> None:
        background = {"status": "explicit"}
        original = "是的，我是你的妻子，也是与你立下恒约的伴侣。"
        answer, repaired = MVPService._repair_explicit_relationship_answer(
            "你是我的妻子吗？",
            original,
            background,
        )
        self.assertFalse(repaired)
        self.assertEqual(answer, original)

    def test_generic_partner_word_does_not_upgrade_relationship_background(self) -> None:
        documents = [
            {
                "document_id": "doc_pet",
                "source_type": "special_mail",
                "title": "宠物伙伴",
                "text": "她是主人最亲密的伴侣。",
                "metadata": {"character_id": "cat"},
            }
        ]
        background = MVPService._relationship_background(documents, "cat")
        self.assertEqual(background["status"], "supported")

    def test_natural_question_contract_keeps_food_and_activity_focus(self) -> None:
        food_intents = MVPService._query_intents("吃了什么")
        self.assertIn("current_state", food_intents)
        self.assertEqual(MVPService._question_focus("吃了什么", food_intents), "food_or_drink")
        activity_intents = MVPService._query_intents("你现在在做什么")
        self.assertEqual(MVPService._question_focus("你现在在做什么", activity_intents), "current_activity")
        contract = MVPService._response_contract("吃了什么", food_intents)
        self.assertIn("食物或饮品", contract)
        self.assertIn("不能回答地点", contract)

    def test_latest_state_guard_corrects_superseded_pain_setting(self) -> None:
        context = {
            "question_focus": "current_condition",
            "hits": [
                {
                    "citation": {"title": "特殊邮件 / 角色 / 猫汐尔 / 2026-01-01-00:00 / 后续"},
                    "text": "痛觉复苏后，更能领悟疗愈的意义。",
                    "metadata": {},
                }
            ],
        }
        answer, repaired = MVPService._repair_latest_state_answer(
            "痛觉恢复了吗",
            "我以前无法感知疼痛。",
            context,
        )
        self.assertTrue(repaired)
        self.assertIn("痛觉已经恢复", answer)

    def test_latest_state_guard_leaves_consistent_answer_unchanged(self) -> None:
        context = {
            "question_focus": "current_condition",
            "hits": [
                {
                    "citation": {"title": "特殊邮件 / 角色 / 猫汐尔 / 2026-01-01-00:00 / 后续"},
                    "text": "痛觉复苏后，更能领悟疗愈的意义。",
                    "metadata": {},
                }
            ],
        }
        original = "已经恢复了，现在我会以如今的状态生活。"
        answer, repaired = MVPService._repair_latest_state_answer("痛觉恢复了吗", original, context)
        self.assertFalse(repaired)
        self.assertEqual(answer, original)

    @pytest.mark.runtime_data
    def test_persona_evidence_is_traceable(self) -> None:
        document_ids = set(self.repository.documents_by_id())
        authoritative_characters = dialogue_characters()
        profiles = self.repository.personas()
        self.assertGreater(len(profiles), 0)
        for profile in profiles:
            for evidence_ids in profile["evidence"].values():
                self.assertTrue(set(evidence_ids).issubset(document_ids))
            self.assertEqual(profile["relationship_invariant"]["user_role"], "分析员")
            self.assertEqual(profile["character_name"], authoritative_characters[profile["character_id"]])

    @pytest.mark.runtime_data
    def test_selectable_characters_are_canonical_dialogue_identities(self) -> None:
        selectable = self.repository.list_characters()
        names = {character["character_name"] for character in selectable}
        self.assertEqual(len(names), len(selectable))
        self.assertTrue({"芬妮", "苔丝", "茉莉安", "辰星", "晴"}.issubset(names))
        self.assertTrue(
            {"芬妮·戈尔登", "苔丝·科特金", "茉莉安·安德烈奥蒂", "姬辰星", "鸣濑晴"}.isdisjoint(names)
        )
        self.assertTrue(NON_DIALOGUE_CHARACTER_IDS.isdisjoint({character["character_id"] for character in selectable}))

    @pytest.mark.runtime_data
    def test_graph_edges_reference_known_nodes(self) -> None:
        nodes = self.repository.graph_nodes()
        edges = self.repository.graph_edges()
        self.assertGreater(len(edges), 0)
        for edge in edges:
            self.assertEqual(edge["review_status"], "verified")
            self.assertIn(edge["from_id"], nodes)
            self.assertIn(edge["to_id"], nodes)
            self.assertGreater(len(edge["evidence_page_ids"]), 0)

    def test_approved_entity_node_becomes_available_without_approving_its_relation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_tower",
                    "page_id": "page_tower",
                    "title": "Tower evidence",
                    "source_type": "main_story",
                    "text": "Alice arrives at Tower.",
                }
            ]
            relation_candidate = {
                "candidate_id": "candidate_visits_tower",
                "subject": "Alice",
                "relation_type": "VISITS_LOCATION",
                "object": "Tower",
                "evidence_quote": "Alice arrives at Tower.",
                "evidence_document_ids": ["doc_tower"],
                "source_type": "main_story",
                "review_status": "pending_review",
            }
            repository = self._review_repository(
                runtime_root,
                [relation_candidate],
                documents,
                [{"node_id": "character:alice", "node_type": "character", "name": "Alice"}],
            )
            proposed_node_id = "location:review_" + common.stable_id("location", "tower")
            entity_candidate = {
                "entity_candidate_id": "entity_candidate_tower",
                "entity_name": "Tower",
                "proposed_node_type": "location",
                "proposed_node_id": proposed_node_id,
                "relation_candidate_ids": ["candidate_visits_tower"],
                "source_types": ["main_story"],
                "evidence_document_ids": ["doc_tower"],
                "evidence_page_ids": ["page_tower"],
                "review_status": "pending_review",
            }
            (runtime_root / "review" / "entity_node_candidates.jsonl").write_text(
                json.dumps(entity_candidate) + "\n", encoding="utf-8"
            )

            node_result = repository.decide_entity_node_candidate(
                "entity_candidate_tower", "approved", "test-reviewer", "Literal location confirmed."
            )

            self.assertEqual(node_result["review_status"], "approved")
            self.assertEqual(node_result["approved_node"]["node_id"], proposed_node_id)
            self.assertIn(proposed_node_id, repository.graph_nodes())
            stored_relation = json.loads(
                (runtime_root / "review" / "narrative_relation_candidates.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(stored_relation["review_status"], "pending_review")

            relation_result = repository.decide_relation_candidate(
                "candidate_visits_tower",
                "approved",
                "test-reviewer",
                "Direct travel statement confirmed.",
                "character:alice",
                proposed_node_id,
            )
            self.assertEqual(relation_result["approved_edge"]["narrative_scope"], "situational")
            self.assertEqual(len(repository.graph_edges()), 1)

    def test_legacy_human_edge_scope_is_hydrated_without_changing_its_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            documents = [
                {
                    "document_id": "doc_scene",
                    "page_id": "page_scene",
                    "title": "Scene evidence",
                    "source_type": "random_event",
                    "text": "Alice and Bob talk during the random event.",
                }
            ]
            candidates = [
                {
                    "candidate_id": "candidate_legacy",
                    "subject": "Alice",
                    "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                    "object": "Bob",
                    "evidence_quote": "Alice and Bob talk during the random event.",
                    "evidence_document_ids": ["doc_scene"],
                    "source_type": "random_event",
                    "review_status": "approved",
                }
            ]
            repository = self._review_repository(
                runtime_root,
                candidates,
                documents,
                [
                    {"node_id": "character:alice", "node_type": "character", "name": "Alice"},
                    {"node_id": "character:bob", "node_type": "character", "name": "Bob"},
                ],
            )
            legacy_edge = {
                "edge_id": "edge_legacy",
                "from_id": "character:alice",
                "relation_type": "HAS_RELATIONSHIP_CONTEXT",
                "to_id": "character:bob",
                "evidence_page_ids": ["page_scene"],
                "source_manifests": ["human_relation_review"],
                "confidence": "human_approved",
                "review_status": "verified",
                "candidate_id": "candidate_legacy",
            }
            approved_path = runtime_root / "review" / "approved_narrative_edges.jsonl"
            approved_path.write_text(json.dumps(legacy_edge) + "\n", encoding="utf-8")

            hydrated = repository.graph_edges()[0]

            self.assertEqual(hydrated["source_types"], ["random_event"])
            self.assertEqual(hydrated["narrative_scope"], "situational")
            stored = json.loads(approved_path.read_text(encoding="utf-8"))
            self.assertNotIn("source_types", stored)
            self.assertNotIn("narrative_scope", stored)

    def test_approved_relation_candidate_becomes_a_verified_graph_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            (runtime_root / "lakehouse").mkdir()
            (runtime_root / "graph").mkdir()
            (runtime_root / "review").mkdir()
            (runtime_root / "lakehouse" / "documents.jsonl").write_text(
                json.dumps({"document_id": "doc_evidence", "page_id": "page_evidence", "title": "测试证据", "source_type": "main_story"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            nodes = [
                {"node_id": "character:source", "node_type": "character", "name": "主体"},
                {"node_id": "character:target", "node_type": "character", "name": "客体"},
            ]
            (runtime_root / "graph" / "nodes.jsonl").write_text(
                "".join(json.dumps(node, ensure_ascii=False) + "\n" for node in nodes), encoding="utf-8"
            )
            (runtime_root / "graph" / "edges.jsonl").write_text("", encoding="utf-8")
            candidate = {
                "candidate_id": "candidate_test",
                "relation_type": "KNOWS",
                "evidence_document_ids": ["doc_evidence"],
                "review_status": "pending_review",
            }
            (runtime_root / "review" / "narrative_relation_candidates.jsonl").write_text(
                json.dumps(candidate, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            settings = Settings(
                data_root=self.settings.data_root,
                runtime_root=runtime_root,
                chat_enabled=False,
                embedding_model=self.settings.embedding_model,
                allowed_origins=self.settings.allowed_origins,
            )
            repository = RuntimeRepository(settings)
            result = repository.decide_relation_candidate(
                "candidate_test", "approved", "test-reviewer", "原文已核对", "character:source", "character:target"
            )
            self.assertEqual(result["review_status"], "approved")
            self.assertEqual(result["approved_edge"]["review_status"], "verified")
            self.assertEqual(result["approved_edge"]["evidence_page_ids"], ["page_evidence"])
            self.assertEqual(len(repository.graph_edges()), 1)

    def test_approved_relation_candidate_rejects_source_page_node_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            (runtime_root / "lakehouse").mkdir()
            (runtime_root / "graph").mkdir()
            (runtime_root / "review").mkdir()
            (runtime_root / "lakehouse" / "documents.jsonl").write_text(
                json.dumps({"document_id": "doc_evidence", "page_id": "page_evidence", "title": "Evidence", "source_type": "main_story"}) + "\n",
                encoding="utf-8",
            )
            nodes = [
                {"node_id": "page:source", "node_type": "page", "name": "Source page"},
                {"node_id": "character:target", "node_type": "character", "name": "Target"},
            ]
            (runtime_root / "graph" / "nodes.jsonl").write_text(
                "".join(json.dumps(node) + "\n" for node in nodes), encoding="utf-8"
            )
            (runtime_root / "graph" / "edges.jsonl").write_text("", encoding="utf-8")
            candidate = {
                "candidate_id": "candidate_page_mapping",
                "relation_type": "ALLY_OF",
                "evidence_document_ids": ["doc_evidence"],
                "review_status": "pending_review",
            }
            candidate_path = runtime_root / "review" / "narrative_relation_candidates.jsonl"
            candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
            settings = Settings(
                data_root=self.settings.data_root,
                runtime_root=runtime_root,
                chat_enabled=False,
                embedding_model=self.settings.embedding_model,
                allowed_origins=self.settings.allowed_origins,
            )
            repository = RuntimeRepository(settings)

            with self.assertRaisesRegex(ValueError, "actor-type source"):
                repository.decide_relation_candidate(
                    "candidate_page_mapping",
                    "approved",
                    "test-reviewer",
                    "page nodes cannot be endpoints",
                    "page:source",
                    "character:target",
                )
            stored = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["review_status"], "pending_review")

    @pytest.mark.runtime_data
    def test_costume_and_armor_material_participate_without_manual_filters(self) -> None:
        costume_documents = [
            document for document in self.repository.documents() if document["metadata"].get("requires_costume_context")
        ]
        self.assertGreater(len(costume_documents), 0)
        document = costume_documents[0]
        self.assertTrue(self.repository._is_allowed_context(document, None))

    @pytest.mark.runtime_data
    def test_mvp_style_resolver_links_costume_to_armor_without_selecting_a_new_character(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "ca0144ccd81b"  # 里芙
        rows = service._style_index()[character_id]
        costume = next(row for row in rows if row["kind"] == "costume")
        query = str(costume["costume_name"])
        context = service._resolve_style_context(character_id, query)

        self.assertEqual(context["status"], "active")
        self.assertEqual(context["kind"], "costume")
        self.assertEqual(context["costume_id"], costume["costume_id"])
        self.assertEqual(context["armor_id"], costume["armor_id"])
        self.assertEqual(context["costume_activation"], "explicit")
        self.assertEqual(context["activation_source"], "message")

        view = service._views()[character_id]
        allowed_costumes = [
            document
            for document in self.repository.documents()
            if document["source_type"] == "character_costume"
            and service._allowed_document(document, view, context)
        ]
        self.assertTrue(allowed_costumes)
        self.assertTrue(
            all(document["metadata"].get("costume_id") == costume["costume_id"] for document in allowed_costumes)
        )

    @pytest.mark.runtime_data
    def test_mvp_exact_costume_retrieval_promotes_matching_armor(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "25b23cb64398"  # 凯西娅
        costume = next(
            row for row in service._style_index()[character_id] if row["kind"] == "costume"
        )
        context = service.retrieve(
            character_id,
            f"和我说说{costume['costume_name']}这套时装的细节。",
            limit=8,
        )
        documents = self.repository.documents_by_id()
        selected = [
            documents[hit["citation"]["document_id"]]
            for hit in context["hits"]
        ]
        self.assertEqual(context["question_focus"], "costume_detail")
        self.assertTrue(
            any(
                document["source_type"] == "character_costume"
                and document["metadata"].get("costume_id") == costume["costume_id"]
                for document in selected
            )
        )
        self.assertTrue(
            any(
                document["source_type"] == "character_armor"
                and document["metadata"].get("armor_id") == costume["armor_id"]
                for document in selected
            )
        )

    @pytest.mark.runtime_data
    def test_mvp_current_condition_promotes_latest_pain_evidence(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = service.retrieve(
            "6862c43d2ac9",
            "猫汐尔现在的痛觉恢复了吗？",
            limit=8,
        )
        selected_ids = [hit["citation"]["document_id"] for hit in context["hits"]]
        self.assertEqual(context["question_focus"], "current_condition")
        self.assertIn("doc_1eec692886123f76", selected_ids)

    @pytest.mark.runtime_data
    def test_mvp_logistics_retrieval_is_scoped_to_character_and_armor(self) -> None:
        """A logistics question must not leak another character's squad."""

        service = MVPService(self.settings, self.repository)
        cases = [
            ("6862c43d2ac9", "\u732b\u6c50\u5c14", "3034f12b4d4d", "\u83b2\u9a71"),
            ("ca0144ccd81b", "\u91cc\u8299", "30bda92f7939", "\u65e0\u9650\u4e4b\u89c6"),
            ("25b23cb64398", "\u51ef\u897f\u5a05", "a33608bcd5ce", "\u671d\u7ffc"),
            ("1b0a6b35719a", "\u82ac\u59ae", "a32352af01cc", "\u8f89\u8000"),
            ("a2ffc5b44d7f", "\u8299\u63d0\u96c5", "fd196ebec705", "\u70ac\u82af"),
        ]
        documents = self.repository.documents_by_id()

        for character_id, character_name, armor_id, armor_name in cases:
            context = service.retrieve(
                character_id,
                f"{armor_name}\u7684\u540e\u52e4\u5c0f\u961f\u6709\u54ea\u4e9b\u6210\u5458\uff1f",
                limit=8,
            )
            self.assertEqual(context["question_focus"], "logistics_detail", character_name)
            logistics_documents = [
                documents[hit["citation"]["document_id"]]
                for hit in context["hits"]
                if hit["citation"].get("source_type") == "logistics_lore"
            ]
            self.assertTrue(logistics_documents, character_name)
            for document in logistics_documents:
                metadata = document.get("metadata") or {}
                relationships = metadata.get("logistics_relationships") or []
                self.assertTrue(
                    any(
                        str(item.get("character_id") or "") == character_id
                        and str(item.get("armor_id") or "") == armor_id
                        for item in relationships
                        if isinstance(item, dict)
                    ),
                    f"{character_name} received unrelated logistics document {document.get('title')}",
                )

    def test_mvp_scene_state_hides_precise_location_from_prompt_unless_asked(self) -> None:
        raw_scene = {
            "analyst_location": "基地大厅",
            "character_location": "医疗室",
            "character_activity": "整理装备",
            "co_located": False,
            "state_scope": "session_simulation",
        }
        hidden = MVPService._scene_state_for_prompt(raw_scene, "早安，凯西娅。", "general")
        visible = MVPService._scene_state_for_prompt(raw_scene, "凯西娅，你现在在哪里？", "location")

        self.assertEqual(hidden["location_visibility"], "hidden_unless_asked")
        self.assertNotIn("analyst_location", hidden)
        self.assertNotIn("character_location", hidden)
        self.assertNotIn("character_activity", hidden)
        self.assertEqual(visible["character_location"], "医疗室")
        self.assertEqual(visible["analyst_location"], "基地大厅")

    def test_mvp_guard_rejects_unprompted_live_scene_disclosure(self) -> None:
        context = {
            "raw_scene_state": {
                "analyst_location": "基地大厅",
                "character_location": "医疗室",
                "character_activity": "整理装备",
            },
            "question_focus": "general",
            "dialogue_boundary": {"kind": "standard"},
        }
        violations = MVPService._answer_guardrail_violations(
            "早安。",
            "早安，我正在医疗室整理装备。",
            context,
            "immersive",
        )

        self.assertTrue(any(item.startswith("unprompted_scene_disclosure:") for item in violations))

    @pytest.mark.runtime_data
    def test_mvp_cross_character_main_story_promotes_futiya_armor_context(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = service.retrieve(
            "a2ffc5b44d7f",
            "芙提雅，安卡希雅的新战术套装是你研发的吗？",
            limit=8,
        )
        documents = self.repository.documents_by_id()
        selected = [documents[hit["citation"]["document_id"]] for hit in context["hits"]]

        self.assertTrue(context["cross_character_story_context"]["active"])
        self.assertTrue(
            any(
                document["source_type"] == "main_story"
                and "芙提雅" in document["text"]
                and "安卡希雅" in document["text"]
                for document in selected
            )
        )

    @pytest.mark.runtime_data
    def test_mvp_futiya_body_teasing_hint_is_source_bound(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = service.retrieve(
            "a2ffc5b44d7f",
            "芙提雅，平板身材只是玩笑，别生气。",
            limit=8,
        )

        hint = context["interaction_hint"]
        self.assertIsNotNone(hint)
        self.assertEqual(hint["required_opening"], "干什么！")
        violations = service._answer_guardrail_violations(
            "芙提雅，平板身材只是玩笑，别生气。",
            "我没有生气，只是你别这样说。",
            {**context, "communication_context": {"channel": "in_person"}},
            "immersive",
            [{"type": "speech", "text": "我没有生气，只是你别这样说。"}],
        )
        self.assertIn("interaction_opening_required:干什么！", violations)

    def test_mvp_continuity_guard_rejects_event_replay_after_medium_switch(self) -> None:
        continuity = MVPService._continuity_card(
            {
                "turns": [
                    {
                        "communication_channel": "text",
                        "user": "辛苦了。",
                        "assistant": "训练已经结束了，我现在能和你聊天。",
                    }
                ]
            },
            "in_person",
        )
        context = {
            "continuity_card": continuity,
            "question_focus": "general",
            "dialogue_boundary": {"kind": "standard"},
        }
        violations = MVPService._answer_guardrail_violations(
            "那就陪我坐一会儿吧。",
            "训练刚结束，我也正想休息。",
            context,
            "immersive",
        )

        self.assertIn("repeated_recent_event_after_channel_switch", violations)

    def test_mvp_continuity_card_keeps_recent_turns_for_natural_progression(self) -> None:
        continuity = MVPService._continuity_card(
            {
                "turns": [
                    {"communication_channel": "in_person", "user": "早安。", "assistant": "早安。"},
                    {"communication_channel": "text", "user": "刚才忙什么？", "assistant": "训练刚结束。"},
                    {"communication_channel": "text", "user": "那现在呢？", "assistant": "现在可以陪你聊天。"},
                    {"communication_channel": "in_person", "user": "坐一会儿吧。", "assistant": "好。"},
                ]
            },
            "in_person",
        )

        self.assertEqual(len(continuity["recent_turns"]), 3)
        self.assertIn("不要为了填充篇幅再次复述同一场景", continuity["rule"])

    def test_mvp_mechanical_retrieval_opening_is_rejected_only_for_natural_chat(self) -> None:
        service = MVPService(self.settings, self.repository)
        natural = service._answer_guardrail_violations(
            "你喜欢什么？",
            "根据目前提供的资料，我无法确定。",
            {"question_focus": "preference_or_value", "dialogue_boundary": {"kind": "standard"}},
            "immersive",
        )
        factual = service._answer_guardrail_violations(
            "请根据资料说明这段关系。",
            "根据目前提供的资料，我无法确定。",
            {"question_focus": "relationship_label", "dialogue_boundary": {"kind": "standard"}},
            "assistant",
        )

        self.assertTrue(any(item.startswith("mechanical_dialogue:") for item in natural))
        self.assertFalse(any(item.startswith("mechanical_dialogue:") for item in factual))

    def test_mvp_system_prompt_includes_normalized_companion_address_rule(self) -> None:
        service = MVPService(self.settings, self.repository)
        prompt = service._system_prompt(
            service.character("a2ffc5b44d7f"),
            None,
            {},
            (),
            "immersive",
            {},
            mentioned_characters=[
                {
                    "character_id": "6862c43d2ac9",
                    "canonical_name": "猫汐尔",
                    "matched_alias": "猫猫",
                    "surface_policy": "canonical_response",
                }
            ],
        )

        self.assertIn("同伴称呼解析", prompt)
        self.assertIn("猫猫", prompt)
        self.assertIn("猫汐尔", prompt)

    def test_mvp_guard_rejects_unrequested_meetup_plan(self) -> None:
        violations = MVPService._answer_guardrail_violations(
            "我想给芬妮准备一份惊喜。",
            "我们在大厅碰面，再一起想想怎么给芬妮惊喜。",
            {"question_focus": "general", "dialogue_boundary": {"kind": "standard"}},
            "immersive",
        )

        self.assertIn("unprompted_logistics_plan", violations)

    @pytest.mark.runtime_data
    def test_mvp_armor_context_does_not_leak_all_costumes(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "ca0144ccd81b"
        armor = next(row for row in service._style_index()[character_id] if row["kind"] == "armor")
        context = service._resolve_style_context(character_id, str(armor["armor_name"]))
        self.assertEqual(context["kind"], "armor")
        self.assertEqual(context["armor_id"], armor["armor_id"])
        view = service._views()[character_id]
        self.assertFalse(
            any(
                document["source_type"] == "character_costume"
                and service._allowed_document(document, view, context)
                for document in self.repository.documents()
            )
        )
        self.assertTrue(
            any(
                document["source_type"] == "character_armor"
                and service._allowed_document(document, view, context)
                for document in self.repository.documents()
            )
        )

    @pytest.mark.runtime_data
    def test_mvp_armor_costume_question_promotes_only_matching_costumes(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "25b23cb64398"
        armor = next(row for row in service._style_index()[character_id] if row["kind"] == "armor")
        context = service._resolve_style_context(
            character_id,
            f"{armor['armor_name']}有哪些皮肤？",
        )
        self.assertEqual(context["kind"], "armor")
        self.assertTrue(context["include_related_costumes"])
        self.assertEqual(context["resolution"], "armor_with_costume_candidates")
        self.assertTrue(context["related_costume_document_ids"])
        view = service._views()[character_id]
        allowed = [
            document
            for document in self.repository.documents()
            if document["source_type"] == "character_costume"
            and service._allowed_document(document, view, context)
        ]
        self.assertTrue(allowed)
        self.assertTrue(
            all(
                document["metadata"].get("armor_id") == armor["armor_id"]
                for document in allowed
            )
        )
        self.assertLessEqual(
            len(allowed), len(context["related_costume_document_ids"])
        )

    def test_mvp_meeting_reference_without_user_premise_is_rejected(self) -> None:
        violations = MVPService._answer_guardrail_violations(
            "你今天过得怎么样？",
            "按照我们约好的见面安排，晚点再一起出发吧。",
            {"question_focus": "casual_check_in", "dialogue_boundary": {"kind": "standard"}},
            "immersive",
        )
        self.assertTrue(any(item.startswith("unsupported_session_premise:") for item in violations))

    def test_mvp_fenny_daily_harshness_is_rejected_outside_high_stakes(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "character": service.character("1b0a6b35719a"),
            "question_focus": "casual_check_in",
            "dialogue_boundary": {"kind": "standard"},
        }
        violations = service._answer_guardrail_violations(
            "早安，芬妮。",
            "给我闭嘴，分析员。",
            context,
            "immersive",
        )
        self.assertTrue(any(item.startswith("fenny_daily_harshness:") for item in violations))

    def test_mvp_explicit_relationship_uses_pet_name_for_direct_address(self) -> None:
        answer, changed = MVPService._normalize_explicit_relationship_address(
            "分析员，早安。我们一起吃点东西吧。",
            {
                "character": type("Character", (), {"character_id": "ca0144ccd81b"})(),
                "relationship_background": {"status": "explicit"},
            },
        )
        self.assertTrue(changed)
        self.assertTrue(answer.startswith("亲爱的"))

    def test_mvp_unresolved_generic_costume_input_does_not_unlock_costumes(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "ca0144ccd81b"
        context = service._resolve_style_context(character_id, "请切换", "皮肤")
        self.assertEqual(context["status"], "unresolved")
        view = service._views()[character_id]
        self.assertFalse(
            any(
                document["source_type"] == "character_costume"
                and service._allowed_document(document, view, context)
                for document in self.repository.documents()
            )
        )

    @pytest.mark.runtime_data
    def test_mvp_two_costumes_in_one_message_are_not_guessed(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "ca0144ccd81b"
        costumes = [row for row in service._style_index()[character_id] if row["kind"] == "costume"][:2]
        context = service._resolve_style_context(
            character_id,
            f"{costumes[0]['costume_name']} 和 {costumes[1]['costume_name']} 哪套更好？",
        )
        self.assertEqual(context["status"], "ambiguous")
        self.assertEqual(context["resolution"], "ambiguous")

    @pytest.mark.runtime_data
    def test_mvp_style_context_and_core_memory_survive_mode_switch(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "ca0144ccd81b"
        costume = next(row for row in service._style_index()[character_id] if row["kind"] == "costume")
        context = service._resolve_style_context(character_id, str(costume["costume_name"]))
        service._remember_session("style-test", character_id, "activate", "answer", "immersive", context)
        same_mode = service._session_snapshot("style-test", character_id, "immersive")
        switched_mode = service._session_snapshot("style-test", character_id, "assistant")
        self.assertEqual(same_mode["style_context"]["costume_id"], costume["costume_id"])
        self.assertEqual(switched_mode["style_context"]["costume_id"], costume["costume_id"])
        self.assertEqual(len(same_mode["turns"]), 1)
        self.assertEqual(switched_mode["turns"], [])
        self.assertEqual(switched_mode["mode"], "assistant")

    def test_mvp_mode_switch_shares_relationship_premise_but_not_turn_history(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = "25b23cb64398"
        service._remember_session(
            "shared-memory-test",
            character_id,
            "你已经是我的妻子了",
            "我记住了。",
            "immersive",
        )
        service._remember_session(
            "shared-memory-test",
            character_id,
            "请把这段资料整理成三点",
            "第一点。第二点。第三点。",
            "assistant",
        )
        immersive = service._session_snapshot("shared-memory-test", character_id, "immersive")
        assistant = service._session_snapshot("shared-memory-test", character_id, "assistant")
        self.assertEqual(immersive["premises"], ["你已经是我的妻子了"])
        self.assertEqual(assistant["premises"], ["你已经是我的妻子了"])
        self.assertEqual([item["user"] for item in immersive["turns"]], ["你已经是我的妻子了"])
        self.assertEqual([item["user"] for item in assistant["turns"]], ["请把这段资料整理成三点"])
        self.assertEqual(set(immersive["mode_turns"]), {"immersive"})
        self.assertEqual(set(assistant["mode_turns"]), {"assistant"})

    def test_mvp_assistant_time_tool_is_read_only_and_immersive_cannot_call_it(self) -> None:
        service = MVPService(self.settings, self.repository)
        with patch.dict(os.environ, {"MVP_CHAT_TIMEZONE": "Asia/Shanghai"}, clear=False):
            assistant = service._assistant_tool_context("现在几点？", "assistant")
            immersive = service._assistant_tool_context("现在几点？", "immersive")
        self.assertEqual([item["name"] for item in assistant["tool_calls"]], ["get_current_time"])
        self.assertEqual(assistant["tool_calls"][0]["status"], "completed")
        self.assertEqual(assistant["tool_results"][0]["name"], "get_current_time")
        self.assertEqual(immersive["tool_calls"], [])
        self.assertEqual(immersive["available_tools"], [])

    def test_mvp_assistant_mode_allows_evidence_style_opening(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "mode": "assistant",
            "question_focus": "general",
            "hits": [],
            "session_context": {"turns": []},
            "communication_context": {"channel": "text"},
            "dialogue_boundary": {"kind": "standard"},
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
        }
        answer = "根据目前提供的资料，这个角色的性格可以归纳为谨慎、可靠。"
        violations = service._answer_guardrail_violations(
            "请总结资料。", answer, context, "assistant", [{"type": "message", "text": answer}]
        )
        self.assertFalse(any(item.startswith("mechanical_dialogue:") for item in violations))

    def test_mvp_kaysia_explicit_relationship_address_is_natural(self) -> None:
        service = MVPService(self.settings, self.repository)
        answer, changed = service._normalize_explicit_relationship_address(
            "早安，分析员。",
            {
                "character": service.character("25b23cb64398"),
                "relationship_background": {"status": "explicit"},
            },
        )
        self.assertTrue(changed)
        self.assertEqual(answer, "早安，亲爱的。")

    def test_mvp_relationship_address_normalizer_handles_warm_tilde_vocative(self) -> None:
        service = MVPService(self.settings, self.repository)
        character = service.character("673ba6851b05")
        answer, changed = service._normalize_explicit_relationship_address(
            "下午好呀，分析员~今天过得怎么样？",
            {
                "character": character,
                "relationship_background": {"status": "explicit"},
            },
        )
        self.assertTrue(changed)
        self.assertEqual(answer, "下午好呀，亲爱的~今天过得怎么样？")

    def test_mvp_system_prompt_distinguishes_two_conversation_modes(self) -> None:
        service = MVPService(self.settings, self.repository)
        expected = {
            "673ba6851b05": "亲爱的",
            "cf0569ac6de9": "郎君",
            "daab0f4cceb4": "亲爱的",
            "25b23cb64398": "亲爱的",
        }
        for character_id, preferred in expected.items():
            character = service.character(character_id)
            for mode in ("immersive", "assistant"):
                prompt = service._system_prompt(
                    character,
                    None,
                    {},
                    (),
                    mode,
                    {},
                    {},
                    "in_person",
                    {},
                    {},
                    {},
                    {},
                    {},
                    {},
                    {},
                    {"status": "explicit", "preferred_address": preferred},
                )
                self.assertIn(preferred, prompt)
        character = service.character("ca0144ccd81b")
        immersive = service._system_prompt(character, None, {}, (), "immersive", {})
        assistant = service._system_prompt(character, None, {}, (), "assistant", {})
        self.assertIn("沉浸式陪伴模式", immersive)
        self.assertIn("禁止工具调用", immersive)
        self.assertIn("无论用户是否直接询问", immersive)
        self.assertIn("角色助手模式", assistant)
        self.assertIn("受控的检索证据", assistant)

    def test_mvp_feedback_options_do_not_depend_on_stale_question_bank(self) -> None:
        service = MVPService(self.settings, self.repository)
        character_id = '25b23cb64398'
        session_id = 'mode-address-memory-regression'
        service._remember_session(session_id, character_id, '你是我的老婆。', '记住了。', 'immersive')
        snapshot = service._session_snapshot(session_id, character_id, 'assistant')
        context = {
            'character': service.character(character_id),
            'relationship_background': {'status': 'unknown'},
            'session_context': snapshot,
        }
        memory = service._relationship_address_memory(context)
        self.assertEqual(memory['preferred_address'], '亲爱的')
        normalized_context = dict(context)
        normalized_context['relationship_address_memory'] = memory
        answer, changed = service._normalize_explicit_relationship_address('好的，分析员。', normalized_context)
        self.assertTrue(changed)
        self.assertEqual(answer, '好的，亲爱的。')
        service = MVPService(self.settings, self.repository)
        status_ids = {item["id"] for item in service.status()["feedback_options"]}
        question_ids = {
            item["id"]
            for item in service.questions("ca0144ccd81b")["feedback_options"]
        }
        self.assertIn("communication_mismatch", status_ids)
        self.assertIn("communication_mismatch", question_ids)

    def test_mvp_dialogue_aliases_resolve_input_without_becoming_default_address(self) -> None:
        service = MVPService(self.settings, self.repository)
        cat = service._resolve_character_mentions("猫猫现在有空吗？")
        teacher = service._resolve_character_mentions("小老师在做什么？")

        self.assertEqual(cat[0]["canonical_name"], "猫汐尔")
        self.assertEqual(cat[0]["surface_policy"], "canonical_response")
        self.assertEqual(teacher[0]["canonical_name"], "芙提雅")
        self.assertEqual(teacher[0]["surface_policy"], "canonical_response")
        normalized, changed = service._normalize_response_aliases(
            "猫猫刚才还在休息。",
            {"character": service.character("a2ffc5b44d7f"), "mentioned_characters": cat},
        )
        self.assertTrue(changed)
        self.assertEqual(normalized, "猫汐尔刚才还在休息。")

    def test_mvp_response_aliases_normalize_model_invented_nicknames(self) -> None:
        service = MVPService(self.settings, self.repository)
        mentioned = service._resolve_character_mentions("猫猫和小老师在休息。")
        normalized, changed = service._normalize_response_aliases(
            "猫猫在休息，小老师在整理资料。",
            {
                "character": service.character("ca0144ccd81b"),
                "mentioned_characters": mentioned,
            },
        )
        self.assertTrue(changed)
        self.assertEqual(normalized, "猫汐尔在休息，芙提雅在整理资料。")
        self_name, self_changed = service._normalize_response_aliases(
            "小老师今天会认真回答。",
            {
                "character": service.character("a2ffc5b44d7f"),
                "mentioned_characters": service._resolve_character_mentions("小老师今天有空吗？"),
            },
        )
        self.assertFalse(self_changed)
        self.assertEqual(self_name, "小老师今天会认真回答。")

    def test_mvp_casual_check_in_does_not_promote_unrelated_current_state_lore(self) -> None:
        service = MVPService(self.settings, self.repository)
        message = "早安，凯西娅。今天过得怎么样？"
        intents = service._query_intents(message)
        self.assertEqual(service._question_focus(message, intents), "casual_check_in")
        self.assertIn("自然问候", service._response_contract(message, intents))

        context = {
            "question_focus": "casual_check_in",
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
            "communication_context": {"channel": "text"},
            "dialogue_boundary": {"kind": "standard"},
        }
        answer = "早安，亲爱的。自从恒约之后，我就变得特别嗜睡，今天也是睡到自然醒。"
        violations = service._answer_guardrail_violations(
            message,
            answer,
            context,
            "immersive",
            [{"type": "message", "text": answer}],
        )

        self.assertTrue(
            any(item.startswith("unsupported_casual_state_claim:") for item in violations)
        )
        relationship = {"status": "explicit", "relationship_label": "恒约伴侣"}
        self.assertEqual(
            service._relationship_background_for_prompt(
                {**context, "query_intents": ("relationship",), "relationship_background": relationship}
            ),
            {},
        )
        self.assertEqual(
            service._relationship_background_for_prompt(
                {
                    **context,
                    "question_focus": "relationship_label",
                    "query_intents": ("relationship",),
                    "relationship_background": relationship,
                }
            ),
            relationship,
        )

    @pytest.mark.runtime_data
    def test_mvp_chat_falls_back_when_casual_greeting_invents_current_state(self) -> None:
        service = MVPService(self.settings, self.repository)
        answer = "早安，亲爱的。自从恒约之后，我就变得特别嗜睡，今天也是睡到自然醒。"
        payload = self._communication_model_payload(answer, block_type="message")
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "25b23cb64398",
                "早安，凯西娅。今天过得怎么样？",
                session_id="casual-state-guard-test",
                communication_channel="text",
            )

        self.assertIn("casual_state_guard", result["response_adjustments"])
        self.assertEqual(result["answer"], "早安，亲爱的。看到你的消息了，今天想和我聊些什么？")
        self.assertEqual(result["content_blocks"], [{"type": "message", "text": result["answer"]}])
        self.assertEqual(result["citations"], [])

    def test_mvp_guard_rejects_unsupported_analyst_habit_claim(self) -> None:
        answer = "倒是你，今天可起得真早，不像平时那个爱睡懒觉的分析员哦。"
        context = {
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
            "communication_context": {"channel": "in_person"},
        }
        violations = MVPService._answer_guardrail_violations(
            "早安。",
            answer,
            context,
            "immersive",
            [{"type": "speech", "text": answer}],
        )
        self.assertTrue(
            any(item.startswith("unsupported_analyst_premise:") for item in violations)
        )
        cleaned = MVPService._strip_unsupported_analyst_premises(
            answer,
            MVPService._unsupported_analyst_premises(answer, context),
        )
        self.assertNotIn("爱睡懒觉", cleaned)
        self.assertNotIn("不像平时", cleaned)
        self.assertNotIn("今天可起得真早", cleaned)

    def test_mvp_guard_removes_unsupported_shared_knowledge_prefix_only(self) -> None:
        context = {
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
            "communication_context": {"channel": "in_person"},
        }
        answer = "你知道的，我不喜欢房间太乱。"
        premises = MVPService._unsupported_analyst_premises(answer, context)
        self.assertTrue(any("知道" in item for item in premises))
        cleaned = MVPService._strip_unsupported_analyst_premises(answer, premises)
        self.assertEqual(cleaned, "我不喜欢房间太乱。")

    def test_mvp_food_guard_rejects_historical_meal_as_present_answer(self) -> None:
        context = {
            "question_focus": "food_or_drink",
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
            "communication_context": {"channel": "text"},
        }
        violations = MVPService._answer_guardrail_violations(
            "你今天吃了什么？",
            "上次我在餐厅吃过面，还和大家聊了很久。",
            context,
            "immersive",
            [{"type": "message", "text": "上次我在餐厅吃过面，还和大家聊了很久。"}],
        )
        self.assertIn("direct_answer_focus:food_or_drink", violations)

    def test_mvp_cross_story_fact_guard_rejects_futiya_uncertainty(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "character": service.character("a2ffc5b44d7f"),
            "cross_character_story_context": {
                "active": True,
                "topic_terms": ["研发", "战术套装"],
                "mentioned_characters": [{"canonical_name": "安卡希雅"}],
            },
        }
        violations = service._answer_guardrail_violations(
            "安卡希雅的新装甲是你研发的吗？",
            "我不清楚，这是第一次听说。",
            context,
            "immersive",
            [{"type": "speech", "text": "我不清楚，这是第一次听说。"}],
        )
        self.assertIn("cross_character_fact_mismatch", violations)
        self.assertIn("研发", service._cross_character_fact_fallback(context))

    def test_mvp_chat_strips_unsupported_analyst_habit_after_failed_rewrite(self) -> None:
        service = MVPService(self.settings, self.repository)
        answer = "倒是你，今天可起得真早，不像平时那个爱睡懒觉的分析员哦。"
        payload = self._communication_model_payload(answer)
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "1b0a6b35719a",
                "早安。",
                session_id="analyst-premise-guard-test",
            )
        self.assertIn("analyst_premise_guard", result["response_adjustments"])
        self.assertNotIn("爱睡懒觉", result["answer"])
        self.assertNotIn("不像平时", result["answer"])
        self.assertTrue(
            any(
                block["type"] == "speech" and block["text"] in result["answer"]
                for block in result["content_blocks"]
            )
        )
        self.assertEqual(result["content_blocks"][0]["type"], "speech")
        self.assertNotIn("in_person_presence_enriched", result["response_adjustments"])

    def test_mvp_explicit_relationship_address_changes_only_direct_vocatives(self) -> None:
        service = MVPService(self.settings, self.repository)
        character = service.character("1b0a6b35719a")
        answer, changed = service._normalize_explicit_relationship_address(
            "早安，分析员。分析员是基地里的职务称呼。",
            {"character": character, "relationship_background": {"status": "explicit"}},
        )
        self.assertTrue(changed)
        self.assertIn("早安，达令", answer)
        self.assertIn("分析员是基地里的职务称呼", answer)

        unchanged, was_changed = service._normalize_explicit_relationship_address(
            "早安，分析员。",
            {"character": character, "relationship_background": {"status": "supported"}},
        )
        self.assertFalse(was_changed)
        self.assertEqual(unchanged, "早安，分析员。")

    def test_mvp_fenny_prompt_prioritizes_warm_daily_voice(self) -> None:
        service = MVPService(self.settings, self.repository)
        prompt = service._system_prompt(
            service.character("1b0a6b35719a"),
            None,
            {"status": "explicit", "evidence": []},
            ("relationship",),
        )
        self.assertIn("自信、明快、骄傲中带亲昵和调侃", prompt)
        self.assertIn("不要持续命令、训斥、敌意或急躁", prompt)
        self.assertIn("达令", prompt)

    def test_mvp_guard_rejects_unestablished_meeting_premise(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "session_context": {"turns": []},
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "mentioned_characters": [],
            "companion_social_context": {},
            "live_scene": None,
            "communication_context": {"channel": "in_person"},
        }
        violations = service._answer_guardrail_violations(
            "早安。",
            "我们约好见面后再聊。",
            context,
            "immersive",
            [{"type": "speech", "text": "我们约好见面后再聊。"}],
        )
        self.assertTrue(any(item.startswith("unsupported_session_premise:") for item in violations))
        self.assertEqual(
            service._unsupported_session_meeting_premises(
                "我们明天见面吧。", "那我们就约好见面。", context
            ),
            [],
        )

    def test_mvp_chat_strips_unestablished_meeting_premise_after_failed_rewrite(self) -> None:
        service = MVPService(self.settings, self.repository)
        answer = "我们约好见面后再一起商量。"
        payload = self._communication_model_payload(answer)
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "ca0144ccd81b",
                "早安。",
                session_id="session-premise-guard-test",
            )
        self.assertIn("session_premise_guard", result["response_adjustments"])
        self.assertNotIn("约好见面", result["answer"])

    def test_mvp_chat_falls_back_when_food_question_is_answered_only_with_a_location(self) -> None:
        service = MVPService(self.settings, self.repository)
        evasive_answer = json.dumps(
            {
                "answer": "我在餐厅那边，刚好和大家说了几句话。",
                "content_blocks": [
                    {"type": "message", "text": "我在餐厅那边，刚好和大家说了几句话。"}
                ],
                "confidence": "low",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(evasive_answer, {}), (evasive_answer, {})]
        ):
            result = service.chat(
                "6862c43d2ac9",
                "早安，猫汐尔，今天吃了什么？",
                session_id="direct-food-answer-test",
                communication_channel="text",
            )

        self.assertIn("direct_answer_guard", result["response_adjustments"])
        self.assertIn("还没决定吃什么", result["answer"])
        self.assertEqual(result["content_blocks"], [{"type": "message", "text": result["answer"]}])
        self.assertEqual(result["citations"], [])

    def test_mvp_chat_rejects_a_historical_meal_as_a_fabricated_current_fact(self) -> None:
        service = MVPService(self.settings, self.repository)
        fabricated_current_meal = json.dumps(
            {
                "answer": "我刚刚随手抓了一把营养补充剂，就当是今天的猫饭了。",
                "content_blocks": [
                    {"type": "message", "text": "我刚刚随手抓了一把营养补充剂，就当是今天的猫饭了。"}
                ],
                "confidence": "low",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(fabricated_current_meal, {}), (fabricated_current_meal, {})]
        ):
            result = service.chat(
                "6862c43d2ac9",
                "你吃了什么？",
                session_id="current-food-fact-guard-test",
                communication_channel="text",
            )

        self.assertIn("current_food_guard", result["response_adjustments"])
        self.assertIn("还没决定吃什么", result["answer"])
        self.assertNotIn("刚刚", result["answer"])
        self.assertNotIn("营养补充剂", result["answer"])
        self.assertEqual(result["content_blocks"], [{"type": "message", "text": result["answer"]}])
        self.assertEqual(result["citations"], [])

    def test_mvp_chat_does_not_render_a_blank_provider_response(self) -> None:
        service = MVPService(self.settings, self.repository)
        empty_payload = json.dumps(
            {
                "answer": "",
                "content_blocks": [],
                "confidence": "low",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(empty_payload, {})
        ):
            result = service.chat(
                "25b23cb64398",
                "早安，凯西娅。",
                session_id="empty-provider-output-test",
                communication_channel="text",
            )

        self.assertIn("empty_model_output_guard", result["response_adjustments"])
        self.assertTrue(result["answer"])
        self.assertEqual(result["content_blocks"], [{"type": "message", "text": result["answer"]}])
        self.assertEqual(result["citations"], [])

    def test_mvp_world_scene_is_shared_across_character_sessions(self) -> None:
        service = MVPService(self.settings, self.repository)
        world = service._world_snapshot("shared-world-scene-test")
        cat_mention = service._resolve_character_mentions("猫猫在哪？")
        asked_from_fenny = service._live_scene_context(
            service.character("1b0a6b35719a"),
            "location",
            cat_mention,
            world,
        )
        asked_as_cat = service._live_scene_context(
            service.character("6862c43d2ac9"),
            "location",
            [],
            world,
        )

        self.assertEqual(asked_from_fenny["character_name"], "猫汐尔")
        self.assertEqual(asked_from_fenny["location"], asked_as_cat["location"])
        self.assertEqual(asked_from_fenny["activity"], asked_as_cat["activity"])
        locations = [item["location"] for item in world["presence"].values()]
        self.assertGreater(len(set(locations)), 1)
        self.assertFalse(all(location in {"训练区", "训练场", "训练室"} for location in locations))

    def test_mvp_world_scene_requires_clarification_for_multiple_characters(self) -> None:
        service = MVPService(self.settings, self.repository)
        world = service._world_snapshot("ambiguous-world-scene-test")
        mentions = service._resolve_character_mentions("猫猫和小老师分别在哪？")
        context = service._live_scene_context(
            service.character("ca0144ccd81b"),
            "location",
            mentions,
            world,
        )

        self.assertEqual(context["status"], "ambiguous")
        self.assertEqual(set(context["candidates"]), {"猫汐尔", "芙提雅"})
        violations = service._answer_guardrail_violations(
            "猫猫和小老师分别在哪？",
            "她们都在训练场。",
            {
                "character": service.character("ca0144ccd81b"),
                "mentioned_characters": mentions,
                "companion_social_context": {"active": True},
                "live_scene": context,
                "question_focus": "location",
                "hits": [],
                "dialogue_boundary": {"kind": "standard"},
            },
            "immersive",
        )
        self.assertIn("live_scene_mismatch:ambiguous_subject", violations)

    def test_mvp_companion_social_guard_does_not_change_unrelated_enemy_facts(self) -> None:
        service = MVPService(self.settings, self.repository)
        base_context = {
            "character": service.character("ca0144ccd81b"),
            "mentioned_characters": [],
            "live_scene": None,
            "question_focus": "general",
            "hits": [],
            "dialogue_boundary": {"kind": "standard"},
        }
        inactive = {**base_context, "companion_social_context": {"active": False}}
        active = {**base_context, "companion_social_context": {"active": True}}

        self.assertEqual(
            service._answer_guardrail_violations(
                "那个敌人怎么样？", "她是我的敌人。", inactive, "immersive"
            ),
            [],
        )
        self.assertTrue(
            any(
                item.startswith("companion_hostility:")
                for item in service._answer_guardrail_violations(
                    "猫汐尔怎么样？", "她是我的敌人。", active, "immersive"
                )
            )
        )

    def test_mvp_chat_request_keeps_world_session_optional_for_old_clients(self) -> None:
        legacy = MVPChatRequest(character_id="ca0144ccd81b", message="你在哪？")
        current = MVPChatRequest(
            character_id="ca0144ccd81b",
            message="你在哪？",
            world_session_id="world-contract-test",
        )

        self.assertIsNone(legacy.world_session_id)
        self.assertEqual(current.world_session_id, "world-contract-test")

    def test_mvp_chat_falls_back_to_shared_live_scene_after_two_bad_answers(self) -> None:
        service = MVPService(self.settings, self.repository)
        bad_answer = json.dumps(
            {
                "answer": "猫猫现在在训练场。",
                "confidence": "low",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service,
            "_call_model",
            side_effect=[(bad_answer, {}), (bad_answer, {})],
        ):
            result = service.chat(
                "a2ffc5b44d7f",
                "猫猫在哪？",
                session_id="live-scene-character-session",
                world_session_id="live-scene-shared-world",
            )

        self.assertIn("live_scene_guard", result["response_adjustments"])
        self.assertIn("猫汐尔", result["answer"])
        self.assertIn(result["live_scene"]["location"], result["answer"])
        self.assertEqual(result["citations"], [])

    def test_mvp_chat_falls_back_to_friendly_companion_relationship(self) -> None:
        service = MVPService(self.settings, self.repository)
        hostile_answer = json.dumps(
            {
                "answer": "猫猫是我的敌人，我讨厌她。",
                "confidence": "low",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service,
            "_call_model",
            side_effect=[(hostile_answer, {}), (hostile_answer, {})],
        ):
            result = service.chat(
                "a2ffc5b44d7f",
                "你和猫猫相处得怎么样？",
                session_id="social-guard-character-session",
                world_session_id="social-guard-shared-world",
            )

        self.assertIn("companion_social_guard", result["response_adjustments"])
        self.assertIn("猫汐尔", result["answer"])
        self.assertNotIn("敌人", result["answer"])
        self.assertNotIn("讨厌", result["answer"])
        self.assertEqual(result["citations"], [])

    def test_mvp_immersive_boundary_reframes_meta_costume_questions_only(self) -> None:
        service = MVPService(self.settings, self.repository)
        question = (
            "如果我没有指定时装，你会怎样保持里芙本体的设定？"
            "如果指定某套时装，又应怎样只在对应语境下改变语气？"
        )
        immersive = service._dialogue_boundary(question, "immersive")
        self.assertEqual(immersive["kind"], "meta_system")
        self.assertEqual(immersive["topic"], "costume_context")
        self.assertEqual(immersive["response_policy"], "diegetic_reframe")

        self.assertEqual(
            service._dialogue_boundary("换上静聆潮韵陪我出去走走", "immersive")["kind"],
            "standard",
        )
        self.assertEqual(
            service._dialogue_boundary("换回本体", "immersive")["kind"],
            "standard",
        )
        self.assertEqual(
            service._dialogue_boundary("你知道自己是游戏角色吗？", "immersive")["kind"],
            "meta_system",
        )
        self.assertEqual(
            service._dialogue_boundary("这是一次 rapid response 演练", "immersive")["kind"],
            "standard",
        )
        self.assertEqual(service._dialogue_boundary(question, "assistant")["kind"], "standard")

    def test_mvp_costume_shortcut_is_an_in_world_question(self) -> None:
        costume_questions = [
            question for question in question_bank() if question["category"] == "costume_scope"
        ]
        self.assertEqual(len(costume_questions), len(MVP_CHARACTERS))
        for question in costume_questions:
            self.assertIn("换一套特别的衣服", question["text"])
            for forbidden in ("本体设定", "对应语境", "改变语气", "模型", "检索"):
                self.assertNotIn(forbidden, question["text"])

    def test_mvp_immersive_answer_guard_detects_meta_leaks_and_unsupported_quotes(self) -> None:
        service = MVPService(self.settings, self.repository)
        question = "如果没有指定时装，如何保持本体设定并只在对应语境改变语气？"
        context = {
            "dialogue_boundary": service._dialogue_boundary(question, "immersive"),
            "hits": [],
            "relationship_background": {},
            "dialogue_profile": None,
            "session_context": {},
        }
        answer = "我会保持本体设定，只在对应语境改变语气，就像我说过“这是你喜欢的感觉么？”一样。"
        violations = service._answer_guardrail_violations(question, answer, context, "immersive")
        self.assertTrue(any(item.startswith("immersive_meta_leak:") for item in violations))
        self.assertIn("immersive_meta_answer_used_direct_quote", violations)
        self.assertTrue(any(item.startswith("unsupported_quote:") for item in violations))

        supported_context = {
            **context,
            "dialogue_boundary": {"kind": "standard"},
            "hits": [{"text": "里芙问分析员：这是你喜欢的感觉么？"}],
        }
        self.assertEqual(
            service._answer_guardrail_violations(
                "你当时问了什么？",
                "我当时问的是“这是你喜欢的感觉么？”。",
                supported_context,
                "immersive",
            ),
            [],
        )

    def test_mvp_chat_rewrites_a_meta_costume_answer_without_exposing_mechanisms(self) -> None:
        service = MVPService(self.settings, self.repository)
        question = (
            "如果我没有指定时装，你会怎样保持里芙本体的设定？"
            "如果指定某套时装，又应怎样只在对应语境下改变语气？"
        )
        leaking = json.dumps(
            {
                "answer": "没有指定时装时我会保持本体设定，指定后只在对应语境改变语气。",
                "confidence": "medium",
                "used_document_ids": [],
            },
            ensure_ascii=False,
        )
        rewritten = json.dumps(
            {
                "answer": "如果你没有特别想看的，我就和平时一样。衣服会让心情有些不同，但我不会因此变成另一个人。",
                "confidence": "medium",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service,
            "_call_model",
            side_effect=[
                (leaking, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                (rewritten, {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}),
            ],
        ) as call_model:
            result = service.chat("ca0144ccd81b", question, session_id="meta-boundary-rewrite")

        self.assertEqual(call_model.call_count, 2)
        self.assertIn("answer_guardrail_retry", result["response_adjustments"])
        self.assertNotIn("immersive_boundary_fallback", result["response_adjustments"])
        for forbidden in ("本体设定", "对应语境", "改变语气", "资料库", "提示词"):
            self.assertNotIn(forbidden, result["answer"])
        self.assertEqual(result["usage"]["total_tokens"], 33)

    def test_mvp_chat_uses_world_internal_fallback_if_rewrite_still_leaks(self) -> None:
        service = MVPService(self.settings, self.repository)
        question = "请解释没有指定时装时如何保持本体设定和时装语境。"
        leaking = json.dumps(
            {
                "answer": "系统会读取时装语境并保持角色设定。",
                "confidence": "low",
                "used_document_ids": [],
            },
            ensure_ascii=False,
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service,
            "_call_model",
            side_effect=[(leaking, {}), (leaking, {})],
        ):
            result = service.chat("ca0144ccd81b", question, session_id="meta-boundary-fallback")

        self.assertIn("immersive_boundary_fallback", result["response_adjustments"])
        self.assertEqual(result["citations"], [])
        for forbidden in ("系统", "设定", "语境", "模型", "检索"):
            self.assertNotIn(forbidden, result["answer"])

    def _communication_model_payload(
        self,
        answer: str = "收到。",
        block_type: str = "speech",
        block_text: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "answer": answer,
                "content_blocks": [
                    {"type": block_type, "text": block_text or answer}
                ],
                "confidence": "medium",
                "used_document_ids": [],
                "used_relation_candidate_ids": [],
            },
            ensure_ascii=False,
        )

    def test_mvp_communication_channel_defaults_and_retains_turn_blocks(self) -> None:
        service = MVPService(self.settings, self.repository)
        payload = self._communication_model_payload()
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat("ca0144ccd81b", "你好", session_id="channel-default-test")
        self.assertEqual(result["communication_channel"], "in_person")
        self.assertEqual(result["content_blocks"][0]["type"], "speech")
        self.assertTrue(any(item["type"] == "speech" for item in result["content_blocks"]))
        self.assertEqual(result["scene_state"]["co_located"], True)
        snapshot = service._session_snapshot("channel-default-test", "ca0144ccd81b", "immersive")
        self.assertEqual(snapshot["communication_channel"], "in_person")
        self.assertEqual(snapshot["turns"][0]["communication_channel"], "in_person")
        self.assertEqual(snapshot["turns"][0]["content_blocks"][0]["type"], "speech")

    def test_mvp_text_channel_bypasses_location_and_accepts_message_blocks(self) -> None:
        service = MVPService(self.settings, self.repository)
        payload = self._communication_model_payload("我看到了你的消息。", "message")
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                "ca0144ccd81b",
                "现在方便聊聊吗？",
                session_id="channel-text-test",
                communication_channel="text",
                world_session_id="channel-text-world",
            )
        self.assertEqual(result["communication_channel"], "text")
        self.assertEqual(result["content_blocks"][0]["type"], "message")
        self.assertFalse(result["scene_state"]["co_located"])

    def test_mvp_face_to_face_different_location_returns_structured_conflict(self) -> None:
        service = MVPService(self.settings, self.repository)
        world_id = "channel-conflict-world"
        world = service._world_snapshot(world_id)
        selected = list(world["presence"].values())
        self.assertGreaterEqual(len(selected), 2)
        first, second = selected[0], selected[1]
        service._set_analyst_location(world_id, first["location"])
        with patch.object(service, "chat_enabled", return_value=True):
            with self.assertRaises(Exception) as caught:
                service.chat(
                    second["character_id"],
                    "我们见面聊吧。",
                    session_id="channel-conflict-session",
                    world_session_id=world_id,
                    communication_channel="in_person",
                )
        self.assertEqual(getattr(caught.exception, "detail", {}).get("code"), "communication_context_conflict")
        options = getattr(caught.exception, "detail", {}).get("options") or []
        self.assertEqual({item["action"] for item in options}, {"join_character", "switch_to_text"})
        detail = getattr(caught.exception, "detail", {})
        self.assertIn("character_reply", detail)
        self.assertEqual(detail["content_blocks"][0]["type"], "speech")

    def test_mvp_join_character_resolves_conflict_and_reports_presence_transition(self) -> None:
        service = MVPService(self.settings, self.repository)
        world_id = "channel-join-world"
        world = service._world_snapshot(world_id)
        selected = list(world["presence"].values())
        first, second = selected[0], selected[1]
        service._set_analyst_location(world_id, first["location"])
        payload = self._communication_model_payload()
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            result = service.chat(
                second["character_id"],
                "过来见我吧。",
                session_id="channel-join-session",
                world_session_id=world_id,
                communication_channel="in_person",
                presence_action="join_character",
            )
        self.assertTrue(result["scene_state"]["co_located"])
        self.assertEqual(result["scene_state"]["presence_transition"]["status"], "joined_character")

    def test_mvp_presence_resolve_is_idempotent_and_scoped_to_one_character(self) -> None:
        service = MVPService(self.settings, self.repository)
        first = service.resolve_presence("ca0144ccd81b", "presence-resolve-world")
        second = service.resolve_presence("ca0144ccd81b", "presence-resolve-world")
        self.assertEqual(first, second)
        self.assertEqual(first["character_id"], "ca0144ccd81b")
        self.assertIn(first["scene_state"]["visual_key"], {
            "quarters", "lounge", "training", "archive", "canteen",
            "observation", "medical", "corridor", "generic",
        })
        self.assertNotIn("presence", first)

    def test_mvp_presence_transition_changes_state_without_model_or_message(self) -> None:
        service = MVPService(self.settings, self.repository)
        world_id = "presence-transition-world"
        resolved = service.resolve_presence("ca0144ccd81b", world_id)
        target_location = resolved["scene_state"]["character_location"]
        joined = service.transition_presence(
            "ca0144ccd81b",
            session_id=None,
            world_session_id=world_id,
            target_channel="in_person",
            action="join_character",
        )
        self.assertTrue(joined["scene_state"]["co_located"])
        self.assertEqual(joined["scene_state"]["analyst_location"], target_location)
        self.assertFalse(joined["message_created"])
        self.assertFalse(joined["model_called"])
        communicator = service.transition_presence(
            "ca0144ccd81b",
            session_id=None,
            world_session_id=world_id,
            target_channel="text",
            action="open_communicator",
        )
        self.assertEqual(communicator["scene_state"]["analyst_location"], target_location)
        self.assertEqual(communicator["communication_channel"], "text")

    def test_mvp_presence_transition_rejects_mismatched_action(self) -> None:
        service = MVPService(self.settings, self.repository)
        with self.assertRaisesRegex(ValueError, "场景动作与目标交流媒介不匹配"):
            service.transition_presence(
                "ca0144ccd81b",
                session_id=None,
                world_session_id="presence-invalid-world",
                target_channel="text",
                action="join_character",
            )

    def test_mvp_presence_arrival_unnoticed_is_idempotent_and_skips_model(self) -> None:
        service = MVPService(self.settings, self.repository)
        with tempfile.TemporaryDirectory() as temporary_directory:
            service.conversation_store = ConversationStore(
                Path(temporary_directory) / "arrival.sqlite3"
            )
            with patch(
                "backend.snow_app.mvp_service.secrets.randbelow", return_value=1
            ) as random_draw, patch.object(service, "chat") as chat:
                first = service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_unnoticed_test",
                    session_id="arrival_unnoticed_session",
                    world_session_id="arrival_unnoticed_world",
                )["ready"]
                replay = service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_unnoticed_test",
                    session_id="arrival_unnoticed_session",
                    world_session_id="arrival_unnoticed_world",
                )["ready"]

            self.assertEqual(first, replay)
            self.assertEqual(first["decision"], "unnoticed")
            self.assertEqual(first["status"], "completed")
            self.assertIsNone(first["reaction"])
            self.assertEqual(random_draw.call_count, 1)
            chat.assert_not_called()
            history = service.conversation_store.history(
                "ca0144ccd81b", session_id="arrival_unnoticed_session"
            )
            self.assertEqual(history["messages"], [])

    def test_mvp_presence_arrival_noticed_persists_assistant_only_once(self) -> None:
        service = MVPService(self.settings, self.repository)
        with tempfile.TemporaryDirectory() as temporary_directory:
            service.conversation_store = ConversationStore(
                Path(temporary_directory) / "arrival.sqlite3"
            )
            with patch(
                "backend.snow_app.mvp_service.secrets.randbelow", return_value=0
            ) as random_draw:
                prepared = service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_noticed_test",
                    session_id="arrival_noticed_session",
                    world_session_id="arrival_noticed_world",
                )
                with patch.object(
                    service,
                    "chat",
                    return_value={
                        "answer": "（我抬头看向你。）\n你来了。刚才说到的事，我们接着聊吧。",
                        "content_blocks": [
                            {"type": "action", "text": "我抬头看向你。"},
                            {"type": "speech", "text": "你来了。刚才说到的事，我们接着聊吧。"},
                        ],
                        "usage": {"total_tokens": 20},
                        "actual_model": {"model_name": "deepseek-v4-flash"},
                        "routing_decision": {"reason": "immersive_text_default"},
                        "thinking_decision": {"effective": "off"},
                        "style_context": None,
                        "response_adjustments": [],
                    },
                ) as chat:
                    first = service.finish_presence_arrival(
                        prepared,
                        model_settings=(
                            "https://api.deepseek.com/v1",
                            "credential",
                            "deepseek-v4-flash",
                        ),
                        model_info={"model_name": "deepseek-v4-flash"},
                        thinking_decision={"effective": "off"},
                    )
                replay = service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_noticed_test",
                    session_id="arrival_noticed_session",
                    world_session_id="arrival_noticed_world",
                )["ready"]

            self.assertEqual(first, replay)
            self.assertEqual(random_draw.call_count, 1)
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(first["decision"], "noticed")
            self.assertEqual(first["reaction"]["source"], "presence_arrival")
            self.assertEqual(first["reaction"]["arrival_id"], "arrival_noticed_test")
            self.assertEqual(
                first["reaction"]["content_blocks"],
                [
                    {"type": "action", "text": f"{first['character_name']}抬头看向你。"},
                    {"type": "speech", "text": "你来了。刚才说到的事，我们接着聊吧。"},
                ],
            )
            self.assertEqual(first["reaction"]["answer"], "你来了。刚才说到的事，我们接着聊吧。")
            history = service.conversation_store.history(
                "ca0144ccd81b", session_id="arrival_noticed_session"
            )
            self.assertEqual([item["role"] for item in history["messages"]], ["assistant"])
            self.assertEqual(
                history["messages"][0]["response"]["source"], "presence_arrival"
            )

    def test_mvp_presence_arrival_rejects_reuse_and_reports_processing(self) -> None:
        service = MVPService(self.settings, self.repository)
        with tempfile.TemporaryDirectory() as temporary_directory:
            service.conversation_store = ConversationStore(
                Path(temporary_directory) / "arrival.sqlite3"
            )
            with patch("backend.snow_app.mvp_service.secrets.randbelow", return_value=0):
                service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_processing_test",
                    session_id="arrival_processing_session",
                    world_session_id="arrival_processing_world",
                )
                with self.assertRaises(MVPRequestInProgress):
                    service.prepare_presence_arrival(
                        "ca0144ccd81b",
                        arrival_id="arrival_processing_test",
                        session_id="arrival_processing_session",
                        world_session_id="arrival_processing_world",
                    )
                with self.assertRaisesRegex(ValueError, "arrival_id"):
                    service.prepare_presence_arrival(
                        MVP_CHARACTERS[1].character_id,
                        arrival_id="arrival_processing_test",
                        session_id="arrival_processing_session_other",
                        world_session_id="arrival_processing_world",
                    )

    def test_mvp_presence_arrival_empty_model_output_falls_back_unnoticed(self) -> None:
        service = MVPService(self.settings, self.repository)
        with tempfile.TemporaryDirectory() as temporary_directory:
            service.conversation_store = ConversationStore(
                Path(temporary_directory) / "arrival.sqlite3"
            )
            with patch("backend.snow_app.mvp_service.secrets.randbelow", return_value=0):
                prepared = service.prepare_presence_arrival(
                    "ca0144ccd81b",
                    arrival_id="arrival_empty_model_test",
                    session_id="arrival_empty_model_session",
                    world_session_id="arrival_empty_model_world",
                )
            with patch.object(
                service,
                "chat",
                return_value={
                    "answer": "本地伪造兜底",
                    "response_adjustments": ["empty_model_output_guard"],
                },
            ):
                with self.assertRaises(MVPProviderError):
                    service.finish_presence_arrival(
                        prepared,
                        model_settings=("base", "credential", "deepseek-v4-flash"),
                        model_info={"model_name": "deepseek-v4-flash"},
                        thinking_decision={"effective": "off"},
                    )
            fallback = service.fallback_presence_arrival(prepared)
            self.assertEqual(fallback["decision"], "unnoticed")
            self.assertEqual(fallback["status"], "fallback_unnoticed")
            self.assertIsNone(fallback["reaction"])
            history = service.conversation_store.history(
                "ca0144ccd81b", session_id="arrival_empty_model_session"
            )
            self.assertEqual(history["messages"], [])

    def test_mvp_text_action_block_is_rewritten_to_deterministic_fallback(self) -> None:
        service = MVPService(self.settings, self.repository)
        payload = self._communication_model_payload(
            "我抱住了你。",
            "action",
            "我抱住了你。",
        )
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", side_effect=[(payload, {}), (payload, {})]
        ):
            result = service.chat(
                "ca0144ccd81b",
                "我想和你聊天。",
                session_id="channel-guard-test",
                communication_channel="text",
            )
        self.assertIn("communication_guard", result["response_adjustments"])
        self.assertEqual(result["content_blocks"][0]["type"], "message")
        self.assertEqual(result["citations"], [])

    def test_mvp_dialogue_switch_happens_after_reply_and_current_declaration_is_immediate(self) -> None:
        service = MVPService(self.settings, self.repository)
        payload = self._communication_model_payload()
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(payload, {})
        ):
            first = service.chat(
                "ca0144ccd81b",
                "你好",
                session_id="channel-transition-session",
                communication_channel="in_person",
            )
            after_reply = service.chat(
                "ca0144ccd81b",
                "我们改用通讯器聊吧。",
                session_id="channel-transition-session",
            )
            immediate = service.chat(
                "ca0144ccd81b",
                "我现在正在用通讯器给你发消息。",
                session_id="channel-immediate-session",
            )
        self.assertEqual(first["communication_channel"], "in_person")
        self.assertEqual(after_reply["communication_channel"], "in_person")
        self.assertEqual(after_reply["channel_transition"]["status"], "applied_after_reply")
        self.assertEqual(after_reply["channel_transition"]["to"], "text")
        self.assertEqual(immediate["communication_channel"], "text")
        self.assertEqual(immediate["channel_transition"]["status"], "applied_immediately")

    def test_mvp_channel_request_with_present_context_is_not_immediate(self) -> None:
        """A switch request must not be confused with a current-channel declaration."""

        transition = MVPService._dialogue_channel_transition(
            "\u6211\u4eec\u6539\u7528\u901a\u8baf\u5668\u804a\u5427\uff0c\u73b0\u5728\u611f\u89c9\u600e\u4e48\u6837\uff1f",
            "in_person",
        )
        self.assertEqual(transition["status"], "applied_after_reply")
        self.assertEqual(transition["to"], "text")

    def test_mvp_recent_activity_question_exposes_live_scene(self) -> None:
        """Natural "刚刚/刚才在做什么" wording must answer the activity directly."""

        service = MVPService(self.settings, self.repository)
        world = service._world_snapshot("recent-activity-world")
        scene = service._scene_state(world, service.character("ca0144ccd81b"))
        for message in (
            "你刚刚在做什么？",
            "刚才在干嘛？",
            "方才在做什么？",
            "你刚刚做了什么？",
            "你刚才忙什么？",
        ):
            intents = service._query_intents(message)
            self.assertEqual(service._question_focus(message, intents), "current_activity")
            context = service.retrieve(
                "ca0144ccd81b",
                message,
                limit=4,
                world_state=world,
                mode="immersive",
                session_context={},
            )
            self.assertEqual(context["question_focus"], "current_activity")
            self.assertEqual(
                context["live_scene"]["activity"],
                world["presence"]["ca0144ccd81b"]["activity"],
            )
            prompt_scene = service._scene_state_for_prompt(
                scene,
                message,
                "current_activity",
            )
            self.assertEqual(prompt_scene["activity_visibility"], "visible_for_current_turn")

    def test_mvp_current_activity_fallback_hides_unasked_location(self) -> None:
        context = {
            "question_focus": "current_activity",
            "live_scene": {
                "status": "active",
                "subject_role": "self",
                "location": "医务室",
                "activity": "刚完成例行检查",
            },
            "raw_scene_state": {
                "character_location": "医务室",
                "character_activity": "刚完成例行检查",
            },
        }
        answer = MVPService._scene_privacy_fallback(
            "我现在在医务室，刚完成例行检查。", context
        )
        self.assertIn("刚完成例行检查", answer)
        self.assertNotIn("医务室", answer)

    def test_mvp_text_channel_rejects_unseen_audio_claim(self) -> None:
        violations = MVPService._communication_block_violations(
            "\u6211\u6b63\u5728\u53d1\u6587\u5b57\u6d88\u606f",
            "\u6211\u542c\u5230\u4e86\u4f60\u7684\u58f0\u97f3",
            "text",
            [{"type": "message", "text": "\u6211\u542c\u5230\u4e86\u4f60\u7684\u58f0\u97f3"}],
        )
        self.assertIn("text_channel_unseen_audio:\u542c\u5230\u4e86\u4f60\u7684\u58f0\u97f3", violations)

    def test_retrieval_identity_is_always_analyst(self) -> None:
        client = TestClient(app)
        original_vector_search = repository.vector_search
        repository.vector_search = lambda query, limit=40: []
        try:
            response = client.post("/api/v1/retrieval/preview", json={"query": "琴诺与分析员的关系"})
        finally:
            repository.vector_search = original_vector_search
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_identity"]["user_role"], "分析员")

    def test_chat_endpoint_is_stage_locked(self) -> None:
        client = TestClient(app)
        response = client.post("/api/v1/chat")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "conversation_stage_locked")

    def test_mvp_relationship_roster_guard_uses_audited_eight_names(self) -> None:
        question = "\u73b0\u5728\u5df2\u7ecf\u548c\u6211\u6052\u7ea6\u7684\u4f60\u77e5\u9053\u6709\u54ea\u51e0\u4f4d\u5417"
        self.assertTrue(MVPService._is_relationship_roster_question(question))
        violations = MVPService._relationship_roster_violations(
            question,
            "\u6211\u662f\u4f60\u7684\u59bb\u5b50\uff0c\u6069\u96c5\u4e5f\u662f\u3002",
        )
        self.assertIn("relationship_roster_missing", violations)
        self.assertIn("relationship_roster_excluded:\u6069\u96c5", violations)
        fallback = MVPService._relationship_roster_fallback()
        for name in ("\u91cc\u8299", "\u82ac\u59ae", "\u51ef\u831c\u5a05", "\u82d4\u4e1d", "\u80b4", "\u8309\u8389\u5b89", "\u5b89\u5361\u5e0c\u96c5", "\u8fb0\u661f"):
            self.assertIn(name, fallback)
        self.assertNotIn("\u6069\u96c5", fallback)

    def test_mvp_current_food_guard_catches_just_ate_claim(self) -> None:
        violations = MVPService._unsupported_current_food_claims(
            "\u5526\u2026\u521a\u5403\u4e86\u70b9\u4e1c\u897f\uff0c\u4e0d\u8fc7\u6ca1\u4ec0\u4e48\u7279\u522b\u7684\u5473\u9053\u55b5\u2026",
            {"question_focus": "food_or_drink"},
        )
        self.assertEqual(violations, ["unsupported_current_food_fact"])

    @pytest.mark.runtime_data
    def test_mvp_runtime_logistics_projection_recovers_old_links(self) -> None:
        service = MVPService(self.settings, self.repository)
        documents = service._runtime_documents_by_id()
        links = set()
        members = set()
        for document in documents.values():
            if document.get("source_type") != "logistics_lore":
                continue
            metadata = document.get("metadata") or {}
            if "d5ecfceba959" in service._document_character_ids(document):
                links.update(
                    (item.get("character_name"), item.get("armor_name"))
                    for item in metadata.get("logistics_relationships", [])
                )
                members.update(metadata.get("_runtime_logistics_members") or [])
        self.assertIn(("\u514b\u7f57\u745e\u5a1c", "\u7eef\u6708"), links)
        self.assertTrue(members)

    def test_mvp_feedback_issue_labels_mark_duplicates_without_rewriting_source(self) -> None:
        service = MVPService(self.settings, self.repository)
        rows = [
            {
                "feedback_id": "one",
                "created_at": "2026-01-01T00:00:00+00:00",
                "category": "character_portrayal",
                "free_text": "\u6052\u7ea6\u89d2\u8272\u5e94\u8be5\u53eb\u4eb2\u7231\u7684",
            },
            {
                "feedback_id": "two",
                "created_at": "2026-01-02T00:00:00+00:00",
                "category": "character_portrayal",
                "free_text": "\u6052\u7ea6\u89d2\u8272\u4e0d\u5e94\u53eb\u5206\u6790\u5458",
            },
        ]
        annotated = service._annotate_feedback_rows(rows, {})
        self.assertEqual(annotated[0]["issue_key"], "formal_relationship_address")
        self.assertEqual(annotated[1]["issue_occurrence"], "duplicate")
        self.assertEqual(annotated[1]["duplicate_of"], "one")

    def test_mvp_feedback_legacy_buckets_are_reclassified_without_reopening_fixes(self) -> None:
        service = MVPService(self.settings, self.repository)
        rows = [
            {
                "feedback_id": "composer",
                "created_at": "2026-01-01T00:00:00+00:00",
                "issue_key": "client_input_state",
                "category": "client_function",
                "free_text": "动作和对白应该支持同时输入",
            },
            {
                "feedback_id": "markdown",
                "created_at": "2026-01-02T00:00:00+00:00",
                "issue_key": "client_input_state",
                "category": "client_function",
                "free_text": "助手疑似不兼容 Markdown 格式",
            },
        ]

        annotated = service._annotate_feedback_rows(rows, {})

        self.assertEqual(annotated[0]["issue_key"], "composer_action_and_speech")
        self.assertEqual(annotated[0]["issue_status"], "fixed_verified")
        self.assertTrue(annotated[0]["verification_tests"])
        self.assertEqual(annotated[1]["issue_key"], "assistant_markdown")
        self.assertEqual(
            annotated[1]["issue_status"], "superseded_by_architecture"
        )

    def test_mvp_repeated_feedback_keeps_verified_status_until_explicitly_reopened(self) -> None:
        service = MVPService(self.settings, self.repository)
        rows = [
            {
                "feedback_id": "before-verification",
                "created_at": "2026-01-01T00:00:00+00:00",
                "category": "client_function",
                "free_text": "动作和对白应该支持同时输入",
            },
            {
                "feedback_id": "after-verification",
                "created_at": "2026-01-03T00:00:00+00:00",
                "category": "client_function",
                "free_text": "动作和对白应该支持同时输入",
            },
        ]
        verified_event = {
            "composer_action_and_speech": {
                "status": "fixed_verified",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "verified_at": "2026-01-02T00:00:00+00:00",
                "source": "manual",
                "verification_tests": ["test_current_version_reproduction"],
            }
        }

        annotated = service._annotate_feedback_rows(rows, verified_event)

        self.assertEqual(annotated[1]["resolution_status"], "fixed_verified")
        self.assertEqual(annotated[1]["issue_occurrence"], "duplicate")
        self.assertEqual(annotated[1]["issue_report_count"], 2)
        self.assertEqual(annotated[1]["recurrence_index"], 1)
        self.assertTrue(annotated[1]["reported_after_verification"])

        reopened = service._annotate_feedback_rows(
            rows,
            {
                "composer_action_and_speech": {
                    "status": "open",
                    "updated_at": "2026-01-04T00:00:00+00:00",
                    "source": "manual",
                    "verification_tests": ["test_current_version_reproduction"],
                }
            },
        )
        self.assertEqual(reopened[1]["resolution_status"], "open")
        self.assertFalse(reopened[1]["reported_after_verification"])

    def test_mvp_parser_extracts_answer_from_truncated_json_without_leaking_syntax(self) -> None:
        parsed = _parse_model_json('{"answer":"自然答复","')
        self.assertEqual(parsed["answer"], "自然答复")
        self.assertNotIn('{"answer"', parsed["answer"])

    def test_mvp_parser_unwraps_quoted_truncated_envelope(self) -> None:
        """A gateway may JSON-encode a truncated envelope one extra time."""
        payload = json.dumps('{"answer":"自然答复","', ensure_ascii=False)
        parsed = _parse_model_json(payload)
        self.assertEqual(parsed["answer"], "自然答复")
        self.assertNotIn('{"answer"', parsed["answer"])

    def test_mvp_parser_does_not_expose_single_quote_envelope(self) -> None:
        parsed = _parse_model_json("{'answer':'自然答复','confidence':'low'}")
        self.assertEqual(parsed["answer"], "自然答复")
        self.assertNotIn("'answer'", parsed["answer"])

    def test_mvp_parser_strips_fence_or_gateway_preamble_from_truncated_envelope(self) -> None:
        for payload in (
            '```json\n{"answer":"自然答复","\n```',
            'Here is the JSON: {"answer":"自然答复","',
        ):
            parsed = _parse_model_json(payload)
            self.assertEqual(parsed["answer"], "自然答复")
            self.assertNotIn("answer", parsed["answer"])

    def test_mvp_content_blocks_never_render_structured_envelope_text(self) -> None:
        service = MVPService(self.settings, self.repository)
        payload = {
            "answer": '{"answer":"可读答复","',
            "content_blocks": [{"type": "speech", "text": '{"answer":"可读答复","'}],
        }
        blocks = service._normalize_content_blocks(payload, "in_person", payload["answer"])
        self.assertEqual(blocks, [{"type": "speech", "text": "可读答复"}])
        self.assertEqual(service._render_content_blocks(blocks), "可读答复")

    def test_mvp_generated_answer_rejects_non_string_answer_and_unknown_json(self) -> None:
        service = MVPService(self.settings, self.repository)
        # A schema-invalid mapping must never be converted to Python repr and
        # shown as if it were dialogue.
        self.assertEqual(
            service._generated_answer({"answer": {"text": "内部字段"}}, '{"answer":{"text":"内部字段"}}'),
            "",
        )
        self.assertEqual(
            service._generated_answer({"metadata": {"trace": "internal"}}, '{"metadata":{"trace":"internal"}}'),
            "",
        )

    def test_mvp_generated_answer_keeps_plain_non_json_fallback(self) -> None:
        service = MVPService(self.settings, self.repository)
        self.assertEqual(
            service._generated_answer({"provider_note": "ignored"}, "这是一条普通回复"),
            "这是一条普通回复",
        )

    def test_mvp_chat_sanitizes_nested_truncated_envelope_before_persistence(self) -> None:
        service = MVPService(self.settings, self.repository)
        nested = json.dumps('{"answer":"可读答复","', ensure_ascii=False)
        with patch.object(service, "chat_enabled", return_value=True), patch.object(
            service, "_call_model", return_value=(nested, {})
        ):
            result = service.chat(
                "ca0144ccd81b",
                "你好",
                session_id="nested-truncated-envelope-test",
                communication_channel="text",
            )
        self.assertEqual(result["answer"], "可读答复")
        self.assertNotIn('{"answer"', result["answer"])
        self.assertEqual(result["content_blocks"], [{"type": "message", "text": "可读答复"}])

    def test_mvp_history_sanitizes_legacy_structured_answer_on_read(self) -> None:
        service = MVPService(self.settings, self.repository)
        stored = {
            "conversation": {"conversation_id": "conversation-legacy"},
            "messages": [{
                "cursor": 1,
                "message_id": "legacy-answer",
                "role": "assistant",
                "mode": "immersive",
                "communication_channel": "text",
                "text": '{"answer":"旧回复","',
                "content_blocks": [{"type": "message", "text": '{"answer":"旧回复","'}],
                "response": {"answer": '{"answer":"旧回复","'},
            }],
            "next_before": None,
        }
        with patch.object(service.conversation_store, "history", return_value=stored):
            result = service.conversation_history("ca0144ccd81b")
        message = result["messages"][0]
        self.assertEqual(message["text"], "旧回复")
        self.assertEqual(message["content_blocks"], [{"type": "message", "text": "旧回复"}])

    def test_mvp_empty_provider_fallback_keeps_narrative_thread(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "question_focus": "past_experience",
            "user_message": "你还记得那次和罗赞的决战吗？",
            "session_context": {
                "turns": [{"user": "我们刚才聊到你的过去", "assistant": "我记得。"}]
            },
        }
        answer = service._empty_model_output_fallback(context)
        self.assertIn("接着", answer)
        self.assertNotIn("想先和我说什么", answer)

    def test_mvp_explicit_morso_question_has_persona_context_in_empty_fallback(self) -> None:
        service = MVPService(self.settings, self.repository)
        context = {
            "character": service.character("8d5b5c3912bb"),
            "user_message": "莫尔索呢，她还好吗？",
            "dual_persona_context": service._dual_persona_context(
                "8d5b5c3912bb", "莫尔索呢，她还好吗？"
            ),
        }
        answer = service._empty_model_output_fallback(context)
        self.assertIn("莫尔索", answer)

    def test_mvp_feedback_issue_key_ignores_unrelated_answer_excerpt(self) -> None:
        service = MVPService(self.settings, self.repository)
        key = service._feedback_issue_key(
            {
                "category": "client_function",
                "free_text": "希望增加主动神态描写",
                "message_excerpt": "现在你应该叫我什么？",
                "answer_excerpt": "是的，我是你的妻子。",
            }
        )
        self.assertEqual(key, "client_input_state")

    @pytest.mark.runtime_data
    def test_mvp_chat_normalizes_relationship_address_in_both_modes(self) -> None:
        service = MVPService(self.settings, self.repository)
        expected = {
            '673ba6851b05': '亲爱的',
            'cf0569ac6de9': '郎君',
            'daab0f4cceb4': '亲爱的',
            '25b23cb64398': '亲爱的',
        }
        for character_id, preferred in expected.items():
            for mode in ('immersive', 'assistant'):
                payload = self._communication_model_payload('早安，分析员。', 'message')
                with patch.object(service, 'chat_enabled', return_value=True), patch.object(
                    service, '_call_model', return_value=(payload, {})
                ):
                    result = service.chat(
                        character_id, '早安',
                        session_id=f'mode-address-chat-{character_id}-{mode}',
                        mode=mode, communication_channel='text'
                    )
                self.assertEqual(result['answer'], f'早安，{preferred}。')
                self.assertIn('relationship_address_normalized', result['response_adjustments'])


if __name__ == "__main__":
    unittest.main()
