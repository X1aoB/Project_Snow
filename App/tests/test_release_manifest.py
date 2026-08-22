from __future__ import annotations

from hashlib import sha256
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
        self.assertEqual(manifest["app_version"], "0.9.2")
        self.assertEqual(manifest["data_version"], "2026.08.19.1")
        self.assertEqual(manifest["media_version"], "2026.08.19.avatar.1")
        self.assertEqual(manifest["sticker_version"], "2026.08.19.sticker.1")
        self.assertEqual(
            set(manifest["release_artifacts"]), {"data", "avatar", "sticker"}
        )
        self.assertEqual(
            manifest["release_artifacts"]["data"]["version"],
            manifest["data_version"],
        )
        self.assertEqual(
            manifest["release_artifacts"]["avatar"]["version"],
            manifest["media_version"],
        )
        self.assertEqual(
            manifest["release_artifacts"]["sticker"]["version"],
            manifest["sticker_version"],
        )
        for kind, artifact in manifest["release_artifacts"].items():
            self.assertRegex(artifact["manifest_sha256"], r"^[0-9a-f]{64}$", kind)
            if kind != "data":
                self.assertRegex(artifact["checksums_sha256"], r"^[0-9a-f]{64}$", kind)
        self.assertEqual(manifest["migration_heads"], ["20260819_0004"])
        self.assertEqual(manifest["application"]["digest"], "sha256:" + "b" * 64)
        self.assertEqual(
            set(manifest["configuration_sha256"]),
            {
                "compose.prod.yml",
                "infra/Caddyfile",
                "infra/OriginEdge.Caddyfile",
                "scripts/cloudflare_origin_firewall.py",
                "ops/project-snow-origin-firewall.service",
                "ops/project-snow-origin-firewall.timer",
                "infra/egress-squid.conf",
                "infra/neo4j-entrypoint.sh",
                "infra/postgres/postgresql.conf",
                "infra/public-api.Dockerfile",
                "requirements-public.txt",
            },
        )
        for relative_path, recorded_digest in manifest["configuration_sha256"].items():
            actual_digest = sha256((app_root / relative_path).read_bytes()).hexdigest()
            self.assertEqual(recorded_digest, actual_digest, relative_path)
        self.assertEqual(
            set(manifest["release_control_sha256"]),
            {"ops/project-snow-release", "ops/project-snow-release.sudoers"},
        )
        for relative_path, recorded_digest in manifest["release_control_sha256"].items():
            actual_digest = sha256((app_root / relative_path).read_bytes()).hexdigest()
            self.assertEqual(recorded_digest, actual_digest, relative_path)
