from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "snow-role-assistant"


class CodexPluginTests(unittest.TestCase):
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
            cwd=PLUGIN_ROOT,
            env=process_environment,
            input="".join(json.dumps(item) + "\n" for item in messages),
            text=True,
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
            re.compile(r"^0\.5\.0\+codex\.\d{14}$"),
        )
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
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


if __name__ == "__main__":
    unittest.main()
