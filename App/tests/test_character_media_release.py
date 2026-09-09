from __future__ import annotations

import copy
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts import build_character_media_release as release
from scripts.character_layered_release import REVIEW_FLAGS


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


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
