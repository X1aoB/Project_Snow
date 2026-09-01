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
        self.assertNotIn('href="/assistant/"', html)
        self.assertIn('type="module" src="/app.js"', html)
        self.assertIn('rel="stylesheet" href="/styles.css"', html)
        self.assertIn('path === "/immersive"', router)
        self.assertNotIn('path === "/assistant"', router)
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
        self.assertIn('const modelName = byId("provider-model").value.trim()', application)
        self.assertIn('byId("provider-model-field").hidden = false;', application)
        self.assertIn(
            'const model = byId("provider-model").value.trim() || byId("provider-model-select").value;',
            application,
        )

    def test_external_persona_plugin_is_not_a_project_snow_surface(self) -> None:
        html = (APP_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        application = (APP_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="plugin-center"', html)
        self.assertNotIn('id="plugin-character"', html)
        self.assertNotIn('id="pair-codex"', html)
        self.assertNotIn("codex plugin add", html)
        self.assertIn('byId("chat-app").hidden = state.surface !== "immersive"', application)
        self.assertNotIn("/api/v1/persona/", application)
        self.assertNotIn("project_snow:plugin_", application)

    def test_immersive_surface_has_distinct_text_and_in_person_renderers(self) -> None:
        html = (APP_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        application = (APP_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        styles = (APP_ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
        for element_id in (
            "text-surface",
            "in-person-surface",
            "go-in-person",
            "open-communicator",
            "open-transcript",
            "transcript-panel",
            "presence-dialog",
            "toggle-action",
            "toggle-stage-ui",
            "stage-portrait",
            "scene-backdrop",
            "presence-arrival-loading",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertNotIn('id="channel-control"', html)
        self.assertIn('/api/v1/mvp/presence/resolve', application)
        self.assertIn('/api/v1/mvp/presence/transition', application)
        self.assertIn('/api/v1/mvp/presence/arrival', application)
        self.assertIn('Math.max(0, 1000 - elapsed)', application)
        self.assertIn('window.requestAnimationFrame(() => resolve())', application)
        arrival_flow = application[application.index("async function arriveInPerson"):application.index("async function resolveScene")]
        self.assertLess(arrival_flow.index("state.arrivalPending = null"), arrival_flow.index("arrivalMessage(result, thread)"))
        self.assertIn('function renderStage()', application)
        self.assertIn('function renderTranscript()', application)
        self.assertIn('.in-person-surface', styles)
        self.assertIn('.channel-memory-card', styles)
        self.assertIn('@keyframes arrival-rise-bounce', styles)
        self.assertIn('.presence-arrival-loading-card', styles)
        self.assertNotIn('id="stage-character-visual"', html)
        self.assertNotIn("avatar?.stage_src", application)
        self.assertIn("beginStageReveal", application)
        self.assertIn("setStageUiHidden", application)
        self.assertIn("portrait_scale", application)
        self.assertIn("portrait_focus_y", application)

    def test_stage_portrait_contract_is_progressive_and_publishable(self) -> None:
        service = (APP_ROOT / "backend" / "snow_app" / "mvp_service.py").read_text(
            encoding="utf-8"
        )
        builder = (APP_ROOT / "scripts" / "build_character_avatars.py").read_text(
            encoding="utf-8"
        )
        manifest_path = APP_ROOT / "frontend" / "assets" / "characters" / "avatars.json"
        self.assertIn('"stage_src": avatar.get("stage_src")', service)
        self.assertIn('"stage_src_deprecated": True', service)
        self.assertIn('"portrait_kind": portrait_kind', service)
        self.assertIn('"portrait_scale": avatar.get(', service)
        self.assertIn('"publishable": avatar.get(', service)
        self.assertIn('"schema_version": "project-snow-avatar-1.2"', builder)
        self.assertIn('"stage_focus_x": 50', builder)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "project-snow-avatar-1.2")
            self.assertTrue(all(item["portrait_kind"] in {"headshot", "full_body"} for item in manifest["characters"]))
            self.assertTrue(any(item["portrait_kind"] == "full_body" for item in manifest["characters"]))
        else:
            # Clean GPL checkouts intentionally omit Wiki-derived avatar media.
            # The text-avatar path is the required fail-safe in that state.
            ui_core = (APP_ROOT / "frontend" / "ui-core.js").read_text(encoding="utf-8")
            self.assertIn('name.slice(0, 1) || "?"', ui_core)

    def test_immersive_visual_novel_assets_cover_all_scene_keys(self) -> None:
        scene_root = APP_ROOT / "frontend" / "assets" / "immersive" / "scenes"
        for key in (
            "generic",
            "quarters",
            "lounge",
            "training",
            "archive",
            "canteen",
            "observation",
            "medical",
            "corridor",
        ):
            asset = scene_root / f"{key}.svg"
            self.assertTrue(asset.exists(), key)
            source = asset.read_text(encoding="utf-8").casefold()
            self.assertIn("<defs>", source)
            self.assertIn("stroke=", source)

    def test_workspace_marks_the_old_agent_as_legacy(self) -> None:
        html = (APP_ROOT / "frontend" / "workspace" / "index.html").read_text(
            encoding="utf-8"
        )
        application = (APP_ROOT / "frontend" / "workspace" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("Legacy Agent", html)
        self.assertIn("不再作为正式助手入口", html)
        self.assertIn("本问题族共 ${issueReportCount} 条", application)
        self.assertIn("已验证后再次反馈", application)
        self.assertNotIn("regression_candidate", application)

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
        for color in ("#ffffff", "#eaf4ff", "#0a5cff", "#062b73", "#24c7ff", "#08234a", "#fbfdff", "#f2f7fb", "#e4edf4", "#b7cad8", "#20384a"):
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
