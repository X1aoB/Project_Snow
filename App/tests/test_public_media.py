from __future__ import annotations

import json
from io import BytesIO
from hashlib import sha1, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zlib import crc32

from PIL import Image

from backend.snow_app.public_media import EXPRESSION_STATES, PublicMediaCatalog
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
    @staticmethod
    def _write_character_expression_release(
        root: Path,
        *,
        publication_status: str = "public_runtime_enabled_by_explicit_operator_rights_waiver",
        runtime_schema: str = "project-snow-character-expression-runtime-1",
    ) -> str:
        character_id = "a" * 12
        avatar_root = root / "avatars"
        expression_root = root / "expressions" / character_id
        face_root = expression_root / "faces"
        stage_root = expression_root / "stage"
        avatar_root.mkdir(parents=True)
        face_root.mkdir(parents=True)
        stage_root.mkdir(parents=True)
        for size in (96, 200):
            Image.new("RGB", (size, size), (80, 140, 170)).save(
                avatar_root / f"{character_id}-{size}.webp",
                format="WEBP",
            )
        expressions = {}
        presentations = {}
        for index, expression_state in enumerate(sorted(EXPRESSION_STATES)):
            face_path = face_root / f"{expression_state}.webp"
            stage_path = stage_root / f"{expression_state}.webp"
            face_image = Image.new("RGBA", (384, 384), (index, 20, 30, 255))
            face_image.putpixel((0, 0), (0, 0, 0, 0))
            face_image.save(face_path, format="WEBP", lossless=True, exact=True)
            stage_image = Image.new("RGBA", (6, 1024), (index, 30, 40, 255))
            stage_image.putpixel((0, 0), (0, 0, 0, 0))
            stage_image.save(stage_path, format="WEBP", lossless=True, exact=True)
            face_sha256 = sha256(face_path.read_bytes()).hexdigest()
            stage_sha256 = sha256(stage_path.read_bytes()).hexdigest()
            hashed_face_path = face_path.with_name(
                f"{expression_state}.{face_sha256[:16]}.webp"
            )
            hashed_stage_path = stage_path.with_name(
                f"{expression_state}.stage.{stage_sha256[:16]}.webp"
            )
            face_path.replace(hashed_face_path)
            stage_path.replace(hashed_stage_path)
            face_path = hashed_face_path
            stage_path = hashed_stage_path
            face_relative = face_path.relative_to(root).as_posix()
            stage_relative = stage_path.relative_to(root).as_posix()
            expression = {
                "face_asset_path": face_relative,
                "face_asset_sha256": face_sha256,
                "stage_asset_path": stage_relative,
                "stage_asset_sha256": stage_sha256,
                "dimensions": {
                    "face": {"width": 384, "height": 384},
                    "stage": {"width": 6, "height": 1024},
                },
            }
            if runtime_schema == "project-snow-character-expression-runtime-2":
                presentation_id = f"expression.{expression_state}"
                expression.update(
                    {
                        "presentation_id": presentation_id,
                        "render_strategy": "single_sprite",
                        "asset_scope": "complete_character_sprite",
                        "stage_layout": {"scale": 1, "focus_x": 50},
                    }
                )
                presentations[presentation_id] = {
                    "render_strategy": "single_sprite",
                    "asset_scope": "complete_character_sprite",
                    "stage_asset_path": stage_relative,
                    "stage_asset_sha256": stage_sha256,
                    "dimensions": {"width": 6, "height": 1024},
                    "stage_layout": {"scale": 1, "focus_x": 50},
                }
            expressions[expression_state] = expression
        expression_manifest = {
            "schema_version": runtime_schema,
            "character_id": character_id,
            "expression_state_count": len(EXPRESSION_STATES),
            "publication_status": publication_status,
            "rights": {
                "waiver": {
                    "granted": publication_status.endswith("explicit_operator_rights_waiver"),
                    "scope": "fixture expression derivatives",
                },
                "independent_verification": publication_status.endswith("verified_source_rights"),
                "verification_status": (
                    "verified" if publication_status.endswith("verified_source_rights") else "not_performed"
                ),
            },
            "stage_layout": {"scale": 1, "focus_x": 50},
            "source": {
                "source_sha256": "c" * 64,
                "source_index_sha256": "d" * 64,
                "approval_manifest_sha256": "e" * 64,
            },
            "expressions": expressions,
        }
        if runtime_schema == "project-snow-character-expression-runtime-2":
            expression_manifest.update(
                {
                    "presentation_contract": {
                        "schema_version": "project-snow-stage-presentation-1",
                        "default_render_strategy": "single_sprite",
                        "active_render_strategies": ["single_sprite"],
                        "reserved_render_strategies": ["layered_sprite", "rigged_2d"],
                        "legacy_stage_asset_fallback_required": True,
                    },
                    "presentations": presentations,
                }
            )
        expression_manifest_path = expression_root / "manifest.json"
        expression_manifest_path.write_text(
            json.dumps(expression_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        row = {
            "character_id": character_id,
            "character_name": "角色",
            "thumbnail_path": f"avatars/{character_id}-96.webp",
            "thumbnail_sha256": sha256((avatar_root / f"{character_id}-96.webp").read_bytes()).hexdigest(),
            "stage_path": f"avatars/{character_id}-200.webp",
            "stage_sha256": sha256((avatar_root / f"{character_id}-200.webp").read_bytes()).hexdigest(),
            "expression_manifest_path": f"expressions/{character_id}/manifest.json",
            "expression_manifest_sha256": sha256(expression_manifest_path.read_bytes()).hexdigest(),
            "expression_state_count": len(EXPRESSION_STATES),
            "expression_source_sha256": "c" * 64,
            "expression_source_index_sha256": "d" * 64,
            "expression_approval_manifest_sha256": "e" * 64,
            **_provenance(),
        }
        manifest = {
            **_release_manifest([row]),
            "schema_version": "project-snow-character-media-4",
            "expression_character_count": 1,
            "expression_state_count": len(EXPRESSION_STATES),
            "expression_asset_count": len(EXPRESSION_STATES) * 2,
            "expression_source_index_sha256": "d" * 64,
            "expression_rights_manifest_sha256": "f" * 64,
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        checksum_paths = [
            root / "manifest.json",
            *sorted(path for path in root.rglob("*") if path.is_file()),
        ]
        checksum_paths = list(dict.fromkeys(checksum_paths))
        (root / "SHA256SUMS").write_text(
            "".join(
                f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
                for path in checksum_paths
                if path.name != "SHA256SUMS"
            ),
            encoding="utf-8",
        )
        return character_id

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
                        "thumbnail_sha256": sha256(
                            (avatar_root / f"{character_id}-96.webp").read_bytes()
                        ).hexdigest(),
                        "stage_path": f"avatars/{character_id}-200.webp",
                        "stage_sha256": sha256(
                            (avatar_root / f"{character_id}-200.webp").read_bytes()
                        ).hexdigest(),
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

    def test_character_media_v4_verifies_all_expression_assets_and_exposes_manifest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id = self._write_character_expression_release(root)
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["schema_version"], "project-snow-character-media-4")
            self.assertEqual(status["expression_character_count"], 1)
            self.assertEqual(status["verified_file_count"], 2 + 1 + 36)
            self.assertEqual(status["expected_file_count"], 2 + 1 + 36)
            self.assertEqual(
                catalog.expression_manifest(character_id),
                {
                    "url": f"/media/test-avatar/expressions/{character_id}/manifest.json",
                    "state_count": 18,
                    "sha256": sha256(
                        (root / "expressions" / character_id / "manifest.json").read_bytes()
                    ).hexdigest(),
                },
            )

    def test_expression_manifest_digest_is_bound_to_verified_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id = self._write_character_expression_release(root)
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            self.assertEqual(catalog.verify(force=True)["status"], "ok")
            trusted = catalog.expression_manifest(character_id)
            self.assertIsNotNone(trusted)
            leaf = root / "expressions" / character_id / "manifest.json"
            original_hash = sha256(leaf.read_bytes()).hexdigest()
            self.assertEqual(trusted["sha256"], original_hash)
            leaf.write_bytes(leaf.read_bytes() + b" ")
            # A modified leaf must never become its own new trust anchor.
            unforced = catalog.expression_manifest(character_id)
            if unforced is not None:
                self.assertEqual(unforced["sha256"], original_hash)
                self.assertNotEqual(unforced["sha256"], sha256(leaf.read_bytes()).hexdigest())
            self.assertEqual(catalog.verify(force=True)["status"], "unavailable")
            self.assertIsNone(catalog.expression_manifest(character_id))

    def test_character_media_accepts_v2_single_sprite_presentations(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id = self._write_character_expression_release(
                root,
                runtime_schema="project-snow-character-expression-runtime-2",
            )
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok", status["errors"])
            self.assertEqual(status["verified_file_count"], 2 + 1 + 36)

            expression_manifest_path = root / "expressions" / character_id / "manifest.json"
            expression_manifest = json.loads(expression_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(expression_manifest["presentations"]), 18)
            for state, expression in expression_manifest["expressions"].items():
                presentation = expression_manifest["presentations"][expression["presentation_id"]]
                self.assertEqual(presentation["render_strategy"], "single_sprite", state)
                self.assertEqual(
                    presentation["stage_asset_path"],
                    expression["stage_asset_path"],
                    state,
                )

    def test_character_media_v4_rejects_pending_expression_rights(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id = self._write_character_expression_release(
                root,
                publication_status="pending_not_public",
            )
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertIn(f"expression_rights_unverified:{character_id}", status["errors"])
            self.assertIsNone(catalog.expression_manifest(character_id))

    @staticmethod
    def _reseal_expression_release(root: Path, character_id: str, expression_manifest: dict) -> None:
        expression_path = root / "expressions" / character_id / "manifest.json"
        expression_path.write_text(json.dumps(expression_manifest), encoding="utf-8")
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["characters"][0]["expression_manifest_sha256"] = sha256(expression_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (root / "SHA256SUMS").write_text("".join(
            f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != "SHA256SUMS"
        ), encoding="utf-8")

    def _write_layered_release(self, root: Path, *, layer_format: str = "WEBP") -> tuple[str, dict]:
        character_id = self._write_character_expression_release(
            root, runtime_schema="project-snow-character-expression-runtime-2",
        )
        path = root / "expressions" / character_id / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["schema_version"] = "project-snow-character-expression-runtime-3"
        manifest["presentation_contract"] = {
            "schema_version": "project-snow-stage-presentation-2",
            "default_render_strategy": "layered_sprite",
            "active_render_strategies": ["single_sprite", "layered_sprite"],
            "legacy_stage_asset_fallback_required": True,
            "native_pixel_composition": True,
            "atomic_surface_commit": True,
        }
        expression = manifest["expressions"]["thinking"]
        presentation = manifest["presentations"][expression["presentation_id"]]
        for item in (expression, presentation):
            item["render_strategy"] = "layered_sprite"
            item["asset_scope"] = "layered_character_presentation"
        presentation["canvas"] = {"width": 8, "height": 12}
        presentation["layers"] = []
        layer_root = root / "expressions" / character_id / "layers"
        layer_root.mkdir()
        suffix = layer_format.lower()
        for name, size, position in (("underlay", (8, 12), (0, 0)), ("head", (3, 3), (2, 1))):
            layer_path = layer_root / f"{name}.{suffix}"
            layer_image = Image.new("RGBA", size, (100, 130, 160, 255))
            layer_image.putpixel((0, 0), (0, 0, 0, 0))
            layer_image.save(layer_path, format=layer_format, lossless=True, exact=True)
            digest = sha256(layer_path.read_bytes()).hexdigest()
            pinned_path = layer_path.with_name(f"{name}.{digest[:16]}.{suffix}")
            layer_path.replace(pinned_path)
            presentation["layers"].append({
                "asset_path": pinned_path.relative_to(root).as_posix(), "asset_sha256": digest,
                "dimensions": {"width": size[0], "height": size[1]},
                "position": {"x": position[0], "y": position[1]}, "composite": "source-over",
            })
        self._reseal_expression_release(root, character_id, manifest)
        return character_id, manifest

    def test_layered_release_verifies_native_layers_and_keeps_flattened_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id, manifest = self._write_layered_release(root)
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok", status["errors"])
            self.assertEqual(status["verified_file_count"], 41)
            self.assertEqual(status["expected_file_count"], 41)
            self.assertIsNotNone(catalog.expression_manifest(character_id))
            (root / manifest["expressions"]["thinking"]["stage_asset_path"]).unlink()
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "unavailable")
            self.assertTrue(any("stage_asset_path" in error for error in status["errors"]))

    def test_layered_release_rejects_bad_geometry_unpinned_and_corrupted_layers(self) -> None:
        cases = ("missing", "outside", "fractional", "wrong_dimensions", "wrong_blend", "tampered", "pending")
        for fault in cases:
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                character_id, manifest = self._write_layered_release(root)
                presentation = manifest["presentations"]["expression.thinking"]
                layer = presentation["layers"][1]
                if fault == "missing":
                    presentation["layers"] = []
                elif fault == "outside":
                    layer["position"]["x"] = 7
                elif fault == "fractional":
                    layer["position"]["x"] = 2.5
                elif fault == "wrong_dimensions":
                    layer["dimensions"]["height"] = 4
                elif fault == "wrong_blend":
                    layer["composite"] = "destination-out"
                elif fault == "pending":
                    manifest["publication_status"] = "pending_not_public"
                else:
                    (root / layer["asset_path"]).write_bytes(b"corrupt layer")
                self._reseal_expression_release(root, character_id, manifest)
                catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
                status = catalog.verify(force=True)
                self.assertEqual(status["status"], "unavailable", status)
                self.assertIsNone(catalog.expression_manifest(character_id))

    def test_native_png_layers_verify_and_are_served_without_conversion(self) -> None:
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles
        from starlette.testclient import TestClient

        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id, manifest = self._write_layered_release(root, layer_format="PNG")
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok", status["errors"])
            self.assertEqual(status["verified_file_count"], 41)
            self.assertIsNotNone(catalog.expression_manifest(character_id))
            self.assertTrue(manifest["expressions"]["thinking"]["stage_asset_path"].endswith(".webp"))
            app = Starlette(routes=[Mount("/", app=StaticFiles(directory=root))])
            with TestClient(app) as client:
                for layer in manifest["presentations"]["expression.thinking"]["layers"]:
                    response = client.get("/" + layer["asset_path"])
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["content-type"], "image/png")
                    self.assertEqual(response.content, (root / layer["asset_path"]).read_bytes())

    def test_native_png_rejects_invalid_chunks_alpha_dimensions_and_format(self) -> None:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return len(payload).to_bytes(4, "big") + kind + payload + crc32(kind + payload).to_bytes(4, "big")

        cases = ("bad_magic", "bad_crc", "opaque", "truncated", "wrong_dimensions",
                 "missing_idat", "empty_idat", "wrong_depth", "interlaced", "animated",
                 "duplicate_ihdr", "unknown_critical", "trailing_bytes", "oversized_chunk")
        for fault in cases:
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                character_id, manifest = self._write_layered_release(root, layer_format="PNG")
                layer = manifest["presentations"]["expression.thinking"]["layers"][1]
                path = root / layer["asset_path"]
                data = path.read_bytes()
                if fault == "bad_magic":
                    data = b"not-PNG!" + data[8:]
                elif fault == "bad_crc":
                    data = data[:-1] + bytes([data[-1] ^ 1])
                elif fault == "opaque":
                    output = BytesIO()
                    Image.new("RGB", (3, 3), (40, 50, 60)).save(output, format="PNG")
                    data = output.getvalue()
                elif fault == "truncated":
                    data = data[:-5]
                elif fault == "wrong_dimensions":
                    layer["dimensions"]["width"] = 4
                elif fault == "missing_idat":
                    data = data[:33] + chunk(b"IEND", b"")
                elif fault == "empty_idat":
                    data = data[:33] + chunk(b"IDAT", b"") + chunk(b"IEND", b"")
                elif fault in ("wrong_depth", "interlaced"):
                    header = bytearray(data[16:29])
                    header[8 if fault == "wrong_depth" else 12] = 16 if fault == "wrong_depth" else 1
                    data = data[:8] + chunk(b"IHDR", bytes(header)) + data[33:]
                elif fault == "animated":
                    data = data[:33] + chunk(b"acTL", (1).to_bytes(4, "big") + bytes(4)) + data[33:]
                elif fault == "duplicate_ihdr":
                    data = data[:33] + data[8:33] + data[33:]
                elif fault == "unknown_critical":
                    data = data[:33] + chunk(b"ABCD", b"") + data[33:]
                elif fault == "trailing_bytes":
                    data += b"trailing"
                else:
                    data = data[:33] + (0x7FFFFFFF).to_bytes(4, "big") + b"IDAT" + bytes(4)
                # Repin the intentionally malformed bytes so the format gate is exercised.
                digest = sha256(data).hexdigest()
                replacement = path.with_name(f"head.{digest[:16]}.png")
                if replacement != path:
                    path.unlink()
                replacement.write_bytes(data)
                layer.update(asset_path=replacement.relative_to(root).as_posix(), asset_sha256=digest)
                self._reseal_expression_release(root, character_id, manifest)
                catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
                status = catalog.verify(force=True)
                self.assertEqual(status["status"], "unavailable", status)
                expected = "alpha_missing" if fault == "opaque" else "dimensions_mismatch" if fault == "wrong_dimensions" else "unreadable"
                self.assertTrue(any(f"expression_layer_{expected}" in error for error in status["errors"]), status)
                self.assertIsNone(catalog.expression_manifest(character_id))

    def _write_performance_release(self, root: Path, count: int = 3) -> tuple[str, dict]:
        character_id, manifest = self._write_layered_release(root)
        manifest["performance_contract"] = {
            "schema_version": "project-snow-character-performance-1",
            "character_scoped": True, "base_expression_states_unchanged": True,
        }
        manifest["performances"] = {}
        for cue in ("friendly_greeting", "quiet_resolve", "shared_memory")[:count]:
            manifest["performances"][cue] = {
                **manifest["expressions"]["thinking"], "presentation_id": f"performance.{cue}",
                "label": cue, "usage_context": "Reviewed character context", "fallback_expression": "thinking",
            }
            manifest["presentations"][f"performance.{cue}"] = json.loads(json.dumps(
                manifest["presentations"]["expression.thinking"],
            ))
        manifest["performance_count"] = count
        path = root / "manifest.json"
        parent = json.loads(path.read_text(encoding="utf-8"))
        parent.update({"performance_count": count, "performance_asset_count": count * 2})
        parent["characters"][0]["performance_count"] = count
        path.write_text(json.dumps(parent), encoding="utf-8")
        self._reseal_expression_release(root, character_id, manifest)
        return character_id, manifest

    def test_performances_are_verified_separately_from_the_eighteen_base_states(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            character_id, manifest = self._write_performance_release(root)
            catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
            status = catalog.verify(force=True)
            self.assertEqual(status["status"], "ok", status["errors"])
            self.assertEqual(len(manifest["expressions"]), 18)
            approved = catalog.performance_catalog(character_id)
            self.assertEqual({row["performance_id"] for row in approved}, set(manifest["performances"]))
            self.assertEqual(catalog.performance_catalog("b" * 12), [])
            self.assertEqual(status["verified_file_count"], status["expected_file_count"])
            self.assertIsNotNone(catalog.expression_manifest(character_id))

    def test_performances_reject_invalid_fallback_cross_character_layers_and_counts(self) -> None:
        for fault in ("fallback", "cross_character", "count", "contract", "unreferenced"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                character_id, manifest = self._write_performance_release(root)
                cue = manifest["performances"]["friendly_greeting"]
                presentation = manifest["presentations"][cue["presentation_id"]]
                if fault == "fallback": cue["fallback_expression"] = "unknown"
                elif fault == "cross_character":
                    layer = presentation["layers"][0]
                    layer["asset_path"] = layer["asset_path"].replace(character_id, "b" * 12)
                elif fault == "count": manifest["performance_count"] = 4
                elif fault == "contract": manifest["performance_contract"]["character_scoped"] = False
                else: manifest["presentations"]["performance.unreviewed"] = dict(presentation)
                self._reseal_expression_release(root, character_id, manifest)
                status = PublicMediaCatalog(root, "test-avatar", (character_id,)).verify(force=True)
                self.assertEqual(status["status"], "unavailable", status)

    def test_removed_cues_do_not_disable_the_remaining_reviewed_performances(self) -> None:
        for count in (0, 1, 2):
            with self.subTest(count=count), TemporaryDirectory() as directory:
                root = Path(directory)
                character_id, manifest = self._write_performance_release(root, count)
                catalog = PublicMediaCatalog(root, "test-avatar", (character_id,))
                status = catalog.verify(force=True)
                self.assertEqual(status["status"], "ok", status["errors"])
                self.assertEqual(len(manifest["expressions"]), 18)
                self.assertEqual(
                    {row["performance_id"] for row in catalog.performance_catalog(character_id)},
                    set(manifest["performances"]),
                )
                self.assertNotIn("shared_memory", manifest["performances"])
                self.assertEqual(status["verified_file_count"], status["expected_file_count"])

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
