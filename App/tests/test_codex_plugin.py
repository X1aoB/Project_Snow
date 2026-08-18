from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "snow-role-assistant"


class CodexPluginTests(unittest.TestCase):
    def _gateway_fixture(self):
        class GatewayHandler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlsplit(self.path)
                self.server.seen_paths.append(parsed.path)
                if self.server.revoked or self.headers.get("Authorization") != "Bearer fixture-token":
                    self._json(401, {"detail": "invalid pairing"})
                    return
                if parsed.path == "/api/v1/persona/pairing":
                    self._json(
                        200,
                        {
                            "pairing_id": "fixture-pairing",
                            "default_character_id": "ca0144ccd81b",
                            "status": "active",
                        },
                    )
                    return
                if parsed.path.startswith("/api/v1/persona/snapshot/"):
                    self._json(
                        200,
                        {
                            "profile_version": "fixture-v1",
                            "character": {
                                "character_id": "ca0144ccd81b",
                                "display_name": "里芙",
                            },
                            "relationship": {
                                "preferred_address": "分析员",
                                "write_back_allowed": False,
                            },
                            "persona": {
                                "sentence_style": [
                                    {
                                        "rule": f"style-{index}",
                                        "description": "克制而精确" * 400,
                                        "evidence": [
                                            {
                                                "document_id": f"doc-{index}-{evidence}",
                                                "quote": "公开角色证据" * 200,
                                            }
                                            for evidence in range(12)
                                        ],
                                    }
                                    for index in range(20)
                                ],
                                "analyst_interaction": [
                                    {"document_id": f"interaction-{index}", "quote": "分析员" * 500}
                                    for index in range(20)
                                ],
                            },
                        },
                    )
                    return
                if parsed.path.startswith("/api/v1/relationships/"):
                    self._json(
                        200,
                        {
                            "profile_version": "fixture-v1",
                            "character": {
                                "character_id": "ca0144ccd81b",
                                "display_name": "里芙",
                            },
                            "relationship": {
                                "preferred_address": "分析员",
                                "write_back_allowed": False,
                            },
                        },
                    )
                    return
                if parsed.path == "/api/v1/knowledge/search":
                    self._json(
                        200,
                        {
                            "character_id": "ca0144ccd81b",
                            "profile_version": "fixture-v1",
                            "results": [
                                {
                                    "text": "公开身份资料",
                                    "citation": {"source_id": "fixture-source"},
                                }
                            ],
                            "write_back_allowed": False,
                        },
                    )
                    return
                self._json(404, {"detail": "not found"})

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        server.seen_paths = []
        server.revoked = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _mcp_server(self) -> dict:
        config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        return config["mcpServers"]["snow-persona"]

    def _run_mcp(
        self,
        messages: list[dict],
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[list[dict], subprocess.CompletedProcess[str]]:
        server = self._mcp_server()
        process_environment = os.environ.copy()
        process_environment.update(environment or {})
        completed = subprocess.run(
            [server["command"], *server["args"]],
            cwd=(PLUGIN_ROOT / server["cwd"]).resolve(),
            env=process_environment,
            input="".join(json.dumps(item) + "\n" for item in messages),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        return responses, completed

    def test_manifest_marketplace_and_skill_are_consistent(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        skill = (
            PLUGIN_ROOT / "skills" / "snow-role-assistant" / "SKILL.md"
        ).read_text(encoding="utf-8")
        server = self._mcp_server()

        self.assertEqual(manifest["name"], "snow-role-assistant")
        self.assertRegex(
            manifest["version"],
            re.compile(r"^0\.5\.1(?:\+codex\.\d{14})?$"),
        )
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["command"], "uv")
        self.assertEqual(
            server["args"],
            [
                "run",
                "--with",
                "keyring>=25.5,<26",
                "--",
                "python",
                "mcp/snow_persona_server/server.py",
            ],
        )
        self.assertEqual(marketplace["plugins"][0]["name"], manifest["name"])
        self.assertEqual(
            marketplace["plugins"][0]["source"]["path"],
            "./plugins/snow-role-assistant",
        )
        self.assertIn("@Snow", skill)
        self.assertIn("Never switch characters inside the same task", skill)
        self.assertIn("never alter", skill.casefold())
        self.assertIn("Never write", skill)

    def test_mcp_server_lists_read_only_tools_over_configured_stdio(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        responses, completed = self._run_mcp(messages)

        initialization = responses[0]["result"]
        self.assertEqual(initialization["serverInfo"]["name"], "snow-persona")
        self.assertEqual(initialization["serverInfo"]["version"], manifest["version"])
        instruction_prefix = initialization["instructions"][:512]
        self.assertIn("snow_get_configuration once", instruction_prefix)
        self.assertIn("keep that character and profile_version fixed", instruction_prefix)
        self.assertIn("Never write data back", instruction_prefix)
        self.assertIn("Preserve numbers", instruction_prefix)
        tools = responses[1]["result"]["tools"]
        self.assertEqual(
            {item["name"] for item in tools},
            {
                "snow_get_configuration",
                "snow_get_persona_snapshot",
                "snow_search_knowledge",
                "snow_get_relationship",
            },
        )
        self.assertTrue(all(item["annotations"]["readOnlyHint"] for item in tools))
        self.assertTrue(all(not item["annotations"]["destructiveHint"] for item in tools))
        self.assertNotIn("jsonrpc", completed.stderr)

    def test_mcp_rejects_remote_gateway_without_leaking_token(self) -> None:
        secret = "snow_pair_this_must_not_appear"
        responses, completed = self._run_mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "snow_get_configuration", "arguments": {}},
                }
            ],
            environment={
                "SNOW_PERSONA_BASE_URL": "https://example.com:443",
                "SNOW_PERSONA_TOKEN": secret,
            },
        )

        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "invalid_configuration",
        )
        self.assertNotIn(secret, completed.stdout)
        self.assertNotIn(secret, completed.stderr)

    def test_mcp_rejects_invalid_tool_arguments_before_network_access(self) -> None:
        responses, _ = self._run_mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "snow_search_knowledge",
                        "arguments": {"character_id": "里芙", "query": "test", "limit": 9},
                    },
                }
            ],
            environment={
                "SNOW_PERSONA_BASE_URL": "http://127.0.0.1:1",
                "SNOW_PERSONA_TOKEN": "test-only-token",
            },
        )

        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_arguments")

    def test_live_validator_calls_all_tools_and_detects_revocation(self) -> None:
        server, thread = self._gateway_fixture()
        environment = os.environ.copy()
        environment.update(
            {
                "SNOW_PERSONA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "SNOW_PERSONA_TOKEN": "fixture-token",
                "SNOW_PERSONA_DEBUG": "1",
            }
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/validate_codex_plugin.py",
                    "--mode",
                    "live",
                    "--character-id",
                    "里芙",
                    "--query",
                    "公开身份",
                ],
                cwd=REPOSITORY_ROOT / "App",
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["mode"], "live")
            self.assertEqual(summary["character_id"], "ca0144ccd81b")
            self.assertEqual(summary["profile_version"], "fixture-v1")
            self.assertEqual(summary["knowledge_results"], 1)
            self.assertEqual(summary["private_key_count"], 0)
            self.assertLessEqual(summary["snapshot_bytes"], 32_000)
            self.assertLessEqual(summary["snapshot_text_bytes"], 2_048)
            self.assertEqual(
                server.seen_paths,
                [
                    "/api/v1/persona/pairing",
                    "/api/v1/persona/snapshot/%E9%87%8C%E8%8A%99",
                    "/api/v1/relationships/%E9%87%8C%E8%8A%99",
                    "/api/v1/knowledge/search",
                ],
            )
            self.assertNotIn("fixture-token", completed.stdout)
            self.assertNotIn("fixture-token", completed.stderr)

            server.revoked = True
            responses, revoked_process = self._run_mcp(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "snow_get_configuration", "arguments": {}},
                    }
                ],
                environment={
                    "SNOW_PERSONA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "SNOW_PERSONA_TOKEN": "fixture-token",
                },
            )
            result = responses[0]["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(
                result["structuredContent"]["error"]["code"],
                "pairing_invalid",
            )
            self.assertNotIn("fixture-token", revoked_process.stdout)
            self.assertNotIn("fixture-token", revoked_process.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
