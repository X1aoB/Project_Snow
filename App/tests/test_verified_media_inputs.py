from __future__ import annotations

import copy
import json
import os
import shutil
from contextlib import nullcontext
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from scripts import build_character_media_release as builder
from scripts import verified_media_inputs as inputs
from tests import test_character_media_release as character_tests
from tests.test_public_media import _provenance


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def fixture_webp(image):
    # Reuse tests care about immutable fallback bytes, not production encoding effort.
    output = BytesIO()
    image.save(output, format="WEBP", lossless=True, quality=0, method=0, exact=True)
    return output.getvalue()


def seal(root):
    lines = [
        f"{digest(path)}  {path.relative_to(root).as_posix()}\n"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    (root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    return digest(root / "manifest.json"), digest(root / "SHA256SUMS")


def avatar_fixture(root, identifiers):
    root.mkdir()
    rows = []
    for cid in [*identifiers, "analyst-default"]:
        prefix = "analyst" if cid == "analyst-default" else "avatars"
        row = {**_provenance(), "release_basis": "verified_public_release"}
        row["asset_id" if prefix == "analyst" else "character_id"] = cid
        for kind, size in (("thumbnail", 96), ("stage", 200)):
            relative = f"{prefix}/{cid}-{size}.webp"
            path = root / relative
            path.parent.mkdir(exist_ok=True)
            Image.new("RGB", (size, size), (20, 30, 40)).save(path, format="WEBP")
            row.update(
                {
                    kind + "_path": relative,
                    kind + "_sha256": digest(path),
                    kind + "_width": size,
                    kind + "_height": size,
                }
            )
        rows.append(row)
    manifest = {
        "schema_version": "project-snow-avatar-media-3",
        "media_version": "fixture-avatar",
        "character_count": len(identifiers),
        "private_candidate": False,
        "release_basis": "verified_public_release",
        "license_review_status": "verified_public_release",
        "license_policy": "fixture original attribution",
        "characters": rows[:-1],
        "analyst": rows[-1],
    }
    write_json(root / "manifest.json", manifest)
    return seal(root)


class VerifiedMediaInputTests(TestCase):
    def test_private_metadata_cannot_cross_the_public_runtime_contract(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(builder, "EXPRESSION_STATES", ("neutral", "happy")),
        ):
            root, cid = Path(directory), "a" * 12
            character_tests.CharacterMediaReleaseTests._write_approved_character(root, cid)
            approved = builder._validate_character_approval(root, {"character_id": cid})
            runtime = builder._render_runtime_manifest(
                root / "package", cid, "Fixture", approved, {}, "pending_not_public", {}
            )
            builder._public_runtime_shape(runtime)
            for location in (
                "root",
                "expression",
                "presentation",
                "layer",
                "contract",
                "transforms",
                "dimensions",
            ):
                changed = copy.deepcopy(runtime)
                nodes = {
                    "root": changed,
                    "expression": changed["expressions"]["neutral"],
                    "presentation": changed["presentations"]["expression.neutral"],
                    "layer": changed["presentations"]["expression.neutral"]["layers"][0],
                    "contract": changed["presentation_contract"],
                    "transforms": changed["transforms"],
                    "dimensions": changed["expressions"]["neutral"]["dimensions"]["stage"],
                }
                nodes[location]["internal_source_path"] = "C:/private/operator-notes.json"
                with (
                    self.subTest(location=location),
                    self.assertRaisesRegex(ValueError, "outside the public"),
                ):
                    builder._public_runtime_shape(changed)

            for section, field in (
                ("transforms", "format"),
                ("transforms", "face"),
                ("presentation_contract", "schema_version"),
                ("performance_contract", "schema_version"),
            ):
                for private_value in (
                    {"internal_source_path": "C:/private/operator-notes.json"},
                    "C:/private/operator-notes.json",
                ):
                    changed = copy.deepcopy(runtime)
                    changed[section][field] = private_value
                    with (
                        self.subTest(section=section, field=field, value=private_value),
                        self.assertRaisesRegex(ValueError, "outside the public"),
                    ):
                        builder._public_runtime_shape(changed)

    def test_avatar_reuse_preserves_all_bytes_and_attribution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ids = {f"{i:012x}" for i in range(22)}
            pins = avatar_fixture(root / "source", sorted(ids))
            package = inputs.VerifiedPackage(root / "source", *pins)
            inputs.validate_avatar(package, ids)
            manifest = inputs.copy_avatar(package, root / "copy", "new-version")
            self.assertEqual(manifest["characters"], package.manifest["characters"])
            self.assertEqual(manifest["analyst"], package.manifest["analyst"])
            self.assertEqual(manifest["license_policy"], package.manifest["license_policy"])
            self.assertEqual(manifest["avatar_source_package"]["manifest_sha256"], pins[0])
            for relative, expected in package.files.items():
                if relative != "manifest.json":
                    self.assertEqual(digest(root / "copy" / relative), expected)
            self.assertEqual(len(list((root / "copy").rglob("*.webp"))), 46)

    def test_metadata_pins_are_mandatory_and_tampering_fails_closed(self):
        for fault in ("missing_pin", "manifest", "checksums", "image", "extra_file", "missing_file"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                pins = list(avatar_fixture(root, ["a" * 12]))
                if fault == "missing_pin":
                    pins[0] = None
                elif fault in {"manifest", "checksums"}:
                    (root / ("manifest.json" if fault == "manifest" else "SHA256SUMS")).write_bytes(
                        b"changed"
                    )
                elif fault == "image":
                    (root / "avatars" / ("a" * 12 + "-96.webp")).write_bytes(b"changed")
                elif fault == "extra_file":
                    (root / "secret.env").write_bytes(b"do-not-publish")
                else:
                    (root / "analyst/analyst-default-96.webp").unlink()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    inputs.VerifiedPackage(root, *pins)

    def test_checksum_duplicates_traversal_and_noncanonical_names_are_rejected(self):
        for relative in (
            "../outside",
            "/absolute",
            "C:/drive",
            "avatars\\a.webp",
            "./manifest.json",
            "manifest.json",
        ):
            with self.subTest(relative=relative), TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                pins = avatar_fixture(root, ["a" * 12])
                path = root / "SHA256SUMS"
                path.write_bytes(path.read_bytes() + f"{'0' * 64}  {relative}\n".encode())
                with self.assertRaises(ValueError):
                    inputs.VerifiedPackage(root, pins[0], digest(path))

    def test_links_and_nonregular_inputs_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            pins = avatar_fixture(root, ["a" * 12])
            target = root / "avatars" / ("a" * 12 + "-96.webp")
            os.link(target, Path(directory) / "hardlink.webp")
            with self.assertRaisesRegex(ValueError, "single regular"):
                inputs.VerifiedPackage(root, *pins)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "single regular"):
                inputs.read_regular(Path(directory))

    def test_rehashed_but_unapproved_or_invalid_avatar_package_is_rejected(self):
        for fault in ("private", "schema", "license", "analyst", "shape"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                avatar_fixture(root, ["a" * 12])
                manifest = json.loads((root / "manifest.json").read_bytes())
                if fault == "private":
                    manifest["private_candidate"] = True
                elif fault == "schema":
                    manifest["schema_version"] = "project-snow-character-media-4"
                elif fault == "license":
                    manifest["characters"][0]["license_status"] = "pending"
                elif fault == "analyst":
                    manifest.pop("analyst")
                else:
                    row = manifest["characters"][0]
                    path = root / row["thumbnail_path"]
                    Image.new("RGB", (12, 12)).save(path, format="WEBP")
                    row["thumbnail_sha256"] = digest(path)
                write_json(root / "manifest.json", manifest)
                package = inputs.VerifiedPackage(root, *seal(root))
                with self.assertRaises(ValueError):
                    inputs.validate_avatar(package, {"a" * 12})

    def test_copy_rechecks_bytes_after_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = inputs.VerifiedPackage(root / "source", *avatar_fixture(root / "source", ["a" * 12]))
            path = "analyst/analyst-default-96.webp"
            (root / "source" / path).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                package.copy_file(path, root / "output")
            self.assertFalse((root / "output" / path).exists())

    def test_full_combined_release_reuses_approved_native_and_avatar_bytes(self):
        self._check_full_combined_release("png")

    def test_webp_combined_release_derives_only_layers_and_keeps_sources_and_fallbacks(self):
        self._check_full_combined_release("webp-q92")

    def test_webp_quality_failure_preserves_review_and_sources_without_publishing(self):
        self._check_full_combined_release("webp-q92", derivation_fault=True)

    def _check_full_combined_release(self, layer_format, derivation_fault=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw, private = root / "raw", root / "private"
            raw.mkdir()
            private.mkdir()
            characters = builder._registry()
            ids = [row["character_id"] for row in characters]
            avatar_pins = avatar_fixture(root / "avatar", ids)
            source_path = raw / "source-index.pending.json"
            write_json(source_path, {"fixture_source_validation": True})
            sources, approvals, rows = {}, {}, []
            template = None
            template_cid = None
            for character in characters:
                cid = character["character_id"]
                character_tests.CharacterMediaReleaseTests._write_approved_character(raw, cid)
                approved = builder._validate_character_approval(raw, {"character_id": cid})
                approvals[cid] = approved
                sources[cid] = {"character_id": cid, "source_sha256": "c" * 64}
                if template is None:
                    with patch.object(builder, "_webp_bytes", side_effect=fixture_webp):
                        runtime = builder._render_runtime_manifest(
                            private,
                            cid,
                            character["display_name"],
                            approved,
                            {"approval_manifest_sha256": approved["approval_manifest_sha256"]},
                            "pending_not_public",
                            {},
                        )
                    template, template_cid = copy.deepcopy(runtime), cid
                else:
                    # All synthetic characters use identical pixels. Encode once;
                    # retain 22 separately approved and hash-verified runtime trees.
                    def relocate(value):
                        if isinstance(value, dict):
                            return {key: relocate(item) for key, item in value.items()}
                        if isinstance(value, list):
                            return [relocate(item) for item in value]
                        if isinstance(value, str) and value.startswith(f"expressions/{template_cid}/"):
                            return value.replace(f"expressions/{template_cid}/", f"expressions/{cid}/", 1)
                        return value

                    runtime = relocate(template)
                    runtime.update(
                        {
                            "character_id": cid,
                            "display_name": character["display_name"],
                            "source": {"approval_manifest_sha256": approved["approval_manifest_sha256"]},
                        }
                    )
                    shutil.copytree(private / "expressions" / template_cid, private / "expressions" / cid)
                relative = f"expressions/{cid}/manifest.json"
                write_json(private / relative, runtime)
                rows.append(
                    {
                        "character_id": cid,
                        "manifest_path": relative,
                        "manifest_sha256": digest(private / relative),
                    }
                )
            count = 22 * 3
            write_json(
                private / "manifest.json",
                {
                    "schema_version": "project-snow-private-character-derivatives-1",
                    "publication_status": "pending_not_public",
                    "public_runtime_eligible": False,
                    "production_deployed": False,
                    "character_count": 22,
                    "base_state_count": 396,
                    "performance_count": count,
                    "presentation_count": 396 + count,
                    "characters": rows,
                    "approval_event_sha256": "d" * 64,
                },
            )
            private_pins = seal(private)
            rights = {
                "schema_version": "project-snow-character-expression-rights-1",
                "source_index_sha256": digest(source_path),
                "character_count": 22,
                "approved_expression_count": 396,
                "approved_performance_count": count,
                "publication_status": "public_runtime_enabled_by_explicit_operator_rights_waiver",
                "rights": {
                    "independent_verification": False,
                    "verification_status": "not_performed",
                    "public_use_authorized": True,
                    "waiver": {"granted": True, "scope": "all fixture approved inputs"},
                    "publication_authorization": {
                        "method": "user_instruction",
                        "source_thread_id": "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12,
                        "source_turn_id": "b" * 8 + "-bbbb-bbbb-bbbb-" + "b" * 12,
                        "recorded_at": "2026-09-09T00:00:00Z",
                        "statement_sha256": "e" * 64,
                        "approval_event_sha256": "d" * 64,
                        "authorization_record_sha256": "f" * 64,
                        "private_package_manifest_sha256": private_pins[0],
                        "private_package_checksums_sha256": private_pins[1],
                    },
                },
                "characters": [
                    {
                        "character_id": cid,
                        "source_sha256": "c" * 64,
                        "approval_manifest_sha256": approvals[cid]["approval_manifest_sha256"],
                    }
                    for cid in ids
                ],
            }
            rights_path = root / "rights.json"
            write_json(rights_path, rights)
            args = dict(
                avatar_source_root=root / "unavailable-originals",
                analyst_source_root=root / "unavailable-analyst",
                expression_root=raw,
                rights_path=rights_path,
                mia_runtime_root=root / "unused",
                output_root=root / "out",
                version="fixture-combined",
                existing_avatar_release_root=root / "avatar",
                existing_avatar_manifest_sha256=avatar_pins[0],
                existing_avatar_checksums_sha256=avatar_pins[1],
                expression_package_root=private,
                expression_package_manifest_sha256=private_pins[0],
                expression_package_checksums_sha256=private_pins[1],
                rights_sha256=digest(rights_path),
            )
            if layer_format == "webp-q92":
                args.update(layer_format=layer_format, layer_review_root=root / "private-layer-review")
            def derive_candidate(*values, **options):
                runtime, summary = character_tests._synthetic_layer_deriver(*values, **options)
                if derivation_fault:
                    next(iter(summary["presentations"].values()))["metrics"]["alpha_max_delta"] = 1
                return runtime, summary
            with (
                patch.object(builder, "_source_index", return_value=({}, sources, digest(source_path))),
                patch.object(
                    builder, "build_avatar_release", side_effect=AssertionError("must reuse existing avatars")
                ),
                patch.object(
                    builder,
                    "_render_runtime_manifest",
                    side_effect=AssertionError("must preserve reviewed encodings"),
                ),
                patch.object(builder, "_derive_runtime_layers", side_effect=derive_candidate) as derive,
                patch.object(builder.layered_release, "NativeBrowserCompositor", return_value=nullcontext(object())),
            ):
                if derivation_fault:
                    with self.assertRaisesRegex(ValueError, "exceeds the fixed"):
                        builder.build_release(**args)
                    self.assertFalse((root / "out/fixture-combined").exists())
                    self.assertEqual(list((root / "out").glob(".fixture-combined.candidate.*")), [])
                    inputs.VerifiedPackage(private, *private_pins).verify()
                    reports = list((root / "private-layer-review").glob("*/report.json"))
                    self.assertEqual(len(reports), 1)
                    before = reports[0].read_bytes()
                    self.assertEqual(json.loads((root / "private-layer-review/build-status.json").read_bytes())["status"],
                                     "failed")
                    with self.assertRaises(FileExistsError):
                        builder.build_release(**args)
                    self.assertEqual(reports[0].read_bytes(), before)
                    return
                output = builder.build_release(**args)
                with self.assertRaises(FileExistsError):
                    builder.build_release(**args)
                self.assertEqual(derive.call_count, 22 if layer_format == "webp-q92" else 0)
            original = inputs.VerifiedPackage(private, *private_pins)
            for relative, expected in original.files.items():
                if not relative.endswith("manifest.json"):
                    if layer_format == "webp-q92" and "/layers/" in relative:
                        self.assertFalse((output / relative).exists())
                    else:
                        self.assertEqual(digest(output / relative), expected)
            avatar_original = inputs.VerifiedPackage(root / "avatar", *avatar_pins)
            for relative, expected in avatar_original.files.items():
                if relative != "manifest.json":
                    self.assertEqual(digest(output / relative), expected)
            manifest = json.loads((output / "manifest.json").read_bytes())
            self.assertEqual(manifest["schema_version"], "project-snow-character-media-4")
            self.assertEqual(manifest["performance_count"], count)
            self.assertEqual(
                manifest["analyst"], json.loads((root / "avatar/manifest.json").read_bytes())["analyst"]
            )
            self.assertFalse(
                json.loads((output / rows[0]["manifest_path"]).read_bytes())["rights"][
                    "independent_verification"
                ]
            )
            if layer_format == "webp-q92":
                derivation = manifest["expression_layer_derivation"]
                self.assertEqual(derivation["character_count"], 22)
                self.assertTrue(derivation["source_approval_exact"])
                self.assertFalse(derivation["human_approval"])
                self.assertEqual(len(derivation["characters"]), 22)
                self.assertEqual(list((output / "expressions").rglob("*.png")), [])
                self.assertNotIn(str(root), json.dumps(manifest))
                for summary in derivation["characters"]:
                    cid = summary["character_id"]
                    report = root / "private-layer-review" / cid / "report.json"
                    self.assertEqual(digest(report), summary["report_sha256"])
                    public_runtime = json.loads((output / f"expressions/{cid}/manifest.json").read_bytes())
                    with self.assertRaisesRegex(ValueError, "outside the public"):
                        builder._public_runtime_shape(public_runtime)
                    for layer in builder._runtime_layers(public_runtime).values():
                        self.assertTrue(layer["asset_path"].endswith(".webp"))
                        self.assertEqual(digest(output / layer["asset_path"]), layer["asset_sha256"])
                self.assertEqual(json.loads((root / "private-layer-review/build-status.json").read_bytes())["status"],
                                 "built_for_review")
                self.assertEqual(list(output.rglob("report.json")), [])
            else:
                self.assertNotIn("expression_layer_derivation", manifest)
            # The same pinned private package cannot be republished under a different decision binding.
            changed = copy.deepcopy(rights)
            changed["rights"]["publication_authorization"]["approval_event_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "another private package"):
                builder._private_expression_manifests(original, changed, approvals)

    def test_waiver_cannot_claim_rights_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rights.json"
            rights = {
                "schema_version": "project-snow-character-expression-rights-1",
                "source_index_sha256": "a" * 64,
                "character_count": 22,
                "approved_expression_count": 396,
                "publication_status": "public_runtime_enabled_by_explicit_operator_rights_waiver",
                "rights": {
                    "independent_verification": True,
                    "verification_status": "verified",
                    "public_use_authorized": True,
                    "waiver": {"granted": True, "scope": "fixture"},
                },
            }
            write_json(path, rights)
            with self.assertRaisesRegex(ValueError, "must not claim"):
                builder._rights_header(path, "a" * 64)
