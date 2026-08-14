from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "snow-role-assistant"


class CodexPluginTests(unittest.TestCase):
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

        self.assertEqual(manifest["name"], "snow-role-assistant")
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(marketplace["plugins"][0]["name"], manifest["name"])
        self.assertEqual(
            marketplace["plugins"][0]["source"]["path"],
            "./plugins/snow-role-assistant",
        )
        self.assertIn("@Snow", skill)
        self.assertIn("Never switch characters inside the same task", skill)
        self.assertIn("never alter", skill.casefold())
        self.assertIn("Never write", skill)

    def test_mcp_server_lists_read_only_tools_over_stdio(self) -> None:
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
        completed = subprocess.run(
            [sys.executable, "mcp/snow_persona_server/server.py"],
            cwd=PLUGIN_ROOT,
            input="".join(json.dumps(item) + "\n" for item in messages),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "snow-persona")
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


if __name__ == "__main__":
    unittest.main()
