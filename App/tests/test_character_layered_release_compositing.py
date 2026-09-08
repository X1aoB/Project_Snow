from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from scripts import build_character_media_release as release
from scripts import character_layered_release as layered
from tests import test_character_media_release as fixtures

_digest = fixtures._digest


class BrowserCompositingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compositor = layered.NativeBrowserCompositor()
        cls.compositor.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.compositor.__exit__(None, None, None)

    @staticmethod
    def browser_row(root, approval):
        row = approval["approved_presentations"]["happy"]
        head = row["layers"][1]
        # Skia premultiplies this source before compositing. This independently
        # specified browser result differs from Pillow's (80, 120, 160, 255).
        source = Image.new("RGBA", (6, 6))
        source.paste((37, 80, 190, 2), (1, 1, 5, 5))
        source.save(root / head["asset_path"])
        head["asset_sha256"] = _digest(root / head["asset_path"])
        with Image.open(root / row["base_asset_path"]) as base:
            preview = base.copy()
        preview.paste((79, 120, 159, 255), (4, 3, 8, 7))
        preview.save(root / row["asset_path"])
        row["asset_sha256"] = _digest(root / row["asset_path"])
        row["source_reference_sha256"] = row["asset_sha256"]
        row["preview_compositing"] = layered.BROWSER_COMPOSITING
        return row

    def validate_row(self, root, approval, row, compositor=True):
        neutral_path = root / approval["neutral_review"]["asset_path"]
        with Image.open(neutral_path) as im:
            neutral = im.copy()
        return layered.validate_presentation(
            root, approval, row, "happy", neutral_path, neutral, release,
            compositor=self.compositor if compositor else None,
        )

    def test_browser_rounding_is_exact_and_pil_mode_still_rejects_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, "a" * 12)
            row = self.browser_row(root, approval)
            result = self.validate_row(root, approval, row)
            self.assertEqual(result["compositing_validation"]["different_visible_pixels"], 0)
            self.assertEqual(result["compositing_validation"]["alpha_delta"], 0)
            self.assertEqual(result["compositing_validation"]["premultiplied_rgb_delta"], 0)
            row["preview_compositing"] = "pil_source_over"
            with self.assertRaisesRegex(ValueError, "flattened preview differs"):
                self.validate_row(root, approval, row)

    def test_browser_mode_keeps_hash_geometry_region_and_visible_pixel_gates(self):
        for fault in ("hash", "geometry", "preview", "alpha", "region", "unknown_mode", "no_context"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, "b" * 12)
                row = self.browser_row(root, approval)
                if fault == "hash":
                    (root / row["layers"][1]["asset_path"]).write_bytes(b"changed PNG")
                elif fault == "geometry":
                    row["layers"][1]["position"]["x"] = 10
                elif fault in ("preview", "alpha"):
                    path = root / row["asset_path"]
                    with Image.open(path) as im:
                        changed = im.copy()
                    changed.putpixel((4, 3), (50, 120, 159, 255) if fault == "preview" else (79, 120, 159, 254))
                    changed.save(path)
                    row["asset_sha256"] = _digest(path)
                elif fault == "region":
                    path = root / row["replacement_region"]["asset_path"]
                    region = Image.new("L", (12, 16))
                    region.putpixel((0, 0), 255)
                    region.save(path)
                    row["replacement_region"]["asset_sha256"] = _digest(path)
                elif fault == "unknown_mode":
                    row["preview_compositing"] = "approximately_equal"
                with self.assertRaises(ValueError):
                    self.validate_row(root, approval, row, compositor=fault != "no_context")

    def test_missing_context_does_not_silently_use_pil(self):
        compositor = layered.NativeBrowserCompositor()
        with self.assertRaisesRegex(ValueError, "open NativeBrowserCompositor"):
            compositor.compare((1, 1), [], Path("unused"), "unused")


class ApprovedCueInventoryTests(TestCase):
    @staticmethod
    def approve_inventory(root, approval, cues):
        cid = approval["character_id"]
        submission = root / "submission.json"
        submission.write_text(json.dumps({"round": "U4", "sealed": True}), encoding="utf-8")
        event = root / "approval.user.json"
        event.write_text(json.dumps({
            "review_status": "approved", "approved_by": "user",
            "submission": {"path": submission.name, "sha256": _digest(submission)},
            "characters": [{"character_id": cid, "approved_cues": cues}],
        }), encoding="utf-8")
        approval["approved_cue_inventory"] = {
            "cues": cues, "approval_event_path": event.name, "approval_event_sha256": _digest(event),
        }
        return event

    def test_explicit_user_inventory_allows_one_or_two_retained_cues(self):
        with patch.object(release, "EXPRESSION_STATES", ("neutral", "happy")):
            for count in (1, 2):
                with self.subTest(count=count), TemporaryDirectory() as directory:
                    root, cid = Path(directory), "c" * 12
                    approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, cid)
                    selected = dict(list(approval["approved_narrative_presentations"].items())[:count])
                    approval["approved_narrative_presentations"] = selected
                    self.approve_inventory(root, approval, list(selected))
                    fixtures.CharacterMediaReleaseTests._save_approval(root, cid, approval)
                    validated = layered.validate_character(root, {"character_id": cid}, release)
                    self.assertEqual(set(validated["performances"]), set(selected))

    def test_missing_tampered_or_mismatched_inventory_cannot_restore_or_remove_cues(self):
        for fault in ("missing", "extra_cue", "duplicate", "event_cues", "event_pending", "event_hash", "unsealed", "duplicate_character"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root, cid = Path(directory), "d" * 12
                approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, cid)
                cue = "friendly_greeting"
                selected = {cue: approval["approved_narrative_presentations"][cue]}
                event = self.approve_inventory(root, approval, [cue])
                inv = approval["approved_cue_inventory"]
                if fault == "missing":
                    del approval["approved_cue_inventory"]
                elif fault == "extra_cue":
                    selected["quiet_resolve"] = copy.deepcopy(selected[cue])
                elif fault == "duplicate":
                    inv["cues"] = [cue, cue]
                else:
                    e = json.loads(event.read_text(encoding="utf-8"))
                    if fault == "event_cues":
                        e["characters"][0]["approved_cues"] = ["quiet_resolve"]
                    elif fault == "event_pending":
                        e["review_status"] = "pending"
                    elif fault == "duplicate_character":
                        e["characters"].append(copy.deepcopy(e["characters"][0]))
                    elif fault == "unsealed":
                        submission = root / "submission.json"
                        submission.write_text(json.dumps({"sealed": False}), encoding="utf-8")
                        e["submission"]["sha256"] = _digest(submission)
                    else:
                        e["approved_by"] = "changed"
                    event.write_text(json.dumps(e), encoding="utf-8")
                    if fault != "event_hash":
                        inv["approval_event_sha256"] = _digest(event)
                with self.assertRaises(ValueError):
                    layered._approved_cues(root, approval, selected, cid, release)

    def test_explicit_approved_empty_inventory_does_not_reinstate_deleted_cues(self):
        with TemporaryDirectory() as directory:
            root, cid = Path(directory), "e" * 12
            approval = {"character_id": cid}
            self.approve_inventory(root, approval, [])
            layered._approved_cues(root, approval, {}, cid, release)

    def test_alternate_approval_path_preserves_historical_manifest_and_rejects_escape(self):
        with patch.object(release, "EXPRESSION_STATES", ("neutral", "happy")), TemporaryDirectory() as directory:
            root, cid = Path(directory), "f" * 12
            approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, cid)
            original = root / "characters" / cid / "APPROVALS.pending.json"
            old_bytes = original.read_bytes()
            alternate = root / "releases/U4/approvals" / (cid + ".json")
            alternate.parent.mkdir(parents=True)
            alternate.write_text(json.dumps(approval), encoding="utf-8")
            checked = layered.validate_character(root, {"character_id": cid}, release, approval_path=alternate)
            self.assertEqual(checked["approval_manifest_path"], alternate.resolve())
            self.assertEqual(original.read_bytes(), old_bytes)
            with self.assertRaisesRegex(ValueError, "inside the private art root"):
                layered.validate_character(root, {"character_id": cid}, release, approval_path=root.parent / "elsewhere.json")


class ReviewedUnderlayTests(TestCase):
    @staticmethod
    def prepare(root, role):
        cid = "8" * 12
        approval = fixtures.CharacterMediaReleaseTests._write_approved_character(root, cid)
        row = approval["approved_presentations"]["happy"]
        layer = copy.deepcopy(row["layers"][0])
        body_path = root / "characters" / cid / "cleared-or-pose-body.png"
        with Image.open(root / layer["asset_path"]) as image:
            body = image.copy()
        layer.update(role=role, asset_path=body_path.relative_to(root).as_posix())
        row["layers"][0] = layer
        return approval, row, body, body_path

    @staticmethod
    def write_body(root, row, body, path):
        body.save(path)
        row["layers"][0]["asset_sha256"] = _digest(path)
        preview = body.copy()
        head = row["layers"][1]
        with Image.open(root / head["asset_path"]) as image:
            preview.alpha_composite(image, (head["position"]["x"], head["position"]["y"]))
        # Actual composition starts with a clear canvas, so hidden RGB is absent.
        composed = Image.new("RGBA", preview.size)
        composed.alpha_composite(preview)
        composed.save(root / row["asset_path"])
        row["asset_sha256"] = _digest(root / row["asset_path"])
        row["source_reference_sha256"] = row["asset_sha256"]

    @staticmethod
    def validate(root, approval, row):
        neutral_path = root / approval["neutral_review"]["asset_path"]
        with Image.open(neutral_path) as image:
            neutral = image.copy()
        return layered.validate_presentation(root, approval, row, "happy", neutral_path, neutral, release)

    @staticmethod
    def bind_pose(root, approval, row):
        data_path = root / "reviews/U4/review-data.json"
        data_path.parent.mkdir(parents=True)
        relative = lambda p: os.path.relpath(root / p, data_path.parent).replace("\\", "/")
        item = {"key": "happy", "preview": relative(row["asset_path"]), "previewSha256": row["asset_sha256"],
                "layers": [{"url": relative(l["asset_path"]), "sha256": l["asset_sha256"],
                            "x": l["position"]["x"], "y": l["position"]["y"],
                            "width": l["dimensions"]["width"], "height": l["dimensions"]["height"]} for l in row["layers"]]}
        data_path.write_text(json.dumps({"round": "U4", "groups": [{"characterId": approval["character_id"], "items": [item]}]}), encoding="utf-8")
        submission_path = root / "submission.json"
        submission_path.write_text(json.dumps({"sealed": True, "round": "U4", "review_data": {
            "path": data_path.relative_to(root).as_posix(), "sha256": _digest(data_path),
        }}), encoding="utf-8")
        event_path = root / "approval.user.json"
        approved_item = {"key": "happy", "selected_for_runtime": True,
            "preview": {"path": row["asset_path"], "sha256": row["asset_sha256"]},
            "layers": [{"path": l["asset_path"], "sha256": l["asset_sha256"],
                        "position_xy": [l["position"]["x"], l["position"]["y"]],
                        "dimensions": [l["dimensions"]["width"], l["dimensions"]["height"]]} for l in row["layers"]]}
        event_path.write_text(json.dumps({"review_status": "approved", "approved_by": "user",
            "submission": {"path": submission_path.name, "sha256": _digest(submission_path)},
            "characters": [{"character_id": approval["character_id"], "approved_items": [approved_item]}]}), encoding="utf-8")
        row["review_item_key"] = "happy"
        row["approval_event"] = {"path": event_path.name, "sha256": _digest(event_path)}
        return event_path, submission_path, data_path

    def test_fractional_clearing_and_hidden_rgb_are_valid_without_repainting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            approval, row, body, path = self.prepare(root, "neutral_underlay")
            body.putpixel((4, 3), (80, 120, 160, 121))
            body.putpixel((0, 0), (123, 231, 78, 0))
            self.write_body(root, row, body, path)
            self.validate(root, approval, row)

    def test_ordinary_underlay_cannot_add_opacity_or_repaint_visible_body(self):
        for fault in ("alpha_increase", "rgb_change"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                approval, row, body, path = self.prepare(root, "neutral_underlay")
                if fault == "alpha_increase":
                    body.putpixel((0, 0), (0, 0, 0, 64))
                else:
                    body.putpixel((4, 3), (90, 120, 160, 255))
                self.write_body(root, row, body, path)
                with self.assertRaisesRegex(ValueError, "underlay may only reduce alpha"):
                    self.validate(root, approval, row)

    def test_fully_cleared_first_underlay_is_valid_but_empty_head_is_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            approval, row, body, path = self.prepare(root, "neutral_underlay")
            self.write_body(root, row, Image.new("RGBA", body.size), path)
            region_path = root / row["replacement_region"]["asset_path"]
            Image.new("L", body.size, 255).save(region_path)
            row["replacement_region"]["asset_sha256"] = _digest(region_path)
            self.validate(root, approval, row)
            head = row["layers"][1]
            Image.new("RGBA", (6, 6)).save(root / head["asset_path"])
            head["asset_sha256"] = _digest(root / head["asset_path"])
            with self.assertRaisesRegex(ValueError, "transparent background"):
                self.validate(root, approval, row)

    def test_exact_approved_pose_is_valid_and_stays_bound_to_event_and_sealed_item(self):
        for fault in (None, "missing_event", "unselected", "wrong_cid", "wrong_key", "wrong_event_geometry", "wrong_review_geometry", "unsealed", "changed_preview", "changed_layer"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                approval, row, body, path = self.prepare(root, "reviewed_pose_underlay")
                body.putpixel((4, 10), (90, 130, 180, 255))
                self.write_body(root, row, body, path)
                region_path = root / row["replacement_region"]["asset_path"]
                with Image.open(region_path) as image:
                    region = image.copy()
                region.putpixel((4, 10), 255)
                region.save(region_path)
                row["replacement_region"]["asset_sha256"] = _digest(region_path)
                ep, sp, dp = self.bind_pose(root, approval, row)
                event = json.loads(ep.read_text(encoding="utf-8"))
                if fault == "missing_event":
                    del row["approval_event"]
                elif fault == "unselected":
                    event["characters"][0]["approved_items"][0]["selected_for_runtime"] = False
                elif fault == "wrong_cid":
                    event["characters"][0]["character_id"] = "9" * 12
                elif fault == "wrong_key":
                    row["review_item_key"] = "angry"
                elif fault == "wrong_event_geometry":
                    event["characters"][0]["approved_items"][0]["layers"][0]["position_xy"] = [1, 0]
                elif fault in ("wrong_review_geometry", "unsealed"):
                    submission = json.loads(sp.read_text(encoding="utf-8"))
                    if fault == "wrong_review_geometry":
                        data = json.loads(dp.read_text(encoding="utf-8"))
                        data["groups"][0]["items"][0]["layers"][0]["x"] = 1
                        dp.write_text(json.dumps(data), encoding="utf-8")
                        submission["review_data"]["sha256"] = _digest(dp)
                    else:
                        submission["sealed"] = False
                    sp.write_text(json.dumps(submission), encoding="utf-8")
                    event["submission"]["sha256"] = _digest(sp)
                elif fault == "changed_preview":
                    preview_path = root / row["asset_path"]
                    with Image.open(preview_path) as image:
                        preview = image.copy()
                    preview.putpixel((4, 10), (91, 130, 180, 255))
                    preview.save(preview_path)
                    row["asset_sha256"] = _digest(preview_path)
                elif fault == "changed_layer":
                    body.putpixel((4, 10), (91, 130, 180, 255))
                    body.save(path)
                    row["layers"][0]["asset_sha256"] = _digest(path)
                if fault != "missing_event":
                    ep.write_text(json.dumps(event), encoding="utf-8")
                    row["approval_event"]["sha256"] = _digest(ep)
                if fault is None:
                    checked = self.validate(root, approval, row)
                    self.assertTrue(checked["reviewed_pose_binding"]["exact_approved_U4_item"])
                else:
                    with self.assertRaises(ValueError):
                        self.validate(root, approval, row)
