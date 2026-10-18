from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts import character_webp_derivatives as derivatives
from scripts.character_layered_release import NativeBrowserCompositor


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root: Path):
    cid = "a" * 12
    source, release, review = (root / name for name in ("private-source", "release", "review"))
    for directory in (source, release, review):
        directory.mkdir()
    layer_directory = release / "expressions" / cid / "layers"
    layer_directory.mkdir(parents=True)
    base = Image.new("RGBA", (256, 256))
    base.paste((80, 120, 160, 255), (4, 4, 252, 256))
    head = Image.new("RGBA", (64, 64))
    head.paste((172, 100, 112, 255), (8, 8, 56, 56))
    states, presentations = {}, {}
    for key, images in (("neutral", [("base", base, 0, 0)]),
                        ("happy", [("base", base, 0, 0), ("head", head, 96, 48)])):
        layers, runtime_layers = [], []
        preview = Image.new("RGBA", base.size)
        for role, image, x, y in images:
            path = source / (role + ".png")
            if not path.exists():
                image.save(path)
            sha256 = digest(path)
            relative = f"expressions/{cid}/layers/{role}.{sha256[:16]}.png"
            (release / relative).write_bytes(path.read_bytes())
            geometry = {"dimensions": {"width": image.width, "height": image.height},
                        "position": {"x": x, "y": y}, "composite": "source-over"}
            layers.append({**geometry, "asset_sha256": sha256, "path": path, "role": role})
            runtime_layers.append({**geometry, "asset_sha256": sha256, "asset_path": relative})
            preview.alpha_composite(image, (x, y))
        preview_path = source / (key + ".preview.png")
        preview.save(preview_path)
        states[key] = {"path": preview_path, "sha256": digest(preview_path), "layers": layers,
                       "canvas": {"width": 256, "height": 256}, "stage_layout": {"scale": 1.0, "focus_x": 50},
                       "compositing_validation": {"different_visible_pixels": 0}}
        presentations["expression." + key] = {
            "render_strategy": "layered_sprite", "canvas": {"width": 256, "height": 256},
            "stage_layout": {"scale": 1.0, "focus_x": 50}, "layers": runtime_layers,
            "stage_asset_path": "unchanged-fallback.webp", "stage_asset_sha256": "b" * 64,
        }
    approved = {"states": states, "performances": {}}
    runtime = {"character_id": cid, "presentations": presentations, "source": {"approval_manifest_sha256": "c" * 64},
               "expressions": {"neutral": {"source_sha256": states["neutral"]["sha256"]}}, "performances": {}}
    return cid, source, release, review, approved, runtime


class QualityMetricTests(TestCase):
    def test_hidden_rgb_is_ignored_but_visible_and_alpha_changes_are_not(self):
        reference = np.zeros((12, 12, 4), dtype=np.uint8)
        delivery = reference.copy()
        delivery[:, :, :3] = 255
        self.assertTrue(derivatives._passes(derivatives.quality_metrics(reference, delivery)))
        reference[3:9, 3:9] = [80, 120, 160, 255]
        delivery = reference.copy()
        delivery[4, 4, 3] = 254
        metrics = derivatives.quality_metrics(reference, delivery)
        self.assertEqual(metrics["alpha_max_delta"], 1)
        self.assertFalse(derivatives._passes(metrics))
        delivery = reference.copy()
        delivery[3:9, 3:9, :3] = 0
        self.assertFalse(derivatives._passes(derivatives.quality_metrics(reference, delivery)))

    def test_p99_cannot_be_hidden_by_a_low_average_or_transparent_padding(self):
        reference = np.zeros((100, 100, 4), dtype=np.uint8)
        reference[40:60, 40:60] = [100, 100, 100, 255]
        delivery = reference.copy()
        delivery[40, 40:48, :3] = 120
        metrics = derivatives.quality_metrics(reference, delivery)
        self.assertLess(metrics["premultiplied_rgb_mae"], 2)
        self.assertGreater(metrics["premultiplied_rgb_p99"], 10)
        self.assertEqual(metrics["visible_pixels"], 400)
        self.assertFalse(derivatives._passes(metrics))

    def test_ssim_uses_structure_and_identical_tiny_images_are_well_defined(self):
        image = np.full((1, 1, 4), [10, 20, 30, 128], dtype=np.uint8)
        self.assertAlmostEqual(derivatives.quality_metrics(image, image.copy())["luminance_ssim"], 1, places=12)
        image = np.full((32, 32, 4), [100, 100, 100, 255], dtype=np.uint8)
        changed = image.copy()
        changed[::2, :, :3] = 120
        self.assertLess(derivatives.quality_metrics(image, changed)["luminance_ssim"], 0.99)
        boundary = {"visible_pixels": 1, "alpha_max_delta": 0,
                    "premultiplied_rgb_mae": 2.0, "premultiplied_rgb_p99": 10.0,
                    "luminance_ssim": 0.99}
        self.assertTrue(derivatives._passes(boundary))
        # Changing encoder effort must not admit the previously rejected SSIM
        # or relax any independent native-alpha / visible-color quality limit.
        for metric, rejected in (("alpha_max_delta", 1), ("premultiplied_rgb_mae", 2.000001),
                                 ("premultiplied_rgb_p99", 10.000001),
                                 ("luminance_ssim", 0.9896460086438131)):
            with self.subTest(metric=metric):
                self.assertFalse(derivatives._passes({**boundary, metric: rejected}))


class DeliveryDerivationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compositor = NativeBrowserCompositor()
        cls.compositor.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.compositor.__exit__(None, None, None)

    def derive(self, values, **kwargs):
        cid, _, release, review, approved, runtime = values
        return derivatives.derive_runtime_layers(release, cid, runtime, approved, review, self.compositor, **kwargs)

    def test_q92_native_delivery_preserves_sources_input_and_private_browser_evidence(self):
        with TemporaryDirectory() as directory:
            values = fixture(Path(directory))
            cid, source, release, review, approved, runtime = values
            old_runtime, old_approved = copy.deepcopy(runtime), copy.deepcopy(approved)
            originals = {path: path.read_bytes() for path in source.iterdir()}
            delivered, summary = self.derive(values)
            self.assertEqual(runtime, old_runtime)
            self.assertEqual(approved, old_approved)
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)
            self.assertTrue(summary["passed"])
            self.assertTrue(summary["technical_qa_only"])
            self.assertFalse(summary["human_approval"])
            self.assertEqual(summary["encoding"], {"format": "webp", "quality": 92,
                             "lossless": False, "method": 4, "alpha_quality": 100, "exact": True})
            self.assertEqual(summary["thresholds"], {"premultiplied_rgb_mae_max": 2.0,
                             "premultiplied_rgb_p99_max": 10.0, "luminance_ssim_min": 0.99,
                             "alpha_max_delta": 0})
            self.assertTrue(all(summary["engine_versions"].values()))
            self.assertNotIn(str(source), json.dumps(summary))
            report_path = review / cid / "report.json"
            self.assertEqual(digest(report_path), summary["report_sha256"])
            report = json.loads(report_path.read_text())
            self.assertEqual(report["encoding"], summary["encoding"])
            self.assertEqual(report["thresholds"], summary["thresholds"])
            for pid, presentation in delivered["presentations"].items():
                for old, new in zip(runtime["presentations"][pid]["layers"], presentation["layers"]):
                    self.assertEqual({k: v for k, v in new.items() if k not in {"asset_path", "asset_sha256"}},
                                     {k: v for k, v in old.items() if k not in {"asset_path", "asset_sha256"}})
                    self.assertTrue(new["asset_path"].endswith(".webp"))
                    self.assertEqual(digest(release / new["asset_path"]), new["asset_sha256"])
                    with Image.open(release / new["asset_path"]) as image:
                        self.assertEqual(image.size, (new["dimensions"]["width"], new["dimensions"]["height"]))
                comparison = report["presentations"][pid]
                self.assertEqual(set(comparison["artifacts"]), {"approved", "reference", "delivery", "difference"})
                self.assertEqual(comparison["renderer"], "browser_canvas_source_over")
                for artifact in comparison["artifacts"].values():
                    path = review / cid / artifact["path"]
                    self.assertEqual(digest(path), artifact["sha256"])
                    self.assertEqual(path.stat().st_size, artifact["bytes"])

    def test_rejected_alpha_geometry_source_hash_and_quality_keep_failure_reports(self):
        original_encode = derivatives._encode_webp
        for fault, code in (("alpha", "delivery_layer_alpha_changed"),
                            ("geometry", "native_layer_geometry_or_order_changed"),
                            ("source_hash", "asset_hash_changed"),
                            ("quality", "delivery_quality_gate_failed")):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                values = fixture(Path(directory))
                cid, _, release, review, _, runtime = values
                if fault == "geometry":
                    runtime["presentations"]["expression.happy"]["layers"][1]["position"] = {"x": 95, "y": 48}
                if fault == "source_hash":
                    path = release / runtime["presentations"]["expression.happy"]["layers"][1]["asset_path"]
                    path.write_bytes(path.read_bytes() + b"tampered")

                def bad_encode(image):
                    changed = image.copy()
                    if fault == "alpha":
                        changed.putpixel((5, 5), (*changed.getpixel((5, 5))[:3], 127))
                    elif fault == "quality":
                        pixels = np.array(changed)
                        pixels[:, :, :3] = 0
                        changed = Image.fromarray(pixels)
                    return original_encode(changed)

                with patch.object(derivatives, "_encode_webp", side_effect=bad_encode):
                    with self.assertRaises(derivatives.DerivativeValidationError) as caught:
                        self.derive(values)
                self.assertEqual(str(caught.exception), code)
                report = json.loads((review / cid / "report.json").read_text())
                self.assertFalse(report["passed"])
                self.assertFalse(report["human_approval"])
                self.assertEqual(report["failure"]["code"], code)
                if fault == "quality":
                    self.assertFalse(report["presentations"]["expression.neutral"]["passed"])
                    self.assertEqual(len(report["presentations"]["expression.neutral"]["artifacts"]), 4)
                elif fault in {"geometry", "source_hash"}:
                    self.assertTrue(report["presentations"]["expression.neutral"]["passed"])

    def test_cache_bytes_are_rehashed_and_completed_reviews_are_never_overwritten(self):
        with TemporaryDirectory() as directory:
            values = fixture(Path(directory))
            cid, _, _, review, _, _ = values
            cache = {}
            self.derive(values, cache=cache)
            original_report = (review / cid / "report.json").read_bytes()
            with self.assertRaises(FileExistsError):
                self.derive(values, cache=cache)
            self.assertEqual((review / cid / "report.json").read_bytes(), original_report)
            cached = next(iter(cache.values()))
            cached["payload"] += b"changed"
            second_review = Path(directory) / "second-review"
            second_review.mkdir()
            values = (*values[:3], second_review, *values[4:])
            with self.assertRaisesRegex(derivatives.DerivativeValidationError, "cached_derivative_hash_changed"):
                self.derive(values, cache=cache)
            self.assertFalse(json.loads((second_review / cid / "report.json").read_text())["passed"])

    def test_review_cannot_write_into_source_or_release_and_exact_gate_stays_separate(self):
        with TemporaryDirectory() as directory:
            values = fixture(Path(directory))
            cid, source, release, _, approved, runtime = values
            for forbidden in (source, release):
                before = set(forbidden.iterdir())
                with self.assertRaises(ValueError):
                    derivatives.derive_runtime_layers(release, cid, runtime, approved, forbidden, self.compositor)
                self.assertEqual(set(forbidden.iterdir()), before)
            self.assertTrue(hasattr(self.compositor, "compare"))
            neutral = approved["states"]["neutral"]
            exact = self.compositor.compare((256, 256), neutral["layers"], neutral["path"], neutral["sha256"])
            self.assertEqual(exact["alpha_delta"], 0)
            self.assertEqual(exact["premultiplied_rgb_delta"], 0)
