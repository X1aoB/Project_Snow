from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import TestCase

from scripts.prepare_ci_runtime import prepare


APP_ROOT = Path(__file__).resolve().parents[1]


class PrepareCiRuntimeTests(TestCase):
    def test_contract_fixtures_follow_registry_without_corpus_or_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            outputs = prepare(
                app_root=temporary_root,
                registry_path=APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json",
            )

            views = [
                json.loads(line)
                for line in Path(outputs["character_views"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            avatars = json.loads(
                Path(outputs["avatar_manifest"]).read_text(encoding="utf-8")
            )

        self.assertEqual(len(views), 22)
        self.assertEqual(len(avatars["characters"]), 22)
        self.assertTrue(all(item["coverage"]["level"] == "limited" for item in views))
        self.assertTrue(all(item["retrieval_document_ids"] == [] for item in views))
        self.assertTrue(all(item["publishable"] is False for item in avatars["characters"]))
        self.assertTrue(all(item["local_path"] is None for item in avatars["characters"]))
        self.assertEqual(avatars["schema_version"], "project-snow-avatar-1.2")
