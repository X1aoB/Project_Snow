from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from scripts.release_manifest import create_manifest


class ReleaseManifestTests(TestCase):
    def test_manifest_records_immutable_runtime_inputs(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        manifest = create_manifest(
            commit_sha="a" * 40,
            public_image="ghcr.io/x1aob/project_snow-public",
            public_digest="sha256:" + "b" * 64,
            embedding_image="ghcr.io/x1aob/project_snow-embedding",
            embedding_digest="sha256:" + "c" * 64,
            app_root=app_root,
        )
        self.assertEqual(manifest["schema_version"], "project-snow-release-1")
        self.assertEqual(manifest["app_version"], "0.7.0")
        self.assertEqual(manifest["data_version"], "2026.08.15.1")
        self.assertEqual(manifest["migration_heads"], ["20260815_0002"])
        self.assertEqual(manifest["application"]["digest"], "sha256:" + "b" * 64)
