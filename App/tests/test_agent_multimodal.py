from __future__ import annotations

import io
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest

from backend.snow_app.agent_runtime import AgentRuntime, AgentSecurityError
from backend.snow_app.agent_store import AgentStore
from backend.snow_app.attachment_manager import AttachmentError, AttachmentManager
from backend.snow_app.provider_registry import ProviderRegistry


class _Vault:
    def get(self, reference: str) -> str:
        return "secret" if reference else ""


class _NoProvider:
    def route(self, *args, **kwargs):
        raise ValueError("not configured")

    def credential_for_selection(self, selection):
        return ""


class _ApprovalRuntime(AgentRuntime):
    def _plan(self, task, override):
        return ([{"tool": "powershell", "arguments": {"command": "Remove-Item important.txt"}}], {"reason": "test"})


class MultimodalAgentTests(unittest.TestCase):
    def test_attachment_is_deduplicated_and_never_uses_client_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            manager = AttachmentManager(root, store)
            first = manager.save_bytes("../note.txt", "hello 世界".encode(), "text/plain")
            second = manager.save_bytes("note.txt", "hello 世界".encode(), "text/plain")
            self.assertEqual(first["attachment_id"], second["attachment_id"])
            self.assertEqual(manager.get(first["attachment_id"], include_text=True)["extracted_text"], "hello 世界")
            internal = store.get_attachment(first["attachment_id"])
            self.assertTrue(Path(internal["storage_path"]).resolve().is_relative_to(manager.root))

    def test_attachment_rejects_executable_and_oversized_image(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = AttachmentManager(Path(temp), AgentStore(Path(temp) / "agent.sqlite3"))
            with self.assertRaises(AttachmentError):
                manager.save_bytes("malware.exe", b"MZ", "application/octet-stream")
            with self.assertRaises(AttachmentError):
                manager.validate("large.png", 21 * 1024 * 1024, "image/png")

    def test_quality_routing_requires_verified_capability_and_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agent.sqlite3")
            registry = ProviderRegistry(store)
            registry.vault = _Vault()
            for provider_id, score in (("low", 20), ("high", 90)):
                store.upsert_provider({
                    "provider_id": provider_id, "display_name": provider_id, "base_url": "https://example.test/v1",
                    "credential_ref": provider_id, "enabled": True, "trusted_data_types": ["text", "image"],
                })
                store.upsert_model({
                    "provider_id": provider_id, "model_name": "model", "probe_status": "verified",
                    "quality_score": score, "capabilities": {"text": True, "vision": True},
                })
            selected = registry.route({"text", "vision"}, required_data_types={"text", "image"})
            self.assertEqual(selected.provider_id, "high")
            self.assertEqual(selected.quality_score, 90)

    def test_agent_read_task_finishes_and_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "example.txt").write_text("ok", encoding="utf-8")
            store = AgentStore(root / "agent.sqlite3")
            runtime = AgentRuntime(store, _NoProvider(), root)
            run = runtime.create({"character_id": "character_lyfe", "task": "列出文件", "mode": "assistant"})
            for _ in range(100):
                snapshot = runtime.snapshot(run["run_id"])
                if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.02)
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["state"]["steps"][0]["risk_level"], "read")

    def test_dangerous_tool_waits_for_approval_and_rejection_cancels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            runtime = _ApprovalRuntime(store, _NoProvider(), root)
            run = runtime.create({"character_id": "character_lyfe", "task": "delete", "mode": "assistant"})
            for _ in range(100):
                snapshot = runtime.snapshot(run["run_id"])
                if snapshot["status"] == "awaiting_approval":
                    break
                time.sleep(0.02)
            self.assertEqual(snapshot["status"], "awaiting_approval")
            approval = snapshot["state"]["approvals"][0]
            rejected = runtime.approve(run["run_id"], approval["approval_id"], "rejected", "test")
            self.assertEqual(rejected["status"], "cancelled")

    def test_destructive_approval_requires_two_confirmations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            runtime = _ApprovalRuntime(store, _NoProvider(), root)
            run = runtime.create({"character_id": "character_lyfe", "task": "delete", "mode": "assistant"})
            for _ in range(100):
                snapshot = runtime.snapshot(run["run_id"])
                if snapshot["status"] == "awaiting_approval":
                    break
                time.sleep(0.02)
            first = snapshot["state"]["approvals"][0]
            after_first = runtime.approve(run["run_id"], first["approval_id"], "approved")
            self.assertEqual(after_first["status"], "awaiting_approval")
            pending = [item for item in after_first["state"]["approvals"] if item["status"] == "pending"]
            self.assertEqual(len(pending), 1)
            self.assertTrue(pending[0]["summary"].startswith("二次确认："))

    def test_authorized_root_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            runtime = AgentRuntime(store, _NoProvider(), root)
            run = store.create_run({"character_id": "x", "task": "x", "state": {"authorized_roots": [str(root)]}})
            with self.assertRaises(AgentSecurityError):
                runtime._resolve_path(run["run_id"], str(root.parent / "outside.txt"))

    def test_secret_looking_task_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            runtime = AgentRuntime(store, _NoProvider(), root)
            with self.assertRaises(AgentSecurityError):
                runtime.create({"character_id": "x", "task": "api_key=TEST_KEY_NOT_A_SECRET", "mode": "assistant"})
            self.assertEqual(store.list_runs(), [])

    def test_sensitive_file_and_shell_chaining_are_not_automatic_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
            store = AgentStore(root / "agent.sqlite3")
            runtime = AgentRuntime(store, _NoProvider(), root)
            run = store.create_run({"character_id": "x", "task": "x", "state": {"authorized_roots": [str(root)]}})
            step = store.append_step(run["run_id"], {"step_index": 0, "tool_name": "read_file"})
            with self.assertRaises(AgentSecurityError):
                runtime._execute_step(run["run_id"], step, {"tool": "read_file", "arguments": {"path": ".env"}}, Event())
            chained = {"tool": "powershell", "arguments": {"command": "Get-Content README.md; Invoke-WebRequest https://example.com"}}
            self.assertEqual(runtime.risk(chained), "system_change")
            with self.assertRaises(AgentSecurityError):
                runtime._execute_step(run["run_id"], step, chained, Event())

    def test_symlink_escape_is_blocked_when_platform_allows_links(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            link = root / "outside-link"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Creating symbolic links is not available for this Windows account")
            runtime = AgentRuntime(AgentStore(root / "agent.sqlite3"), _NoProvider(), root)
            run = runtime.store.create_run({"character_id": "x", "task": "x", "state": {"authorized_roots": [str(root)]}})
            with self.assertRaises(AgentSecurityError):
                runtime._resolve_path(run["run_id"], str(link / "secret.txt"))

    def test_agent_client_run_id_is_idempotent_and_failed_run_can_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentStore(root / "agent.sqlite3")
            runtime = AgentRuntime(store, _NoProvider(), root)
            payload = {"character_id": "x", "task": "列出文件", "mode": "assistant", "client_run_id": "client-run-123"}
            first = runtime.create(payload)
            second = runtime.create(payload)
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second["idempotent_replay"])
            for _ in range(100):
                if runtime.snapshot(first["run_id"])["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            store.update_run(first["run_id"], status="failed")
            retried = runtime.retry(first["run_id"])
            self.assertNotEqual(retried["run_id"], first["run_id"])
            for _ in range(100):
                if runtime.snapshot(retried["run_id"])["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
