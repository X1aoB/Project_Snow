from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.snow_app.main as snow_main
from backend.snow_app.config import Settings
from backend.snow_app.repository import RuntimeRepository
from backend.snow_app.review_automation import (
    CALIBRATION_QUOTAS,
    ReviewAutomationService,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class FakeBatchClient:
    def __init__(self) -> None:
        self.uploaded: list[list[dict]] = []
        self.batches: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.cancelled: list[str] = []

    def upload(self, path: Path) -> dict:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.uploaded.append(rows)
        return {"id": f"file-input-{len(self.uploaded)}", "status": "uploaded"}

    def create(self, input_file_id: str, endpoint: str, metadata: dict[str, str]) -> dict:
        rows = self.uploaded[-1]
        number = len(self.batches) + 1
        batch_id = f"batch-{number}"
        output_file_id = f"file-output-{number}"
        output: list[dict] = []
        for row in reversed(rows):  # Deliberately return results out of input order.
            custom_id = row["custom_id"]
            if row["body"]["model"] == "batch-test-model":
                result = {}
            else:
                payload = json.loads(row["body"]["messages"][-1]["content"])
                result = None
            if result is None and custom_id.startswith("relation-"):
                candidate_id = payload["candidate"]["candidate_id"]
                result = {
                    "candidate_id": candidate_id,
                    "verdict": "recommend_approve",
                    "evidence_sufficiency": "direct",
                    "relation_type_valid": True,
                    "identity_mapping_confidence": "exact_literal",
                    "temporal_scope": "situational",
                    "risk_flags": [],
                    "supporting_quote": "任务结束后晴来到汉诺塔。",
                    "verdict_rationale": "原文逐字说明晴来到汉诺塔。",
                }
            elif result is None:
                candidate_id = payload["candidate"]["entity_candidate_id"]
                result = {
                    "entity_candidate_id": candidate_id,
                    "verdict": "recommend_approve",
                    "node_type_valid": True,
                    "exact_name_in_quote": True,
                    "reusable_named_entity": True,
                    "risk_flags": [],
                    "supporting_quote": "任务结束后晴来到汉诺塔。",
                    "verdict_rationale": "汉诺塔是原文明示的地点。",
                }
            output.append({
                "custom_id": custom_id,
                "response": {
                    "status_code": 200,
                    "body": {
                        "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                    },
                },
            })
        self.files[output_file_id] = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output).encode()
        self.batches[batch_id] = {
            "id": batch_id,
            "status": "completed",
            "output_file_id": output_file_id,
            "request_counts": {"total": len(rows), "completed": len(rows), "failed": 0},
        }
        return {"id": batch_id, "status": "validating"}

    def retrieve(self, batch_id: str) -> dict:
        return self.batches[batch_id]

    def cancel(self, batch_id: str) -> dict:
        self.cancelled.append(batch_id)
        self.batches[batch_id]["status"] = "cancelled"
        return {"id": batch_id, "status": "cancelling"}

    def content(self, file_id: str) -> bytes:
        return self.files[file_id]


class ReviewAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        runtime = self.root / "runtime"
        data = self.root / "data"
        data.mkdir()
        self.settings = Settings(
            data_root=data,
            runtime_root=runtime,
            chat_enabled=False,
            embedding_model="local-test-model",
            allowed_origins=["http://localhost"],
        )
        document = {
            "document_id": "doc-1",
            "page_id": "page-1",
            "title": "地点测试",
            "source_type": "main_story",
            "text": "任务结束后晴来到汉诺塔。",
        }
        relation = {
            "candidate_id": "relation-candidate-1",
            "subject": "晴",
            "relation_type": "VISITS_LOCATION",
            "object": "汉诺塔",
            "source_type": "main_story",
            "evidence_quote": "任务结束后晴来到汉诺塔。",
            "evidence_document_ids": ["doc-1"],
            "review_status": "pending_review",
        }
        entity = {
            "entity_candidate_id": "entity-candidate-1",
            "entity_name": "汉诺塔",
            "normalized_name": "汉诺塔",
            "proposed_node_type": "location",
            "proposed_node_id": "location:review_4dfe67ca40939595",
            "review_status": "pending_review",
            "relation_candidate_ids": ["relation-candidate-1"],
            "source_types": ["main_story"],
            "evidence_document_ids": ["doc-1"],
            "evidence_page_ids": ["page-1"],
            "evidence_examples": [{
                "relation_candidate_id": "relation-candidate-1",
                "subject": "晴",
                "relation_type": "VISITS_LOCATION",
                "object": "汉诺塔",
                "source_type": "main_story",
                "evidence_quote": "任务结束后晴来到汉诺塔。",
            }],
        }
        write_jsonl(runtime / "lakehouse" / "documents.jsonl", [document])
        write_jsonl(runtime / "review" / "narrative_relation_candidates.jsonl", [relation])
        write_jsonl(runtime / "review" / "entity_node_candidates.jsonl", [entity])
        write_jsonl(runtime / "graph" / "nodes.jsonl", [{"node_id": "character:qing", "node_type": "character", "name": "晴"}])
        write_jsonl(runtime / "graph" / "edges.jsonl", [])
        self.repository = RuntimeRepository(self.settings)
        self.fake = FakeBatchClient()
        self.service = ReviewAutomationService(self.settings, self.repository, batch_client=self.fake)
        self.environment = patch.dict(os.environ, {
            "EVIDENCE_REVIEW_PROVIDER": "dashscope-batch",
            "EVIDENCE_REVIEW_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "EVIDENCE_REVIEW_API_KEY": "test-key-never-persisted",
            "EVIDENCE_REVIEW_MODEL": "qwen3.8-max",
            "EVIDENCE_REVIEW_MAX_BUDGET_CNY": "300",
        })
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_estimate_is_read_only_and_does_not_expose_key(self) -> None:
        estimate = self.service.estimate("production")
        self.assertEqual(estimate["counts"]["first_calls"], 2)
        self.assertLess(estimate["projected_batch_cny"], 300)
        self.assertFalse((self.settings.runtime_root / "review" / "automation").exists())
        self.assertNotIn("test-key", json.dumps(estimate))

    def test_estimate_hash_changes_when_review_input_changes(self) -> None:
        estimate = self.service.estimate("production")
        document = json.loads(
            (self.settings.runtime_root / "lakehouse" / "documents.jsonl").read_text(encoding="utf-8")
        )
        document["title"] = "different-title"
        write_jsonl(self.settings.runtime_root / "lakehouse" / "documents.jsonl", [document])
        self.repository.clear_caches()
        refreshed = self.service.estimate("production")
        self.assertNotEqual(refreshed["input_fingerprint"], estimate["input_fingerprint"])
        self.assertNotEqual(refreshed["estimate_hash"], estimate["estimate_hash"])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.service.create_run("production", estimate["estimate_hash"], "review_run_calibration1234")

    def _completed_production_run(self) -> dict:
        self._passing_calibration()
        estimate = self.service.estimate("production")
        run = self.service.create_run("production", estimate["estimate_hash"], "review_run_calibration1234")
        self.assertEqual(run["status"], "submitted")
        run = self.service.sync_run(run["run_id"])
        self.assertEqual(run["active_phase"], "pass-2")
        run = self.service.sync_run(run["run_id"])
        self.assertEqual(run["status"], "ready_to_admit")
        self.assertIn("response_format", self.fake.uploaded[0][0]["body"])
        self.assertFalse(self.fake.uploaded[0][0]["body"]["enable_thinking"])
        self.assertNotIn("response_format", self.fake.uploaded[1][0]["body"])
        self.assertTrue(self.fake.uploaded[1][0]["body"]["enable_thinking"])
        run_text = "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.service._run_path(run["run_id"]).glob("*")
            if path.is_file()
        )
        self.assertNotIn("test-key-never-persisted", run_text)
        return run

    def _passing_calibration(self) -> None:
        calibration_id = "review_run_calibration1234"
        calibration_path = self.service._run_path(calibration_id)
        calibration_path.mkdir(parents=True, exist_ok=True)
        (calibration_path / "manifest.json").write_text(
            json.dumps({
                "run_id": calibration_id,
                "mode": "calibration",
                "status": "awaiting_calibration",
                "model": "qwen3.8-max",
                "policy_version": "qwen-batch-evidence-review-v1",
            }),
            encoding="utf-8",
        )
        write_jsonl(calibration_path / "reports.jsonl", [])
        samples = []
        for category, quota in CALIBRATION_QUOTAS.items():
            for index in range(quota):
                samples.append({
                    "sample_id": f"sample-{category}-{index}",
                    "run_id": calibration_id,
                    "category": category,
                    "kind": "relation" if category.startswith("relation") else "entity",
                    "candidate_id": f"placeholder-{category}-{index}",
                    "prediction": "approve" if "approve" in category else "reject",
                    "correct": True,
                    "critical_error": False,
                    "error_category": "none",
                })
        write_jsonl(self.service.calibration_samples_path, samples)

    def test_two_pass_admission_and_rollback_are_auditable(self) -> None:
        run = self._completed_production_run()
        admitted = self.service.admit_run(run["run_id"])
        self.assertEqual(admitted["status"], "admitted")
        entity = json.loads(self.repository.entity_candidates_path.read_text(encoding="utf-8"))
        relation = json.loads(self.repository.review_candidates_path.read_text(encoding="utf-8"))
        self.assertEqual(entity["review_status"], "approved")
        self.assertEqual(relation["review_status"], "approved")
        node = json.loads(self.repository.approved_entity_nodes_path.read_text(encoding="utf-8"))
        edge = json.loads(self.repository.reviewed_edges_path.read_text(encoding="utf-8"))
        self.assertEqual(node["confidence"], "model_approved_audited")
        self.assertEqual(edge["confidence"], "model_approved_audited")
        self.assertEqual(edge["candidate_ids"], ["relation-candidate-1"])
        self.assertEqual(len(node["model_report_ids"]), 2)
        self.assertEqual(len(edge["model_report_ids"]), 1)

        rolled_back = self.service.rollback_run(run["run_id"])
        self.assertEqual(rolled_back["status"], "rolled_back")
        entity = json.loads(self.repository.entity_candidates_path.read_text(encoding="utf-8"))
        relation = json.loads(self.repository.review_candidates_path.read_text(encoding="utf-8"))
        self.assertEqual(entity["review_status"], "pending_review")
        self.assertEqual(relation["review_status"], "pending_review")
        self.assertEqual(self.repository.approved_entity_nodes_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.repository.reviewed_edges_path.read_text(encoding="utf-8"), "")

    def test_calibration_reports_are_reused_without_a_second_paid_submission(self) -> None:
        estimate = self.service.estimate("calibration")
        calibration = self.service.create_run("calibration", estimate["estimate_hash"])
        calibration = self.service.sync_run(calibration["run_id"])
        self.assertEqual(calibration["active_phase"], "pass-2")
        calibration = self.service.sync_run(calibration["run_id"])
        self.assertEqual(calibration["status"], "awaiting_calibration")
        status = self.service.calibration_status(calibration["run_id"])
        self.assertEqual(status["sample_count"], 2)
        for sample in status["samples"]:
            self.service.label_calibration(sample["sample_id"], {
                "correct": True,
                "critical_error": False,
                "error_category": "none",
                "reviewer_id": "calibration-reviewer",
            })

        production_estimate = self.service.estimate("production")
        self.assertEqual(production_estimate["counts"]["first_calls"], 0)
        self.assertEqual(production_estimate["counts"]["second_calls"], 0)
        self.assertEqual(production_estimate["counts"]["reused_reports"], 3)
        production = self.service.create_run(
            "production", production_estimate["estimate_hash"], calibration["run_id"]
        )
        self.assertEqual(production["status"], "ready_to_admit")
        self.assertEqual(production["reused_report_count"], 3)
        self.assertEqual(len(self.fake.uploaded), 2)
        reused = [
            json.loads(line)
            for line in (self.service._run_path(production["run_id"]) / "reports.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(all(row["automation_run_id"] == production["run_id"] for row in reused))
        self.assertTrue(all(row["reused_from_run_id"] == calibration["run_id"] for row in reused))

    def test_sync_tracks_provider_lifecycle_without_early_import(self) -> None:
        estimate = self.service.estimate("test")
        run = self.service.create_run("test", estimate["estimate_hash"])
        batch_id = run["phases"][0]["provider_batch_id"]
        for provider_status in ("validating", "in_progress", "finalizing"):
            self.fake.batches[batch_id]["status"] = provider_status
            current = self.service.sync_run(run["run_id"])
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["phases"][0]["provider_status"], provider_status)
        self.fake.batches[batch_id]["status"] = "completed"
        completed = self.service.sync_run(run["run_id"])
        self.assertEqual(completed["status"], "completed")

    def test_terminal_provider_failures_stop_the_run(self) -> None:
        for provider_status in ("failed", "expired", "cancelled", "canceled"):
            with self.subTest(provider_status=provider_status):
                estimate = self.service.estimate("test")
                run = self.service.create_run("test", estimate["estimate_hash"])
                batch_id = run["phases"][0]["provider_batch_id"]
                self.fake.batches[batch_id]["status"] = provider_status
                self.fake.batches[batch_id].pop("output_file_id", None)
                terminal = self.service.sync_run(run["run_id"])
                self.assertEqual(terminal["status"], provider_status)
                self.assertIn(provider_status, terminal["last_error"])

    def test_cancelled_batch_with_complete_output_is_recovered(self) -> None:
        estimate = self.service.estimate("calibration")
        run = self.service.create_run("calibration", estimate["estimate_hash"])
        batch_id = run["phases"][0]["provider_batch_id"]
        self.fake.batches[batch_id]["status"] = "cancelled"
        recovered = self.service.sync_run(run["run_id"])
        first_phase = recovered["phases"][0]
        self.assertTrue(first_phase["recovered_terminal_output"])
        self.assertEqual(first_phase["provider_terminal_status"], "cancelled")
        self.assertEqual(recovered["active_phase"], "pass-2")

    def test_only_failed_batch_requests_are_retried(self) -> None:
        estimate = self.service.estimate("calibration")
        run = self.service.create_run("calibration", estimate["estimate_hash"])
        batch = self.fake.batches[run["phases"][0]["provider_batch_id"]]
        output_file_id = batch["output_file_id"]
        rows = [json.loads(line) for line in self.fake.files[output_file_id].decode().splitlines()]
        failed_id = next(row["custom_id"] for row in rows if row["custom_id"].startswith("relation-"))
        next(row for row in rows if row["custom_id"] == failed_id)["response"]["status_code"] = 500
        self.fake.files[output_file_id] = (
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
        )

        retry = self.service.sync_run(run["run_id"])
        self.assertEqual(retry["active_phase"], "pass-1-retry1")
        self.assertEqual([row["custom_id"] for row in self.fake.uploaded[-1]], [failed_id])

    def _chunk_test_manifest(self, run_id: str) -> dict:
        run_path = self.service._run_path(run_id)
        run_path.mkdir(parents=True)
        manifest = {
            "run_id": run_id,
            "mode": "test",
            "status": "creating",
            "active_phase": None,
            "phases": [],
            "estimate": {"budget_cny": 300},
            "actual_usage": {},
            "actual_cost_cny": 0,
        }
        (run_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    @staticmethod
    def _chunk_rows(all_ids: list[str], retry_custom_ids: set[str] | None) -> tuple[list[dict], list[dict]]:
        selected = [item for item in all_ids if retry_custom_ids is None or item in retry_custom_ids]
        rows = [{
            "custom_id": item,
            "method": "POST",
            "url": "/v1/chat/ds-test",
            "body": {"model": "batch-test-model"},
        } for item in selected]
        request_index = [{
            "custom_id": item,
            "kind": "relation",
            "candidate_id": item,
            "pass_index": 1,
            "input_hash": item,
            "evidence_truncated": False,
        } for item in selected]
        return rows, request_index

    def test_large_retry_is_chunked_and_resumes_after_service_restart(self) -> None:
        identifiers = [f"relation-chunk-{index:03d}" for index in range(251)]
        manifest = self._chunk_test_manifest("review_run_chunk_resume")

        def build_rows(_manifest, _pass_index, *, retry_custom_ids=None):
            return self._chunk_rows(identifiers, retry_custom_ids)

        with patch.object(self.service, "_build_phase_rows", side_effect=build_rows):
            submitted = self.service._submit_phase(
                manifest, 1, retry_custom_ids=set(identifiers), retry_count=1
            )
        self.assertEqual(submitted["active_phase"], "pass-1-retry1-part001")
        self.assertEqual(len(self.fake.uploaded[-1]), 250)
        self.assertEqual(len(submitted["retry_chunk_state"]["remaining_custom_id_chunks"]), 1)

        restarted = ReviewAutomationService(self.settings, self.repository, batch_client=self.fake)
        with patch.object(restarted, "_build_phase_rows", side_effect=build_rows):
            second = restarted.sync_run(submitted["run_id"])
        self.assertEqual(second["active_phase"], "pass-1-retry1-part002")
        self.assertEqual(len(self.fake.uploaded[-1]), 1)
        self.assertEqual(second["retry_chunk_state"]["remaining_custom_id_chunks"], [])

        with patch.object(restarted, "_build_phase_rows", side_effect=build_rows):
            completed = restarted.sync_run(submitted["run_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertNotIn("retry_chunk_state", completed)

    def test_chunk_failures_accumulate_before_retry_two(self) -> None:
        identifiers = [f"relation-failure-{index:03d}" for index in range(251)]
        manifest = self._chunk_test_manifest("review_run_chunk_failures")

        def build_rows(_manifest, _pass_index, *, retry_custom_ids=None):
            return self._chunk_rows(identifiers, retry_custom_ids)

        with patch.object(self.service, "_build_phase_rows", side_effect=build_rows):
            first = self.service._submit_phase(
                manifest, 1, retry_custom_ids=set(identifiers), retry_count=1
            )
            first_batch = self.fake.batches[first["phases"][-1]["provider_batch_id"]]
            first_error_id = "file-error-first-chunk"
            self.fake.files[first_error_id] = (json.dumps({"custom_id": identifiers[0]}) + "\n").encode()
            first_batch["error_file_id"] = first_error_id
            second = self.service.sync_run(first["run_id"])

            second_batch = self.fake.batches[second["phases"][-1]["provider_batch_id"]]
            second_error_id = "file-error-second-chunk"
            self.fake.files[second_error_id] = (json.dumps({"custom_id": identifiers[-1]}) + "\n").encode()
            second_batch["error_file_id"] = second_error_id
            retry_two = self.service.sync_run(first["run_id"])

        self.assertEqual(retry_two["active_phase"], "pass-1-retry2")
        self.assertEqual(
            {row["custom_id"] for row in self.fake.uploaded[-1]},
            {identifiers[0], identifiers[-1]},
        )
        self.assertNotIn("retry_chunk_state", retry_two)

    def test_restart_active_phase_requests_provider_cancellation(self) -> None:
        estimate = self.service.estimate("test")
        run = self.service.create_run("test", estimate["estimate_hash"])
        batch_id = run["phases"][0]["provider_batch_id"]
        self.fake.batches[batch_id]["status"] = "in_progress"

        restarted = self.service.restart_active_phase_in_chunks(run["run_id"])

        self.assertEqual(self.fake.cancelled, [batch_id])
        self.assertTrue(restarted["phases"][0]["restart_in_chunks"])
        self.assertEqual(restarted["phases"][0]["provider_status"], "cancelling")

    def test_force_restart_after_cancel_timeout_uses_request_index_chunks(self) -> None:
        identifiers = [f"relation-force-{index:03d}" for index in range(251)]
        manifest = self._chunk_test_manifest("review_run_force_restart")

        def build_rows(_manifest, _pass_index, *, retry_custom_ids=None):
            return self._chunk_rows(identifiers, retry_custom_ids)

        with patch.object(self.service, "_build_phase_rows", side_effect=build_rows):
            active = self.service._submit_phase(manifest, 1)
            phase = active["phases"][-1]
            self.fake.batches[phase["provider_batch_id"]]["status"] = "in_progress"
            self.service.restart_active_phase_in_chunks(active["run_id"])
            self.fake.batches[phase["provider_batch_id"]]["status"] = "cancelling"
            restarted = self.service.force_restart_active_phase_in_chunks(active["run_id"])

        self.assertTrue(restarted["phases"][0]["abandoned_after_cancel_timeout"])
        self.assertEqual(restarted["active_phase"], "pass-1-part001")
        self.assertEqual(len(self.fake.uploaded[-1]), 250)
        self.assertEqual(len(restarted["retry_chunk_state"]["remaining_custom_id_chunks"]), 1)

    def test_repaired_provider_json_is_never_eligible_for_automatic_action(self) -> None:
        estimate = self.service.estimate("calibration")
        run = self.service.create_run("calibration", estimate["estimate_hash"])
        batch = self.fake.batches[run["phases"][0]["provider_batch_id"]]
        output_file_id = batch["output_file_id"]
        rows = [json.loads(line) for line in self.fake.files[output_file_id].decode().splitlines()]
        relation = next(row for row in rows if row["custom_id"].startswith("relation-"))
        content = relation["response"]["body"]["choices"][0]["message"]["content"]
        relation["response"]["body"]["choices"][0]["message"]["content"] = f"```json\n{content}\n```"
        self.fake.files[output_file_id] = (
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
        )

        self.service.sync_run(run["run_id"])
        report = next(
            row
            for row in (
                json.loads(line)
                for line in self.service.relation_reports_path.read_text(encoding="utf-8").splitlines()
            )
            if row.get("candidate_id") == "relation-candidate-1"
        )
        self.assertEqual(report["verdict"], "abstain")
        self.assertFalse(report["audit_eligible"])
        self.assertIn("provider_output_not_exact_json", report["validation_flags"])

    def test_rollback_refuses_a_candidate_changed_after_admission(self) -> None:
        run = self._completed_production_run()
        admitted = self.service.admit_run(run["run_id"])
        relation = json.loads(self.repository.review_candidates_path.read_text(encoding="utf-8"))
        relation["reviewed_at"] = "2099-01-01T00:00:00+00:00"
        write_jsonl(self.repository.review_candidates_path, [relation])
        with self.assertRaisesRegex(ValueError, "changed after automation"):
            self.service.rollback_run(admitted["run_id"])

    def test_budget_ceiling_blocks_submission_before_upload(self) -> None:
        estimate = self.service.estimate("production")
        blocked = {**estimate, "estimate_hash": "blocked-estimate-hash", "projected_batch_cny": 2.0}
        with patch.dict(os.environ, {"EVIDENCE_REVIEW_MAX_BUDGET_CNY": "1"}):
            with patch.object(self.service, "estimate", return_value=blocked):
                with self.assertRaisesRegex(ValueError, "budget ceiling"):
                    self.service.create_run("production", blocked["estimate_hash"], "review_run_calibration1234")
        self.assertEqual(self.fake.uploaded, [])

    def test_any_critical_calibration_error_closes_its_decision_category(self) -> None:
        self._passing_calibration()
        samples = [
            json.loads(line)
            for line in self.service.calibration_samples_path.read_text(encoding="utf-8").splitlines()
        ]
        sample = next(row for row in samples if row["category"] == "relation_reject")
        sample.update({
            "correct": False,
            "critical_error": True,
            "error_category": "identity_confusion",
        })
        write_jsonl(self.service.calibration_samples_path, samples)
        category = self.service.calibration_status("review_run_calibration1234")["categories"]["relation_reject"]
        self.assertGreaterEqual(category["accuracy"], 0.95)
        self.assertEqual(category["critical_errors"], 1)
        self.assertFalse(category["passed"])

    def test_user_calibration_override_is_audited_and_requires_all_correct_labels(self) -> None:
        run = self._completed_production_run()
        calibration_samples = [
            row for row in (
                json.loads(line)
                for line in self.service.calibration_samples_path.read_text(encoding="utf-8").splitlines()
            )
            if row.get("run_id") == "review_run_calibration1234"
        ]
        calibration_samples[0]["correct"] = False
        write_jsonl(self.service.calibration_samples_path, calibration_samples)
        with self.assertRaisesRegex(ValueError, "all labelled samples"):
            self.service.grant_calibration_override(
                run["run_id"], authorized_by="workspace-user", reason="explicit waiver"
            )

        calibration_samples[0]["correct"] = True
        write_jsonl(self.service.calibration_samples_path, calibration_samples)
        overridden = self.service.grant_calibration_override(
            run["run_id"], authorized_by="workspace-user", reason="explicit waiver"
        )
        self.assertTrue(overridden["calibration_override"]["enabled"])
        self.assertEqual(
            overridden["calibration_override"]["policy"],
            "strict_two_pass_consensus_user_override_v1",
        )
        admitted = self.service.admit_run(run["run_id"])
        self.assertEqual(admitted["admission_policy"], "strict_two_pass_consensus_user_override_v1")
        self.assertTrue(all(admitted["calibration_gates"].values()))
        self.assertTrue(admitted["admission_attempt_id"].startswith("admission_"))

    def test_strict_consensus_override_does_not_accept_single_pass_relation(self) -> None:
        reports = {
            ("relation-candidate-1", 1): {
                "verdict": "recommend_approve",
                "audit_eligible": True,
            }
        }
        candidate = {"candidate_id": "relation-candidate-1"}
        self.assertEqual(
            self.service._strict_consensus_relation_prediction(candidate, reports),
            "needs_human_review",
        )
        reports[("relation-candidate-1", 2)] = {
            "verdict": "recommend_approve",
            "audit_eligible": True,
        }
        self.assertEqual(
            self.service._strict_consensus_relation_prediction(candidate, reports),
            "approve",
        )


class FakeAutomationApiService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def estimate(self, mode: str) -> dict:
        self.calls.append(("estimate", mode))
        return {"mode": mode, "estimate_hash": "e" * 64, "projected_batch_cny": 12.34}

    def list_runs(self) -> list[dict]:
        self.calls.append(("list",))
        return [{"run_id": "review_run_mock1234", "status": "submitted"}]

    def create_run(self, mode: str, estimate_hash: str, calibration_run_id: str | None) -> dict:
        self.calls.append(("create", mode, estimate_hash, calibration_run_id))
        return {"run_id": "review_run_mock1234", "status": "submitted"}

    def get_run(self, run_id: str) -> dict:
        self.calls.append(("get", run_id))
        return {"run_id": run_id, "status": "submitted"}

    def sync_run(self, run_id: str) -> dict:
        self.calls.append(("sync", run_id))
        return {"run_id": run_id, "status": "in_progress"}

    def label_calibration(self, sample_id: str, label: dict) -> dict:
        self.calls.append(("label", sample_id, label))
        if label["correct"] and (label["critical_error"] or label["error_category"] != "none"):
            raise ValueError("A correct sample cannot carry an error category or critical-error flag.")
        return {"sample_id": sample_id, **label}

    def admit_run(self, run_id: str) -> dict:
        self.calls.append(("admit", run_id))
        return {"run_id": run_id, "status": "admitted"}

    def rollback_run(self, run_id: str) -> dict:
        self.calls.append(("rollback", run_id))
        return {"run_id": run_id, "status": "rolled_back"}


class ReviewAutomationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeAutomationApiService()
        self.service_patch = patch.object(snow_main, "review_automation", self.service)
        self.service_patch.start()
        self.client = TestClient(snow_main.app)

    def tearDown(self) -> None:
        self.client.close()
        self.service_patch.stop()

    def test_estimate_and_confirmed_run_creation(self) -> None:
        response = self.client.get("/api/v1/review/automation/estimate", params={"mode": "calibration"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["projected_batch_cny"], 12.34)

        missing_confirmation = self.client.post(
            "/api/v1/review/automation/runs",
            json={"mode": "calibration", "estimate_hash": "e" * 64},
        )
        self.assertEqual(missing_confirmation.status_code, 422)
        self.assertFalse(any(call[0] == "create" for call in self.service.calls))

        created = self.client.post(
            "/api/v1/review/automation/runs",
            json={
                "mode": "production",
                "estimate_hash": "e" * 64,
                "calibration_run_id": "review_run_calibration1234",
                "confirmation": "submit_qwen_batch",
            },
        )
        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["status"], "submitted")
        self.assertIn(
            ("create", "production", "e" * 64, "review_run_calibration1234"),
            self.service.calls,
        )

    def test_calibration_label_validation_is_exposed_as_422(self) -> None:
        invalid = self.client.post(
            "/api/v1/review/automation/calibration/sample-1/label",
            json={
                "correct": True,
                "critical_error": True,
                "error_category": "identity_confusion",
                "reviewer_id": "reviewer-1",
            },
        )
        self.assertEqual(invalid.status_code, 422)

        valid = self.client.post(
            "/api/v1/review/automation/calibration/sample-1/label",
            json={
                "correct": False,
                "critical_error": True,
                "error_category": "identity_confusion",
                "reviewer_id": "reviewer-1",
                "note": "identity mismatch",
            },
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()["sample_id"], "sample-1")

    def test_admission_and_rollback_require_distinct_confirmations(self) -> None:
        run_path = "/api/v1/review/automation/runs/review_run_mock1234"
        wrong_admit = self.client.post(
            f"{run_path}/admit", json={"confirmation": "rollback_machine_decisions"}
        )
        self.assertEqual(wrong_admit.status_code, 422)
        self.assertNotIn(("admit", "review_run_mock1234"), self.service.calls)

        admitted = self.client.post(
            f"{run_path}/admit", json={"confirmation": "apply_machine_decisions"}
        )
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(admitted.json()["status"], "admitted")

        wrong_rollback = self.client.post(
            f"{run_path}/rollback", json={"confirmation": "apply_machine_decisions"}
        )
        self.assertEqual(wrong_rollback.status_code, 422)
        self.assertNotIn(("rollback", "review_run_mock1234"), self.service.calls)

        rolled_back = self.client.post(
            f"{run_path}/rollback", json={"confirmation": "rollback_machine_decisions"}
        )
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
