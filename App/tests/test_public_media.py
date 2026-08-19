from __future__ import annotations

import json
from hashlib import sha1, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from backend.snow_app.public_media import PublicMediaCatalog
from scripts.build_avatar_media_release import _source_sha1_matches, build_release


def _provenance(name: str = "测试.png") -> dict[str, object]:
    return {
        "file_page_url": f"https://wiki.biligame.com/sonw/%E6%96%87%E4%BB%B6:{name}",
        "source_image_url": "https://patchwiki.biligame.com/images/sonw/test.png",
        "source_revision_id": "12345",
        "source_revision_timestamp": "2026-08-19T00:00:00Z",
        "source_uploader": "WikiUser",
        "source_sha1": "a" * 40,
        "original_sha1": "a" * 40,
        "original_sha256": "b" * 64,
        "license": "CC BY-NC-SA 4.0",
        "license_version": "4.0",
        "license_status": "verified_site_policy_no_page_exception",
        "license_source_page": "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5",
        "license_source_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "license_source_revision_id": "21546",
        "transformations": ["square crop", "WebP conversion"],
    }


def _release_manifest(characters: list[dict], analyst: dict | None = None) -> dict:
    return {
        "schema_version": "project-snow-avatar-media-3",
        "media_version": "test-avatar",
        "private_candidate": False,
        "license_review_status": "verified_public_release",
        "characters": characters,
        **({"analyst": analyst} if analyst is not None else {}),
    }


class PublicMediaCatalogTests(TestCase):
    def test_mediawiki_source_sha1_gate_rejects_corrupted_bytes(self) -> None:
        content = b"reviewed-avatar-source"
        declared = sha1(content).hexdigest()
        self.assertTrue(_source_sha1_matches(content, declared))
        self.assertFalse(_source_sha1_matches(content + b"tampered", declared))

    @staticmethod
    def _write_analyst_release(root: Path, *, license_status: str = "verified_explicit") -> None:
        avatar_root = root / "avatars"
        analyst_root = root / "analyst"
        avatar_root.mkdir()
        analyst_root.mkdir()
        character_id = "a" * 12
        files = []
        for directory, name, size, color in (
            (avatar_root, f"{character_id}-96.webp", 96, (80, 140, 170)),
            (avatar_root, f"{character_id}-200.webp", 200, (80, 140, 170)),
            (analyst_root, "analyst-default-96.webp", 96, (160, 180, 190)),
            (analyst_root, "analyst-default-200.webp", 200, (160, 180, 190)),
        ):
            path = directory / name
            Image.new("RGB", (size, size), color).save(path, format="WEBP")
            files.append(path)
        row = {
            "character_id": character_id,
            "character_name": "角色",
            "thumbnail_path": f"avatars/{character_id}-96.webp",
            "thumbnail_sha256": sha256(files[0].read_bytes()).hexdigest(),
            "stage_path": f"avatars/{character_id}-200.webp",
            "stage_sha256": sha256(files[1].read_bytes()).hexdigest(),
            **_provenance(),
        }
        analyst = {
            "asset_id": "analyst-default",
            "display_name": "分析员（默认头像）",
            "thumbnail_path": "analyst/analyst-default-96.webp",
            "thumbnail_sha256": sha256(files[2].read_bytes()).hexdigest(),
            "stage_path": "analyst/analyst-default-200.webp",
            "stage_sha256": sha256(files[3].read_bytes()).hexdigest(),
            **_provenance("%E5%88%86%E6%9E%90%E5%91%98%E5%A4%B4%E5%83%8F.png"),
            "source_revision_id": "6667",
            "license_status": license_status,
        }
        manifest = _release_manifest([row], analyst)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        checksum_paths = [manifest_path, *files]
        (root / "SHA256SUMS").write_text(
            "".join(
                f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
                for path in checksum_paths
            ),
            encoding="utf-8",
        )

    def test_manifest_and_both_portrait_sizes_are_verified(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            avatar_root = root / "avatars"
            avatar_root.mkdir()
            rows = []
            for character_id, color in (("a" * 12, (80, 140, 170)), ("b" * 12, (120, 160, 190))):
                for size in (96, 200):
                    image = Image.new("RGB", (size, size), color)
                    image.save(avatar_root / f"{character_id}-{size}.webp", format="WEBP")
                rows.append(
                    {
                        "character_id": character_id,
                        "character_name": character_id,
                        "thumbnail_path": f"avatars/{character_id}-96.webp",
                        "thumbnail_sha256": sha256((avatar_root / f"{character_id}-96.webp").read_bytes()).hexdigest(),
                        "stage_path": f"avatars/{character_id}-200.webp",
                        "stage_sha256": sha256((avatar_root / f"{character_id}-200.webp").read_bytes()).hexdigest(),
                        **_provenance(),
                    }
                )
            (root / "manifest.json").write_text(
                json.dumps(_release_manifest(rows)),
                encoding="utf-8",
            )
            checksum_paths = [root / "manifest.json", *sorted(avatar_root.glob("*.webp"))]
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
                    for path in checksum_paths
                ),
                encoding="utf-8",
            )
            catalog = PublicMediaCatalog(root, "test-avatar", (row["character_id"] for row in rows))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["checksums"], "ok")
            self.assertEqual(status["verified_file_count"], 4)
            avatar = catalog.avatar("a" * 12)
            self.assertEqual(avatar["src"], "/media/test-avatar/avatars/aaaaaaaaaaaa-200.webp")
            self.assertEqual(avatar["thumbnail_src"], "/media/test-avatar/avatars/aaaaaaaaaaaa-96.webp")

    def test_hash_tampering_degrades_catalog_and_hides_urls(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "avatars").mkdir()
            (root / "avatars" / ("a" * 12 + "-96.webp")).write_bytes(b"not-an-image")
            (root / "avatars" / ("a" * 12 + "-200.webp")).write_bytes(b"not-an-image")
            row = {
                "character_id": "a" * 12,
                "thumbnail_path": f"avatars/{'a' * 12}-96.webp",
                "thumbnail_sha256": "0" * 64,
                "stage_path": f"avatars/{'a' * 12}-200.webp",
                "stage_sha256": "0" * 64,
            }
            (root / "manifest.json").write_text(
                json.dumps({"media_version": "test-avatar", "characters": [row]}),
                encoding="utf-8",
            )
            (root / "SHA256SUMS").write_text(
                f"{sha256((root / 'manifest.json').read_bytes()).hexdigest()}  manifest.json\n",
                encoding="utf-8",
            )
            catalog = PublicMediaCatalog(root, "test-avatar", ("a" * 12,))
            self.assertEqual(catalog.verify(force=True)["status"], "unavailable")
            self.assertIsNone(catalog.avatar("a" * 12))

    def test_manifest_checksum_tampering_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"media_version": "test-avatar", "characters": []}),
                encoding="utf-8",
            )
            (root / "SHA256SUMS").write_text(
                f"{'0' * 64}  manifest.json\n",
                encoding="utf-8",
            )
            catalog = PublicMediaCatalog(root, "test-avatar", ())
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("manifest_checksum_mismatch", status["errors"])

    def test_analyst_manifest_exposes_verified_media_and_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_analyst_release(root)
            catalog = PublicMediaCatalog(root, "test-avatar", ("a" * 12,), require_analyst=True)
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["analyst"], "ok")
            self.assertEqual(status["verified_file_count"], 4)
            self.assertEqual(status["expected_file_count"], 4)
            analyst = catalog.analyst_avatar()
            self.assertEqual(analyst["asset_id"], "analyst-default")
            self.assertTrue(analyst["src"].endswith("analyst-default-200.webp"))
            self.assertEqual(analyst["license_version"], "4.0")
            self.assertEqual(analyst["license_source_revision_id"], "21546")

    def test_avatar_manifest_rejects_inconsistent_source_sha1_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_analyst_release(root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["characters"][0]["source_sha1"] = "0" * 40
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            checksum_paths = [
                root / "manifest.json",
                *sorted((root / "avatars").glob("*.webp")),
                *sorted((root / "analyst").glob("*.webp")),
            ]
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
                    for path in checksum_paths
                ),
                encoding="utf-8",
            )
            catalog = PublicMediaCatalog(
                root, "test-avatar", ("a" * 12,), require_analyst=True
            )
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("character_license_unverified:" + "a" * 12, status["errors"])

    def test_unverified_analyst_license_hides_url_and_degrades_package(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_analyst_release(root, license_status="pending_review")
            catalog = PublicMediaCatalog(root, "test-avatar", ("a" * 12,), require_analyst=True)
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("analyst_license_unverified", status["errors"])
            self.assertIsNone(catalog.analyst_avatar())

    def test_analyst_hash_tampering_hides_url(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_analyst_release(root)
            (root / "analyst" / "analyst-default-200.webp").write_bytes(b"tampered")
            catalog = PublicMediaCatalog(root, "test-avatar", ("a" * 12,), require_analyst=True)
            self.assertIsNone(catalog.analyst_avatar())
            self.assertEqual(catalog.verify(force=True)["analyst"], "unavailable")

    def test_unverified_analyst_source_does_not_create_partial_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            analyst_root = root / "analyst-source"
            analyst_root.mkdir()
            Image.new("RGB", (200, 200), (160, 180, 190)).save(
                analyst_root / "analyst-default.png",
                format="PNG",
            )
            (analyst_root / "analyst.json").write_text(
                json.dumps(
                    {
                        "publishable": True,
                        "license": "CC BY-NC-SA 4.0",
                        "license_version": "4.0",
                        "license_status": "pending_review",
                        "license_source_page": "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5",
                        "license_source_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
                        "license_source_revision_id": "21546",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_root = root / "releases"
            version = "test-unverified-avatar"
            character_source = root / "unused-character-source"
            character_source.mkdir()
            (character_source / "avatars.json").write_text(
                json.dumps({"characters": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "license review is incomplete"):
                build_release(
                    source_root=character_source,
                    analyst_source_root=analyst_root,
                    output_root=output_root,
                    version=version,
                )

            self.assertFalse((output_root / version).exists())
