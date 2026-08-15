from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from backend.snow_app.public_media import PublicMediaCatalog


class PublicMediaCatalogTests(TestCase):
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
                        "source_page": "https://example.invalid/source",
                        "license": "CC BY-NC-SA",
                        "license_version": "version unspecified by source",
                    }
                )
            (root / "manifest.json").write_text(
                json.dumps({"media_version": "test-avatar", "characters": rows}),
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
