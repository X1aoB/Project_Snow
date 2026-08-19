from __future__ import annotations

from hashlib import sha1, sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from backend.snow_app.public_contracts import ChatRequest
from backend.snow_app.public_service import PublicChatService
from backend.snow_app.public_stickers import PublicStickerCatalog
from scripts.build_sticker_media import _resolve_character_ids, _source_sha1_matches


def _write_release(root: Path) -> None:
    (root / "stickers").mkdir(parents=True)
    (root / "thumbnails").mkdir(parents=True)
    (root / "display").mkdir(parents=True)
    source = root / "stickers" / "asset123.png"
    thumbnail = root / "thumbnails" / "asset123.webp"
    display = root / "display" / "asset123.webp"
    Image.new("RGBA", (24, 24), (30, 120, 180, 255)).save(source, format="PNG")
    Image.new("RGBA", (16, 16), (30, 120, 180, 255)).save(thumbnail, format="WEBP")
    Image.new("RGBA", (24, 24), (30, 120, 180, 255)).save(display, format="WEBP")
    entry = {
        "asset_id": "asset123",
        "caption": "测试表情",
        "section": "测试",
        "character_ids": [],
        "emotion_tags": ["neutral"],
        "candidate_scope": "generic",
        "path": "stickers/asset123.png",
        "thumbnail_path": "thumbnails/asset123.webp",
        "display_path": "display/asset123.webp",
        "sha256": sha256(source.read_bytes()).hexdigest(),
        "content_hash": sha256(source.read_bytes()).hexdigest(),
        "thumbnail_sha256": sha256(thumbnail.read_bytes()).hexdigest(),
        "display_sha256": sha256(display.read_bytes()).hexdigest(),
        "mime_type": "image/png",
        "animated": False,
        "display_mime_type": "image/webp",
        "width": 24,
        "height": 24,
        "file_page_url": "https://wiki.biligame.com/sonw/%E6%96%87%E4%BB%B6:%E6%B5%8B%E8%AF%95.png",
        "source_page_url": "https://wiki.biligame.com/sonw/%E8%81%8A%E5%A4%A9%E8%A1%A8%E6%83%85",
        "source_image_url": "https://patchwiki.biligame.com/images/sonw/test.png",
        "license": "CC BY-NC-SA 4.0",
        "license_version": "4.0",
        "license_status": "verified",
        "source_revision_id": "12345",
        "source_revision_timestamp": "2026-08-19T00:00:00Z",
        "source_uploader": "WikiUser",
        "source_sha1": sha1(source.read_bytes()).hexdigest(),
        "license_source_page": "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5",
        "license_source_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "license_source_revision_id": "21546",
        "transformations": ["thumbnail WebP", "display WebP"],
        "attribution": "https://wiki.biligame.com/sonw/%E8%81%8A%E5%A4%A9%E8%A1%A8%E6%83%85",
    }
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "project-snow-sticker-2",
                "media_version": "test-stickers",
                "count": 1,
                "private_candidate": False,
                "license_review_status": "verified_public_release",
                "license_policy": "Fixture reviewed for tests.",
                "stickers": [entry],
            }
        ),
        encoding="utf-8",
    )
    _write_checksums(root)


def _write_checksums(root: Path) -> None:
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )


class PublicStickerCatalogTests(TestCase):
    def test_mediawiki_source_sha1_gate_rejects_corrupted_bytes(self) -> None:
        content = b"reviewed-sticker-source"
        declared = sha1(content).hexdigest()
        self.assertTrue(_source_sha1_matches(content, declared))
        self.assertFalse(_source_sha1_matches(content + b"tampered", declared))

    def test_public_release_license_review_covers_every_sticker(self) -> None:
        review = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "config"
                / "public_media"
                / "sticker_license_review.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(review["status"], "verified_public_release")
        self.assertEqual(review["license"], "CC BY-NC-SA 4.0")
        self.assertEqual(review["license_source_revision_id"], "21546")
        self.assertEqual(review["sticker_source_page_revision_id"], "32041")
        self.assertEqual(review["asset_count"], 363)
        self.assertEqual(review["passed_count"], 363)
        files = review["files"]
        self.assertEqual(len(files), 363)
        self.assertEqual(len({item["asset_id"] for item in files}), 363)
        self.assertTrue(all(item["source_revision_id"] for item in files))

    def test_costume_labels_resolve_to_the_base_character(self) -> None:
        lookup = {
            "凯茜娅": "25b23cb64398",
            "芬妮": "1b0a6b35719a",
            "里芙": "ca0144ccd81b",
        }
        self.assertEqual(
            _resolve_character_ids(
                ["凯茜娅·蓝闪", "芬妮·辉耀", "里芙·狂猎", "未知角色·装扮"],
                lookup,
            ),
            ["1b0a6b35719a", "25b23cb64398", "ca0144ccd81b"],
        )

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
            self.assertEqual(page["total"], 1)
            self.assertTrue(page["stickers"][0]["display_src"].endswith("display/asset123.webp"))
            self.assertEqual(catalog.list(q="测试")["total"], 1)
            self.assertEqual(catalog.list(emotion_tag="missing")["total"], 0)
            self.assertEqual(catalog.list(candidate_scope="generic")["total"], 1)
            self.assertEqual(catalog.list(candidate_scope="character")["total"], 0)
            with self.assertRaises(ValueError):
                catalog.list(candidate_scope="unknown")
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
            self.assertEqual(
                blocks[-1]["display_src"],
                "/media/test-stickers/display/asset123.webp",
            )
            self.assertEqual(blocks[-1]["display_mime_type"], "image/webp")

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

    def test_manifest_source_sha1_must_match_packaged_original(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_release(root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["stickers"][0]["source_sha1"] = "0" * 40
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            _write_checksums(root)
            status = PublicStickerCatalog(root, "test-stickers").verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("sticker_source_sha1_mismatch:asset123", status["errors"])

    def test_private_candidate_and_incomplete_license_are_hidden(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_release(root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["private_candidate"] = True
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            _write_checksums(root)
            catalog = PublicStickerCatalog(root, "test-stickers")
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("license_review_incomplete", status["errors"])
            self.assertEqual(catalog.list()["stickers"], [])

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

    def test_candidate_tiers_cooldown_scope_and_recent_asset_filter(self) -> None:
        class Catalog:
            def list(self, **_kwargs):
                return {
                    "status": "ok",
                    "stickers": [
                        {
                            "asset_id": "own0001",
                            "caption": "角色自己的表情",
                            "section": "角色",
                            "character_ids": ["character-123"],
                            "emotion_tags": ["neutral"],
                            "candidate_scope": "character",
                        },
                        {
                            "asset_id": "own0002",
                            "caption": "角色自己的另一张",
                            "section": "角色",
                            "character_ids": ["character-123"],
                            "emotion_tags": ["neutral"],
                            "candidate_scope": "character",
                        },
                        {
                            "asset_id": "generic1",
                            "caption": "通用表情",
                            "section": "通用",
                            "character_ids": [],
                            "emotion_tags": ["neutral"],
                            "candidate_scope": "generic",
                        },
                        {
                            "asset_id": "other001",
                            "caption": "其他角色专属",
                            "section": "角色",
                            "character_ids": ["other-12345"],
                            "emotion_tags": ["neutral"],
                            "candidate_scope": "character",
                        },
                    ],
                }

            def resolve(self, asset_id):
                return {"asset_id": asset_id, "src": "/x", "thumbnail_src": "/x", "caption": "x", "animated": False}

        service = object.__new__(PublicChatService)
        service.stickers = Catalog()
        ordinary = {}
        service.sticker_candidates(
            request_id="ordinary-request",
            character_id="character-123",
            message="普通消息",
            diagnostics=ordinary,
        )
        self.assertEqual(ordinary["sticker_probability_tier"], "ordinary")
        self.assertEqual(ordinary["sticker_probability"], 0.08)
        playful = {}
        service.sticker_candidates(
            request_id="playful-request",
            character_id="character-123",
            message="哈哈，发个表情包",
            diagnostics=playful,
        )
        self.assertEqual(playful["sticker_probability_tier"], "emotional")
        self.assertEqual(playful["sticker_probability"], 0.25)
        cooldown = {}
        history = [
            {"role": "assistant", "content_blocks": [{"type": "sticker", "asset_id": "own0001"}]},
            {"role": "assistant", "content_blocks": [{"type": "sticker", "asset_id": "own0002"}]},
        ]
        self.assertEqual(
            service.sticker_candidates(
                request_id="cooldown-request",
                character_id="character-123",
                message="普通消息",
                recent_history=history,
                diagnostics=cooldown,
            ),
            [],
        )
        self.assertEqual(cooldown["sticker_rejected_reason"], "cooldown")
        recent = {}
        candidates = service.sticker_candidates(
            request_id="explicit-request",
            character_id="character-123",
            message="请发个表情包",
            recent_history=[
                {"role": "assistant", "content_blocks": [{"type": "sticker", "asset_id": "own0001"}]}
            ],
            diagnostics=recent,
        )
        self.assertTrue(candidates)
        self.assertNotIn("other001", {item["asset_id"] for item in candidates})
        self.assertNotIn("generic1", {item["asset_id"] for item in candidates})
        self.assertNotEqual(candidates[0]["asset_id"], "own0001")
