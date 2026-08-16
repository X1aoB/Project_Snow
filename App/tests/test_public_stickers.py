from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from backend.snow_app.public_contracts import ChatRequest
from backend.snow_app.public_service import PublicChatService
from backend.snow_app.public_stickers import PublicStickerCatalog


def _write_release(root: Path) -> None:
    (root / "stickers").mkdir(parents=True)
    (root / "thumbnails").mkdir(parents=True)
    source = root / "stickers" / "asset123.png"
    thumbnail = root / "thumbnails" / "asset123.webp"
    Image.new("RGBA", (24, 24), (30, 120, 180, 255)).save(source, format="PNG")
    Image.new("RGBA", (16, 16), (30, 120, 180, 255)).save(thumbnail, format="WEBP")
    entry = {
        "asset_id": "asset123",
        "caption": "测试表情",
        "section": "测试",
        "path": "stickers/asset123.png",
        "thumbnail_path": "thumbnails/asset123.webp",
        "sha256": sha256(source.read_bytes()).hexdigest(),
        "thumbnail_sha256": sha256(thumbnail.read_bytes()).hexdigest(),
        "mime_type": "image/png",
        "animated": False,
        "width": 24,
        "height": 24,
    }
    (root / "manifest.json").write_text(
        json.dumps({"media_version": "test-stickers", "count": 1, "stickers": [entry]}),
        encoding="utf-8",
    )
    files = sorted(path for path in root.rglob("*") if path.is_file())
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )


class PublicStickerCatalogTests(TestCase):
    def test_wire_contract_requires_text_before_sticker(self) -> None:
        common = {
            "request_id": "00000000-0000-0000-0000-000000000001",
            "provider": "openai",
            "credential": "c" * 24,
            "model": "test-model",
            "character_id": "character-123",
        }
        with self.assertRaises(ValueError):
            ChatRequest(
                **common,
                content_blocks=[
                    {"type": "sticker", "asset_id": "asset123"},
                    {"type": "message", "text": "之后的文字"},
                ],
            )

    def test_manifest_hashes_and_pagination_are_verified(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_release(root)
            catalog = PublicStickerCatalog(root, "test-stickers")
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["sticker_count"], 1)
            page = catalog.list(limit=1)
            self.assertEqual(page["stickers"][0]["asset_id"], "asset123")
            self.assertEqual(catalog.resolve("asset123")["src"], "/media/test-stickers/stickers/asset123.png")
            service = object.__new__(PublicChatService)
            service.stickers = catalog
            blocks, answer, _truncated, _safety = service._public_generation_content(
                {
                    "content_blocks": [
                        {"type": "message", "text": "收到"},
                        {"type": "sticker", "asset_id": "asset123"},
                    ]
                },
                "text",
            )
            self.assertEqual(answer, "收到")
            self.assertEqual(blocks[-1]["src"], "/media/test-stickers/stickers/asset123.png")

    def test_invalid_asset_and_path_tampering_are_not_resolvable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_release(root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["stickers"][0]["path"] = "../outside.png"
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            catalog = PublicStickerCatalog(root, "test-stickers")
            self.assertEqual(catalog.verify(force=True)["status"], "unavailable")
            self.assertIsNone(catalog.resolve("../outside.png"))

    def test_candidate_roll_is_stable_for_same_request_and_day(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_release(root)
            service = object.__new__(PublicChatService)
            service.stickers = PublicStickerCatalog(root, "test-stickers")
            candidates = []
            for index in range(256):
                value = service.sticker_candidates(
                    request_id=f"request-{index:03d}",
                    character_id="character-123",
                )
                if value:
                    candidates = value
                    request_id = f"request-{index:03d}"
                    break
            self.assertTrue(candidates)
            self.assertEqual(
                candidates,
                service.sticker_candidates(request_id=request_id, character_id="character-123"),
            )
