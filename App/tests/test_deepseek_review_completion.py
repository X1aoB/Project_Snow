from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.snow_app.config import Settings
from backend.snow_app.deepseek_review_completion import (
    COMPLETION_POLICY_VERSION,
    DeepSeekReviewCompletionService,
)
from backend.snow_app.repository import RuntimeRepository, _read_jsonl, _review_node_id


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class FakeSelection:
    provider_id = "deepseek"
    provider_kind = "deepseek"
    model_name = "deepseek-v4-pro"
    base_url = "https://api.deepseek.test/v1"


class FakeRegistry:
    def route(self, *_args, **_kwargs):
        return FakeSelection()

    def credential_for_selection(self, _selection):
        return "vault-key-never-persisted"

    @staticmethod
    def thinking_request_fields(_provider_kind, _effective):
        return {"thinking": {"type": "enabled"}}


class DeepSeekReviewCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            data_root=root / "data",
            runtime_root=root / "runtime",
            chat_enabled=False,
            embedding_model="local-test-model",
            allowed_origins=["http://localhost"],
        )
        self.settings.data_root.mkdir(parents=True)
        document = {
            "document_id": "doc-1",
            "page_id": "page-1",
            "title": "游乐园约会",
            "source_type": "character_story",
            "text": "任务结束后，晴和分析员来到星光游乐园，并一起乘坐摩天轮。",
        }
        relation = {
            "candidate_id": "relation-candidate-1",
            "subject": "晴藏锋",
            "relation_type": "VISITS_LOCATION",
            "object": "星光游乐园",
            "source_type": "character_story",
            "evidence_quote": "晴和分析员来到星光游乐园",
            "evidence_document_ids": ["doc-1"],
            "review_status": "needs_human_review",
            "automation_run_id": "qwen-source-run",
        }
        untouched = {
            "candidate_id": "already-approved",
            "subject": "晴",
            "relation_type": "VISITS_LOCATION",
            "object": "星光游乐园",
            "source_type": "character_story",
            "evidence_quote": "晴和分析员来到星光游乐园",
            "evidence_document_ids": ["doc-1"],
            "review_status": "approved",
        }
        entity = {
            "entity_candidate_id": "entity-candidate-1",
            "entity_name": "星光游乐园",
            "normalized_name": "星光游乐园",
            "proposed_node_type": "location",
            "proposed_node_id": _review_node_id("location", "星光游乐园"),
            "review_status": "needs_human_review",
            "automation_run_id": "qwen-source-run",
            "relation_candidate_ids": ["relation-candidate-1"],
            "source_types": ["character_story"],
            "evidence_document_ids": ["doc-1"],
            "evidence_page_ids": ["page-1"],
            "evidence_examples": [{
                "relation_candidate_id": "relation-candidate-1",
                "subject": "晴藏锋",
                "relation_type": "VISITS_LOCATION",
                "object": "星光游乐园",
                "source_type": "character_story",
                "evidence_quote": "晴和分析员来到星光游乐园",
            }],
        }
        write_jsonl(self.settings.runtime_root / "lakehouse" / "documents.jsonl", [document])
        write_jsonl(self.settings.runtime_root / "review" / "narrative_relation_candidates.jsonl", [relation, untouched])
        write_jsonl(self.settings.runtime_root / "review" / "entity_node_candidates.jsonl", [entity])
        write_jsonl(self.settings.runtime_root / "graph" / "nodes.jsonl", [
            {"node_id": "character:qing", "node_type": "character", "name": "晴"},
        ])
        write_jsonl(self.settings.runtime_root / "graph" / "edges.jsonl", [])
        write_jsonl(self.settings.runtime_root / "review" / "approved_entity_nodes.jsonl", [])
        write_jsonl(self.settings.runtime_root / "review" / "approved_narrative_edges.jsonl", [])
        self.repository = RuntimeRepository(self.settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def completion(kind: str, _system: str, payload: dict) -> tuple[dict, dict]:
        if kind == "entity":
            identifier = payload["candidate"]["entity_candidate_id"]
            return ({
                "entity_candidate_id": identifier,
                "decision": "approve",
                "confidence": 0.98,
                "canonical_name": "星光游乐园",
                "node_type": "location",
                "supporting_quote": "来到星光游乐园",
                "reason": "原文明确将其作为到访地点。",
            }, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        identifier = payload["candidate"]["candidate_id"]
        return ({
            "candidate_id": identifier,
            "decision": "approve",
            "confidence": 0.97,
            "supporting_quote": "晴和分析员来到星光游乐园",
            "subject_endpoint": {
                "action": "use_existing", "node_id": "character:qing", "node_type": "character", "name": "晴",
            },
            "object_endpoint": {
                "action": "use_existing",
                "node_id": _review_node_id("location", "星光游乐园"),
                "node_type": "location",
                "name": "星光游乐园",
            },
            "reason": "原文直接描述到访。",
        }, {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180})

    def test_run_admit_and_rollback_are_auditable(self) -> None:
        service = DeepSeekReviewCompletionService(
            self.settings,
            self.repository,
            registry=FakeRegistry(),
            completion=self.completion,
        )
        estimate = service.estimate()
        self.assertEqual(estimate["request_count"], 2)
        run = service.create_run(estimate["selection_hash"])
        run = service.run(run["run_id"], concurrency=2)
        self.assertEqual(run["status"], "ready_to_admit")
        self.assertEqual(run["model_decision_coverage"], 1.0)
        self.assertNotIn("vault-key", json.dumps(run))

        admitted = service.admit(run["run_id"])
        self.assertEqual(admitted["status"], "admitted")
        self.assertEqual(admitted["final_decision_coverage"], 1.0)
        relations = _read_jsonl(self.repository.review_candidates_path)
        self.assertEqual(relations[0]["review_status"], "approved")
        self.assertEqual(relations[0]["decision_source"], "deepseek_v4_pro_high_coverage")
        self.assertEqual(relations[1]["review_status"], "approved")
        nodes = _read_jsonl(self.repository.approved_entity_nodes_path)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["review_policy_version"], COMPLETION_POLICY_VERSION)
        edges = _read_jsonl(self.repository.reviewed_edges_path)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["from_id"], "character:qing")

        rolled_back = service.rollback(run["run_id"])
        self.assertEqual(rolled_back["status"], "rolled_back")
        relations = _read_jsonl(self.repository.review_candidates_path)
        self.assertEqual(relations[0]["review_status"], "needs_human_review")
        self.assertEqual(relations[1]["review_status"], "approved")
        self.assertEqual(_read_jsonl(self.repository.approved_entity_nodes_path), [])
        self.assertEqual(_read_jsonl(self.repository.reviewed_edges_path), [])

    def test_provider_failure_becomes_final_rejection(self) -> None:
        def failing_completion(_kind: str, _system: str, _payload: dict):
            raise ValueError("invalid response")

        service = DeepSeekReviewCompletionService(
            self.settings,
            self.repository,
            registry=FakeRegistry(),
            completion=failing_completion,
        )
        run = service.create_run()
        with patch("backend.snow_app.deepseek_review_completion.time.sleep"):
            completed = service.run(run["run_id"], concurrency=2)
        self.assertEqual(completed["status"], "ready_to_admit")
        self.assertEqual(completed["report_summary"]["provider_failed_default_reject"], 2)
        admitted = service.admit(run["run_id"])
        self.assertEqual(admitted["final_decision_coverage"], 1.0)
        self.assertTrue(all(
            row["review_status"] == "rejected"
            for row in _read_jsonl(self.repository.review_candidates_path)
            if row["candidate_id"] == "relation-candidate-1"
        ))
        self.assertEqual(_read_jsonl(self.repository.entity_candidates_path)[0]["review_status"], "rejected")


if __name__ == "__main__":
    unittest.main()
