from __future__ import annotations

import io
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from backend.snow_app.agent_runtime import AgentRuntime, AgentSecurityError
from backend.snow_app.agent_store import AgentStore
from backend.snow_app.attachment_manager import AttachmentError, AttachmentManager
from backend.snow_app.provider_registry import ModelSelection, ProviderRegistry


class _Vault:
    def get(self, reference: str) -> str:
        return "secret" if reference else ""


class _NoProvider:
    def route(self, *args, **kwargs):
        raise ValueError("not configured")

    def credential_for_selection(self, selection):
        return ""


class _ApprovalRuntime(AgentRuntime):
    def _plan(self, task, override, thinking_mode="auto"):
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

    def test_discovered_model_is_selectable_before_probe_but_not_auto_routed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agent.sqlite3")
            registry = ProviderRegistry(store)
            registry.vault = _Vault()
            store.upsert_provider({
                "provider_id": "deepseek", "display_name": "DeepSeek", "kind": "deepseek",
                "base_url": "https://api.deepseek.com/v1", "credential_ref": "deepseek",
                "enabled": True, "trusted_data_types": ["text"],
            })
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]}
            with patch("backend.snow_app.provider_registry.httpx.get", return_value=response):
                discovered = registry.discover_models("deepseek")
            self.assertEqual(discovered["status"], "succeeded")
            flash = next(item for item in discovered["models"] if item["model_name"] == "deepseek-v4-flash")
            self.assertTrue(flash["selectable"])
            self.assertEqual(flash["text_status"], "unverified")
            selected = registry.route(
                {"text"}, {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"}
            )
            self.assertEqual(selected.model_name, "deepseek-v4-flash")
            environment_defaults = {
                "MVP_CHAT_BASE_URL": "",
                "DASHSCOPE_BASE_URL": "",
                "OPENAI_COMPATIBLE_BASE_URL": "",
                "MVP_CHAT_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "OPENAI_COMPATIBLE_API_KEY": "",
                "MVP_CHAT_CREDENTIAL_REF": "",
                "MVP_CHAT_MODEL": "",
                "OPENAI_COMPATIBLE_MODEL": "",
            }
            with patch.dict("os.environ", environment_defaults, clear=False):
                with self.assertRaises(ValueError):
                    registry.route({"text"})

    def test_deepseek_probe_disables_thinking_and_keeps_text_when_json_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agent.sqlite3")
            registry = ProviderRegistry(store)
            registry.vault = _Vault()
            store.upsert_provider({
                "provider_id": "deepseek", "display_name": "DeepSeek", "kind": "deepseek",
                "base_url": "https://api.deepseek.com/v1", "credential_ref": "deepseek",
                "enabled": True, "trusted_data_types": ["text"],
            })
            basic = Mock(status_code=200)
            basic.raise_for_status.return_value = None
            basic.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
            structured = Mock(status_code=200)
            structured.raise_for_status.return_value = None
            structured.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
            with patch("backend.snow_app.provider_registry.httpx.post", side_effect=[basic, structured]) as request:
                result = registry.probe("deepseek", "deepseek-v4-flash", {"structured_output": True})
            self.assertEqual(request.call_args_list[0].kwargs["json"]["thinking"], {"type": "disabled"})
            self.assertEqual(request.call_args_list[1].kwargs["json"]["max_tokens"], 256)
            self.assertEqual(result["text_status"], "ready")
            self.assertFalse(result["capabilities"]["structured_output"])
            self.assertEqual(result["capability_status"]["structured_output"], "failed")
            self.assertEqual(registry.route({"text"}).model_name, "deepseek-v4-flash")

    def test_changing_provider_base_url_invalidates_old_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agent.sqlite3")
            registry = ProviderRegistry(store)
            registry.vault = _Vault()
            store.upsert_provider({
                "provider_id": "openai", "display_name": "OpenAI", "kind": "openai",
                "base_url": "https://relay.example/v1", "credential_ref": "openai",
                "enabled": True, "trusted_data_types": ["text"],
            })
            store.upsert_model({
                "provider_id": "openai", "model_name": "relay-model", "probe_status": "verified",
                "capabilities": {"text": True}, "probe": {"text": "passed"},
            })
            registry.save_provider({
                "provider_id": "openai", "display_name": "OpenAI", "kind": "openai",
                "base_url": "https://api.openai.com/v1", "enabled": True,
                "trusted_data_types": ["text"], "config": {},
            })
            model = registry.models()[0]
            self.assertEqual(model["text_status"], "unverified")
            self.assertFalse(model["automatic_routing_eligible"])
            self.assertEqual(model["probe"]["stale_reason"], "provider_base_url_changed")

    def test_thinking_policy_forces_immersive_off_and_enables_complex_assistant(self):
        selection = ModelSelection(
            "deepseek", "DeepSeek", "deepseek-v4-flash", "https://api.deepseek.com/v1",
            "deepseek", {"text": True}, "test", provider_kind="deepseek",
        )
        immersive = ProviderRegistry.resolve_thinking(selection, "immersive", "on", complex_task=True)
        assistant = ProviderRegistry.resolve_thinking(selection, "assistant", "auto", complex_task=True)
        self.assertEqual(immersive["effective"], "off")
        self.assertEqual(immersive["request_fields"]["thinking"], {"type": "disabled"})
        self.assertEqual(assistant["effective"], "on")
        self.assertEqual(assistant["request_fields"]["thinking"], {"type": "enabled"})
        openai = ModelSelection(
            "openai", "OpenAI", "text-model", "https://api.openai.com/v1",
            "openai", {"text": True}, "test", provider_kind="openai",
        )
        unverified = ProviderRegistry.resolve_thinking(openai, "assistant", "on", complex_task=True)
        self.assertEqual(unverified["effective"], "off")
        self.assertEqual(unverified["reason"], "provider_thinking_unverified")

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
