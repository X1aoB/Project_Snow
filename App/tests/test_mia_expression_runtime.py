from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from unittest import TestCase

from PIL import Image

APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = APP_ROOT / "public_frontend"
ASSET_ROOT = PUBLIC_ROOT / "assets" / "expressions" / "mia"
MANIFEST_PATH = ASSET_ROOT / "manifest.json"
EXPECTED_STATES = {
    "neutral",
    "gentle_smile",
    "happy",
    "amused",
    "teasing",
    "relieved",
    "serious",
    "focused",
    "thinking",
    "confused",
    "skeptical",
    "concerned",
    "surprised",
    "embarrassed",
    "sad",
    "disappointed",
    "annoyed",
    "angry",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MiaExpressionRuntimeTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.javascript = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.privacy_html = (PUBLIC_ROOT / "privacy" / "index.html").read_text(encoding="utf-8")

    def test_manifest_records_approval_and_explicit_unverified_waiver(self) -> None:
        self.assertEqual(
            self.manifest["schema_version"],
            "project-snow-mia-expression-runtime-2",
        )
        self.assertEqual(self.manifest["character_id"], "702f4375675b")
        self.assertEqual(self.manifest["expression_state_count"], 18)
        self.assertEqual(set(self.manifest["expressions"]), EXPECTED_STATES)
        self.assertEqual(
            self.manifest["publication_status"],
            "public_runtime_enabled_by_explicit_operator_rights_waiver",
        )
        rights = self.manifest["rights"]
        self.assertFalse(rights["independent_verification"])
        self.assertEqual(rights["verification_status"], "not_performed")
        self.assertFalse(rights["ownership_claimed_by_project"])
        self.assertTrue(rights["waiver"]["granted"])
        self.assertEqual(rights["waiver"]["recorded_date"], "2026-09-02")
        self.assertEqual(rights["takedown_contact"], "admin@xiaob.dev")

    def test_all_runtime_assets_are_lossless_content_hashed_derivatives(self) -> None:
        transforms = self.manifest["transforms"]
        self.assertEqual(transforms["face"]["crop_box_xyxy"], [440, 145, 635, 340])
        self.assertEqual(transforms["face"]["output_dimensions"], {"width": 384, "height": 384})
        self.assertEqual(transforms["stage"]["resize_mode"], "contain_no_padding")
        self.assertEqual(transforms["stage"]["output_dimensions"], {"width": 620, "height": 1024})
        self.assertEqual(transforms["format"], "lossless_webp")
        for state, record in self.manifest["expressions"].items():
            self.assertRegex(record["source_sha256"], r"^[0-9a-f]{64}$", state)
            self.assertGreaterEqual(record["approved_round"], 1, state)
            for asset_kind, expected_size in (("face", (384, 384)), ("stage", (620, 1024))):
                relative_asset = record[f"{asset_kind}_asset_path"].removeprefix("/")
                asset_path = PUBLIC_ROOT / relative_asset
                self.assertTrue(asset_path.is_file(), f"{state}/{asset_kind}")
                digest = sha256(asset_path)
                self.assertEqual(record[f"{asset_kind}_asset_sha256"], digest, state)
                expected_stem = f"{state}.stage" if asset_kind == "stage" else state
                self.assertRegex(
                    asset_path.name,
                    rf"^{re.escape(expected_stem)}\.{digest[:16]}\.webp$",
                    state,
                )
                with Image.open(asset_path) as image:
                    self.assertEqual(image.format, "WEBP", state)
                    self.assertEqual(image.size, expected_size, state)
                    self.assertEqual(image.mode, "RGBA", state)

    def test_bundled_fallback_pins_both_repository_and_windows_manifest_bytes(self) -> None:
        manifest_bytes = MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
        pinned = re.search(r"MIA_BUNDLED_EXPRESSION_MANIFEST_HASHES = Object.freeze\(\[(.*?)\]\)", self.javascript, re.S)
        self.assertIsNotNone(pinned)
        self.assertEqual(set(re.findall(r'"([a-f0-9]{64})"', pinned.group(1))), {
            hashlib.sha256(manifest_bytes).hexdigest(),
            hashlib.sha256(manifest_bytes.replace(b"\n", b"\r\n")).hexdigest(),
        })

    def test_client_loads_the_manifest_without_mia_only_asset_maps(self) -> None:
        for state, record in self.manifest["expressions"].items():
            self.assertNotIn(f'{state}: "{record["face_asset_path"]}"', self.javascript, state)
            self.assertNotIn(f'{state}: "{record["stage_asset_path"]}"', self.javascript, state)
        self.assertIn('const MIA_CHARACTER_ID = "702f4375675b"', self.javascript)
        self.assertIn(
            'const MIA_BUNDLED_EXPRESSION_MANIFEST = "/assets/expressions/mia/manifest.json"',
            self.javascript,
        )
        self.assertIn("function expressionStateForMessage(message)", self.javascript)
        self.assertIn("function loadExpressionManifest(character, signal)", self.javascript)
        self.assertIn(
            "function normalizeExpressionManifest(payload, character, manifestUrl)", self.javascript
        )
        self.assertIn("function updateStageCharacterArt(node, character, expressionState, performanceId = \"\")", self.javascript)
        self.assertIn("const STAGE_PRESENTATION_RENDERERS = Object.freeze", self.javascript)
        self.assertIn("function stagePresentationRenderer(presentation)", self.javascript)
        self.assertIn("async function preloadStagePresentation(presentation, signal)", self.javascript)
        self.assertIn(
            'requestedState, "neutral"',
            self.javascript,
        )
        self.assertNotIn("if (character?.character_id !== MIA_CHARACTER_ID)", self.javascript)
        self.assertIn('const artNode = $("stage-character-art")', self.javascript)
        self.assertIn("updateStageCharacterArt(artNode, character, expressionState,", self.javascript)
        self.assertNotIn("stage-portrait-avatar", self.javascript)
        self.assertNotIn("function fillStagePortrait", self.javascript)

    def test_character_switch_keeps_a_decoded_outgoing_layer_for_crossfade(self) -> None:
        stylesheet = (PUBLIC_ROOT / "app.css").read_text(encoding="utf-8")
        self.assertIn("function crossfadeStageCharacterArt(node)", self.javascript)
        self.assertIn("previousCharacterId !== characterId", self.javascript)
        self.assertIn("duration: 280", self.javascript)
        self.assertIn("Promise.allSettled", self.javascript)
        self.assertIn(".stage-character-art-outgoing", stylesheet)
        self.assertIn('data-stage-art-transition="incoming"', stylesheet)
        self.assertIn("if (previousId && previousId !== characterId) cancelStageArt();", self.javascript)
        self.assertNotIn(
            "if (previousId && previousId !== characterId) cancelStageArt({ hide: true });",
            self.javascript,
        )

    def test_public_materials_page_discloses_the_waiver_and_manifest(self) -> None:
        self.assertIn("米娅舞台表情（0.9.5）", self.privacy_html)
        self.assertIn("首次发布跳过独立素材权利核验", self.privacy_html)
        self.assertIn("不表示 小吉终端 已确认", self.privacy_html)
        self.assertIn(
            'href="/assets/expressions/mia/manifest.json"',
            self.privacy_html,
        )
