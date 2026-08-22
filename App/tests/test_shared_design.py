from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.validate_shared_design import REQUIRED_SCENES, validate


class SharedDesignContractTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_root = Path(__file__).resolve().parents[1]

    def test_local_and_public_surfaces_use_one_design_and_scene_root(self) -> None:
        result = validate(self.app_root)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["design_version"], "0.9.2")
        self.assertEqual(result["shared_references"], {"local": True, "public": True})
        self.assertEqual(result["missing_scenes"], [])
        self.assertEqual(result["duplicate_public_scene_assets"], [])
        self.assertEqual(result["required_scene_count"], len(REQUIRED_SCENES))

    def test_duplicate_public_scene_copy_is_rejected(self) -> None:
        # Copy only the small contract inputs into a temporary tree.  The
        # validator should fail before a second public SVG can drift away from
        # the canonical asset directory.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frontend" / "shared").mkdir(parents=True)
            (root / "frontend" / "assets" / "immersive" / "scenes").mkdir(parents=True)
            (root / "public_frontend").mkdir()
            (root / "public_frontend" / "assets" / "immersive" / "scenes").mkdir(parents=True)
            (root / "frontend" / "shared" / "immersive.css").write_text("", encoding="utf-8")
            (root / "frontend" / "shared" / "design-version.json").write_text(
                '{"design_version":"0.9.2","canonical_stylesheet":"/shared/immersive.css","canonical_scene_root":"/assets/immersive/scenes"}',
                encoding="utf-8",
            )
            (root / "frontend" / "index.html").write_text(
                '<link rel="stylesheet" href="/shared/immersive.css?v=0.9.2">',
                encoding="utf-8",
            )
            (root / "public_frontend" / "index.html").write_text(
                '<link rel="stylesheet" href="/shared/immersive.css?v=0.9.2">',
                encoding="utf-8",
            )
            for scene in REQUIRED_SCENES:
                (root / "frontend" / "assets" / "immersive" / "scenes" / f"{scene}.svg").write_text(
                    "<svg />", encoding="utf-8"
                )
            duplicate = root / "public_frontend" / "assets" / "immersive" / "scenes" / "generic.svg"
            duplicate.write_text("<svg />", encoding="utf-8")

            result = validate(root)
            self.assertEqual(result["status"], "invalid")
            self.assertIn("duplicate_public_scene_assets", result["errors"])
