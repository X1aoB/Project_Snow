from __future__ import annotations

import copy
import json
import os
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts import build_character_media_release as release
from scripts.character_layered_release import REVIEW_FLAGS


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _synthetic_layer_deriver(release_root, cid, runtime, approved, review_root, compositor, cache=None):
    """Builder integration fixture; codec/visual quality have independent core regressions."""
    derived = copy.deepcopy(runtime)
    mapping = []
    replacements = {}
    for relative, layer in release._runtime_layers(runtime).items():
        content = (release_root / relative).read_bytes()
        with Image.open(release_root / relative) as image:
            output = BytesIO()
            image.save(output, format="WEBP", quality=92, method=4, exact=True)
        encoded = output.getvalue()
        digest = sha256(encoded).hexdigest()
        target = f"expressions/{cid}/layers/{Path(relative).name.split('.')[0]}.{digest[:16]}.webp"
        (release_root / target).write_bytes(encoded)
        replacements[relative] = (target, digest)
        mapping.append({"source_asset_path": relative, "source_sha256": layer["asset_sha256"],
                        "delivery_asset_path": target, "delivery_sha256": digest,
                        "dimensions": layer["dimensions"], "source_bytes": len(content),
                        "delivery_bytes": len(encoded)})
    for presentation in derived["presentations"].values():
        for layer in presentation["layers"]:
            layer["asset_path"], layer["asset_sha256"] = replacements[layer["asset_path"]]
    directory = review_root / cid
    directory.mkdir()
    (directory / "report.json").write_text('{"synthetic_builder_fixture":true}\n', encoding="utf-8")
    summary = {"schema_version": "project-snow-character-webp-derivatives-1", "character_id": cid,
               "encoding": {"format": "webp", "quality": 92, "lossless": False, "method": 4,
                            "alpha_quality": 100, "exact": True},
               "thresholds": {"premultiplied_rgb_mae_max": 2.0, "premultiplied_rgb_p99_max": 10.0,
                              "luminance_ssim_min": 0.99, "alpha_max_delta": 0},
               "engine_versions": {"pillow": "fixture", "libwebp": "fixture", "chromium": "fixture"},
               "report_sha256": _digest(directory / "report.json"), "layer_mapping": mapping,
               "presentations": {pid: {"passed": True, "metrics": {"visible_pixels": 1,
                                   "alpha_max_delta": 0, "premultiplied_rgb_mae": 0.0,
                                   "premultiplied_rgb_p99": 0.0, "luminance_ssim": 1.0}}
                                 for pid in derived["presentations"]},
               "passed": True, "technical_qa_only": True, "human_approval": False}
    return derived, summary


class CharacterMediaReleaseTests(TestCase):
    @staticmethod
    def _save_approval(root, cid, approval):
        (root / "characters" / cid / "APPROVALS.pending.json").write_text(json.dumps(approval), encoding="utf-8")

    @staticmethod
    def _write_approved_character(root, cid):
        directory = root / "characters" / cid
        directory.mkdir(parents=True)
        def save(name, image):
            path = directory / name
            image.save(path, format="PNG")
            return {"asset_path": path.relative_to(root).as_posix(), "asset_sha256": _digest(path)}
        policy = root / "PRESENTATION_PIPELINE.pending.json"
        policy.write_text(json.dumps({
            "schema_version": "project-snow-character-presentation-pipeline-2", "publication_status": "pending_not_public",
            "authoring_policy": {
                "standard_expression_asset_scope": "layered_character_presentation", "neutral_base": "approved_rgba_pixel_locked",
                "expression_layer": "single_contiguous_head_face_layer_from_approved_expression",
                "micro_feature_mask_compositing": "forbidden", "feathered_face_recompositing": "forbidden",
                "complete_character_redraw_for_standard_expression": "forbidden", "partial_hand_or_limb_compositing": "forbidden",
            },
        }), encoding="utf-8")
        processing = directory / "source-processing.json"
        processing.write_text(json.dumps({"character_id": cid, "publication_status": "pending_not_public"}), encoding="utf-8")
        design = directory / "design.json"
        design.write_text(json.dumps({"character_id": cid}), encoding="utf-8")
        base = Image.new("RGBA", (12, 16))
        base.paste((80, 120, 160, 255), (2, 1, 10, 16))
        neutral = save("neutral.png", base)
        base_layer = {**neutral, "role": "neutral_base", "dimensions": {"width": 12, "height": 16},
                      "position": {"x": 0, "y": 0}, "composite": "source-over"}
        mask = Image.new("L", base.size)
        mask.paste(255, (3, 2, 9, 8))
        region = save("region.png", mask)
        rows = {}
        for i, state in enumerate(release.EXPRESSION_STATES):
            sprite = base.copy()
            layers = [base_layer]
            if state != "neutral":
                head = Image.new("RGBA", (6, 6))
                head.paste((170 + i, 100, 110, 255), (1, 1, 5, 5))
                head_ref = save(f"{state}.head.png", head)
                layers = [base_layer, {**head_ref, "role": "head_face", "dimensions": {"width": 6, "height": 6},
                                      "position": {"x": 3, "y": 2}, "composite": "source-over"}]
                sprite.alpha_composite(head, (3, 2))
            asset = save(f"{state}.preview.png", sprite)
            rows[state] = {
                "approved": True, "approved_variant": "A", "approved_round": 1, "user_decision": f"{state}=approve",
                **asset, "asset_scope": "layered_character_presentation", "render_strategy": "layered_sprite",
                "base_asset_path": neutral["asset_path"], "base_asset_sha256": neutral["asset_sha256"],
                "canvas": {"width": 12, "height": 16}, "layers": layers, "replacement_region": region,
                "layered_review": dict.fromkeys(REVIEW_FLAGS, True),
                "source_reference_path": asset["asset_path"], "source_reference_sha256": asset["asset_sha256"],
            }
        performances = {cue: {
            **copy.deepcopy(rows["happy"]), "label": cue, "usage_context": "Reviewed character context",
            "fallback_expression": "happy", "design_path": design.relative_to(root).as_posix(), "design_sha256": _digest(design),
        } for cue in ("friendly_greeting", "quiet_resolve", "shared_memory")}
        approval = {
            "schema_version": "project-snow-character-expression-approvals-1", "character_id": cid,
            "all_18_layered_presentations_approved": True,
            "neutral_review": {"approved": True, **neutral, "processing_manifest_path": processing.relative_to(root).as_posix(),
                               "processing_manifest_sha256": _digest(processing)},
            "presentation_policy": {"policy_path": policy.relative_to(root).as_posix(), "policy_sha256": _digest(policy),
                "render_strategy": "layered_sprite", "asset_scope": "layered_character_presentation",
                "micro_feature_mask_compositing": "forbidden", "partial_limb_compositing": "forbidden"},
            "face_crop_box_xyxy": [3, 2, 9, 8], "stage_layout": {"scale": 1.05, "focus_x": 48},
            "approved_presentations": rows, "approved_narrative_presentations": performances,
        }
        CharacterMediaReleaseTests._save_approval(root, cid, approval)
        return approval

    def test_rights_gate_rejects_missing_or_pending_decision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "RIGHTS.public.json"
            with self.assertRaisesRegex(ValueError, "separate public rights decision"):
                release._rights_header(path, "a" * 64)
            path.write_text(json.dumps({"schema_version": "project-snow-character-expression-rights-1",
                "source_index_sha256": "a" * 64, "character_count": 22, "approved_expression_count": 396,
                "publication_status": "pending_not_public"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not approved for public runtime"):
                release._rights_header(path, "a" * 64)

    def test_layered_release_preserves_native_pixels_and_separate_performance_count(self):
        with patch.object(release, "EXPRESSION_STATES", ("neutral", "happy")), TemporaryDirectory() as directory:
            root, cid = Path(directory), "a" * 12
            approval = self._write_approved_character(root, cid)
            resolved = release._validate_character_approval(root, {"character_id": cid})
            target = root / "release"
            manifest = release._render_runtime_manifest(target, cid, "Test", resolved, {}, "test", {})
            self.assertEqual(set(manifest["expressions"]), {"neutral", "happy"})
            self.assertEqual(manifest["performance_count"], 3)
            self.assertEqual(len(manifest["presentations"]), 5)
            composed = Image.new("RGBA", (12, 16))
            for layer in manifest["presentations"]["expression.happy"]["layers"]:
                path = target / layer["asset_path"]
                self.assertEqual(_digest(path), layer["asset_sha256"])
                with Image.open(path) as image:
                    self.assertEqual(image.size, (layer["dimensions"]["width"], layer["dimensions"]["height"]))
                    composed.alpha_composite(image, (layer["position"]["x"], layer["position"]["y"]))
            with Image.open(root / approval["approved_presentations"]["happy"]["asset_path"]) as preview:
                self.assertTrue(np.array_equal(np.asarray(composed), np.asarray(preview)))
            for row in manifest["expressions"].values():
                with Image.open(target / row["face_asset_path"]) as image:
                    self.assertEqual(image.size, (384, 384))
                with Image.open(target / row["stage_asset_path"]) as image:
                    self.assertEqual(image.height, 1024)

    def test_rejected_or_unapproved_sources_cannot_be_packaged(self):
        for fault in ("legacy_policy", "raw_only", "incomplete_review", "missing_performances", "cross_character_design", "missing_head"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root, cid = Path(directory), "b" * 12
                approval = self._write_approved_character(root, cid)
                happy = approval["approved_presentations"]["happy"]
                if fault == "legacy_policy": approval["presentation_policy"]["render_strategy"] = "single_sprite"
                elif fault == "raw_only": approval["all_18_layered_presentations_approved"] = False
                elif fault == "incomplete_review": happy["layered_review"]["no_layer_seam_or_duplicate_feature"] = False
                elif fault == "missing_performances": approval["approved_narrative_presentations"] = {}
                elif fault == "missing_head": happy["layers"][1]["role"] = "front_hair"
                else:
                    row = approval["approved_narrative_presentations"]["friendly_greeting"]
                    path = root / row["design_path"]
                    path.write_text(json.dumps({"character_id": "c" * 12}), encoding="utf-8")
                    for item in approval["approved_narrative_presentations"].values(): item["design_sha256"] = _digest(path)
                self._save_approval(root, cid, approval)
                with self.assertRaises(ValueError): release._validate_character_approval(root, {"character_id": cid})

    def test_tampered_layer_and_preview_are_rejected(self):
        for fault in ("hash", "geometry", "preview", "outside_region", "partial_limb"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root, cid = Path(directory), "d" * 12
                approval = self._write_approved_character(root, cid)
                row = approval["approved_presentations"]["happy"]
                layer = row["layers"][1]
                if fault == "hash": (root / layer["asset_path"]).write_bytes(b"bad")
                elif fault == "geometry": layer["position"]["x"] = 10
                elif fault == "outside_region":
                    path = root / row["replacement_region"]["asset_path"]
                    image = Image.new("L", (12, 16)); image.putpixel((0, 0), 255); image.save(path)
                    row["replacement_region"]["asset_sha256"] = _digest(path)
                elif fault == "partial_limb":
                    row["layers"].append({**copy.deepcopy(layer), "role": "connected_gesture"})
                    row["connected_gesture"] = {"old_limbs_removed": True, "arm_chains": [{"side": "left", "parts": ["hand"]}]}
                else:
                    path = root / row["asset_path"]
                    with Image.open(path) as source: image = source.copy()
                    image.putpixel((3, 3), (255, 255, 255, 255)); image.save(path)
                    row["asset_sha256"] = _digest(path)
                self._save_approval(root, cid, approval)
                with self.assertRaises(ValueError): release._validate_character_approval(root, {"character_id": cid})

    def test_rights_rows_bind_each_source_and_approval_hash(self):
        ids = [f"{i:012x}" for i in range(22)]
        sources = {cid: {"source_sha256": f"{i+1:064x}"} for i, cid in enumerate(ids)}
        approvals = {cid: {"approval_manifest_sha256": f"{i+101:064x}"} for i, cid in enumerate(ids)}
        manifest = {"characters": [{"character_id": cid, **sources[cid], **approvals[cid]} for cid in ids]}
        release._validate_rights_rows(manifest, sources, approvals)
        manifest["characters"][-1]["approval_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "rights approval hash mismatch"):
            release._validate_rights_rows(manifest, sources, approvals)

    def test_webp_requires_a_new_review_directory_outside_sources_and_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "delivery"
            source.mkdir(); output.mkdir()
            with self.assertRaisesRegex(ValueError, "requires a new"):
                release._layer_review_destination("webp-q92", None, [source, output])
            for target in (source / "review", output / "review", root):
                with self.subTest(target=target), self.assertRaises((ValueError, FileExistsError)):
                    release._layer_review_destination("webp-q92", target, [source, output])
            review = root / "private-review"
            self.assertEqual(release._layer_review_destination("webp-q92", review, [source, output]), review)
            review.mkdir(); (review / "decision.json").write_bytes(b"preserve")
            with self.assertRaises(FileExistsError):
                release._layer_review_destination("webp-q92", review, [source, output])
            self.assertEqual((review / "decision.json").read_bytes(), b"preserve")
            with self.assertRaisesRegex(ValueError, "requires webp-q92"):
                release._layer_review_destination("png", root / "unused", [])

    def test_failed_layer_review_retains_private_evidence_without_overwriting(self):
        with TemporaryDirectory() as directory:
            review = Path(directory) / "review"
            with self.assertRaisesRegex(ValueError, "synthetic quality failure"):
                with release._layer_review_attempt(review, "fixture"):
                    (review / "comparison.png").write_bytes(b"failed candidate evidence")
                    raise ValueError("synthetic quality failure")
            status = json.loads((review / "build-status.json").read_bytes())
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["failure_type"], "ValueError")
            self.assertFalse(status["human_approval"])
            with self.assertRaises(FileExistsError):
                with release._layer_review_attempt(review, "another"):
                    self.fail("an existing review directory must not be reused")
            self.assertEqual((review / "comparison.png").read_bytes(), b"failed candidate evidence")

    def test_webp_conversion_cannot_change_fallbacks_geometry_or_leak_private_metadata(self):
        for fault in ("fallback", "geometry", "private_summary", "quality", "encoding_method", "report"):
            with (self.subTest(fault=fault), TemporaryDirectory() as directory,
                  patch.object(release, "EXPRESSION_STATES", ("neutral", "happy"))):
                root, cid = Path(directory), "a" * 12
                self._write_approved_character(root, cid)
                approved = release._validate_character_approval(root, {"character_id": cid})
                runtime = release._render_runtime_manifest(root / "delivery", cid, "Fixture", approved, {}, "test", {})
                before = copy.deepcopy(runtime)
                review = root / "review"; review.mkdir()
                def faulty(*args, **kwargs):
                    changed, summary = _synthetic_layer_deriver(*args, **kwargs)
                    if fault == "fallback": changed["expressions"]["neutral"]["face_asset_sha256"] = "0" * 64
                    elif fault == "geometry": changed["presentations"]["expression.neutral"]["layers"][0]["position"]["x"] = 1
                    elif fault == "private_summary": summary["private_path"] = "C:/private/source"
                    elif fault == "quality": summary["presentations"]["expression.neutral"]["metrics"]["alpha_max_delta"] = 1
                    elif fault == "encoding_method": summary["encoding"]["method"] = 6
                    else: summary["report_sha256"] = "0" * 64
                    return changed, summary
                with patch.object(release, "_derive_runtime_layers", side_effect=faulty), self.assertRaises(ValueError):
                    release._checked_webp_runtime(root / "delivery", cid, runtime, approved, review, object(), {})
                self.assertEqual(runtime, before)

    @skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
    def test_real_webp_derivatives_satisfy_the_builder_public_summary_boundary(self):
        from tests.test_character_webp_derivatives import fixture
        with TemporaryDirectory() as directory, release.layered_release.NativeBrowserCompositor() as compositor:
            cid, _, delivery, review, approved, runtime = fixture(Path(directory))
            runtime["transforms"] = {"format": "original_native_png_layers_with_lossless_webp_derivatives"}
            before = copy.deepcopy(runtime)
            converted, summary = release._checked_webp_runtime(
                delivery, cid, runtime, approved, review, compositor, {}
            )
            self.assertEqual(runtime, before)
            self.assertTrue(summary["passed"])
            self.assertEqual(summary["encoding"]["quality"], 92)
            self.assertEqual(summary["encoding"]["method"], 4)
            self.assertEqual(converted["transforms"]["format"],
                             "q92_webp_native_layers_with_original_lossless_webp_derivatives")
