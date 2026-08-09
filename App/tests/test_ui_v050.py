from __future__ import annotations

import json
from pathlib import Path
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]


class V050SurfaceTests(unittest.TestCase):
    def test_landing_and_chat_surfaces_are_declared(self) -> None:
        html = (APP_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        router = (APP_ROOT / "frontend" / "ui-router.js").read_text(encoding="utf-8")
        application = (APP_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("v0.5.0 · 本地测试版", html)
        self.assertIn('href="/immersive/"', html)
        self.assertIn('href="/assistant/"', html)
        self.assertIn('type="module" src="/app.js"', html)
        self.assertIn('rel="stylesheet" href="/styles.css"', html)
        self.assertIn('path === "/immersive"', router)
        self.assertIn('path === "/assistant"', router)
        self.assertNotIn("她们都在这里", html)
        self.assertNotIn("landing-character-marks", html)
        self.assertNotIn("character?.conversation ||", application)
        self.assertNotIn("character.conversation ||", application)

    def test_assistant_analysis_and_provider_picker_are_progressive(self) -> None:
        html = (APP_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        application = (APP_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for provider in ("openai", "deepseek", "dashscope", "zhipu", "moonshot", "openai-compatible"):
            self.assertIn(f'data-provider-choice="{provider}"', html)
        self.assertIn('id="windows-env-code"', html)
        self.assertIn('id="copy-windows-env"', html)
        self.assertIn('id="provider-model-select"', html)
        self.assertIn('id="default-immersive-model"', html)
        self.assertIn('id="default-assistant-model"', html)
        self.assertIn('id="thinking-mode"', html)
        self.assertNotIn('id="provider-quality"', html)
        self.assertNotIn('id="cap-vision"', html)
        self.assertIn("analysis_process", application)
        self.assertIn('class="work-trace analysis-trace"', application)
        self.assertNotIn("角色化处理摘要", application)
        self.assertIn('$env:MVP_CHAT_BASE_URL', application)
        self.assertIn('discover-models', application)
        self.assertNotIn('filter((item) => item.probe_status === "verified")', application)

    def test_text_portraits_use_only_the_first_character(self) -> None:
        ui_core = (APP_ROOT / "frontend" / "ui-core.js").read_text(encoding="utf-8")
        self.assertIn('name.slice(0, 1) || "?"', ui_core)
        self.assertNotIn("name.slice(-2)", ui_core)

    def test_workspace_has_all_six_hash_views_and_detail_drawer(self) -> None:
        html = (APP_ROOT / "frontend" / "workspace" / "index.html").read_text(
            encoding="utf-8"
        )
        for view in (
            "overview",
            "evidence",
            "relations",
            "entities",
            "feedback",
            "dialogue-debug",
        ):
            self.assertIn(f'data-workspace-target="{view}"', html)
            self.assertIn(f'data-workspace-view="{view}"', html)
        self.assertIn('id="workspace-detail-drawer"', html)

    def test_blue_white_design_uses_no_gradients(self) -> None:
        styles = "\n".join(
            (APP_ROOT / "frontend" / path).read_text(encoding="utf-8")
            for path in ("styles.css", "workspace/styles.css")
        )
        self.assertNotIn("gradient(", styles)
        for color in ("#ffffff", "#eaf4ff", "#0a5cff", "#062b73", "#24c7ff", "#08234a"):
            self.assertIn(color, styles.lower())

    def test_electron_is_v050_and_starts_at_selector(self) -> None:
        package = json.loads((APP_ROOT / "client" / "package.json").read_text(encoding="utf-8"))
        main = (APP_ROOT / "client" / "main.js").read_text(encoding="utf-8")
        self.assertEqual(package["version"], "0.5.0")
        self.assertIn('const WEB_URL = "http://127.0.0.1:8080/"', main)
        self.assertIn("v0.5.0 · 本地测试版", main)
        self.assertIn("contextIsolation: true", main)
        self.assertIn("nodeIntegration: false", main)
        self.assertIn("sandbox: true", main)


if __name__ == "__main__":
    unittest.main()
