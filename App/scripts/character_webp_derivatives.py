"""Explicit q92 delivery derivatives; approved PNG sources remain immutable.

This post-process consumes an already strictly validated native release. Actual
Chromium source-over output must pass fixed engineering thresholds. Private
comparison artifacts are evidence for human review, never human approval.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import inspect
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

import numpy as np
import PIL
from PIL import Image, features

try:
    from scripts.character_layered_release import BROWSER_COMPOSITING, NativeBrowserCompositor
except ImportError:
    from character_layered_release import BROWSER_COMPOSITING, NativeBrowserCompositor


ENCODING = {"format": "webp", "quality": 92, "lossless": False, "method": 4,
            "alpha_quality": 100, "exact": True}
THRESHOLDS = {"premultiplied_rgb_mae_max": 2.0, "premultiplied_rgb_p99_max": 10.0,
              "luminance_ssim_min": 0.99, "alpha_max_delta": 0}
SCHEMA = "project-snow-character-webp-derivatives-1"
HASH = re.compile(r"[0-9a-f]{64}")
MAX_BYTES = 128 * 1024 * 1024


class DerivativeValidationError(ValueError):
    def __init__(self, code: str, report_path: Path) -> None:
        super().__init__(code)
        self.report_path = report_path


class _Rejected(ValueError):
    pass


def _need(condition: Any, code: str) -> None:
    if not condition:
        raise _Rejected(code)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode()


def _write_new(path: Path, raw: bytes, *, private: bool = True) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600 if private else 0o644)


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _checked_bytes(path: Path, expected: str) -> bytes:
    _need(isinstance(expected, str) and HASH.fullmatch(expected), "source_hash_invalid")
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and not _linked(path)
          and 0 < info.st_size <= MAX_BYTES, "asset_not_bounded_regular_file")
    raw = path.read_bytes()
    _need(len(raw) == info.st_size and _digest(raw) == expected, "asset_hash_changed")
    return raw


def _rgba(raw: bytes, size: tuple[int, int], expected_format: str) -> Image.Image:
    with Image.open(io.BytesIO(raw)) as source:
        _need(source.format == expected_format and source.size == size, "native_geometry_or_format_changed")
        _need(expected_format != "PNG" or source.mode == "RGBA", "native_source_not_rgba_png")
        source.load()
        return source.convert("RGBA")


def _size(value: Any) -> tuple[int, int]:
    _need(isinstance(value, dict) and set(value) == {"width", "height"}
          and all(type(value.get(key)) is int and 0 < value[key] <= 4096
                  for key in ("width", "height")), "native_geometry_invalid")
    return value["width"], value["height"]


def _release_path(root: Path, cid: str, value: Any, suffix: str) -> Path:
    _need(isinstance(value, str) and re.fullmatch(
        re.escape("expressions/" + cid + "/layers/") + r"[A-Za-z0-9_.-]+\." + suffix, value),
        "delivery_layer_path_invalid")
    parsed = PurePosixPath(value)
    _need(all(part not in {".", ".."} for part in parsed.parts), "delivery_layer_path_invalid")
    path = root.joinpath(*parsed.parts)
    for parent in (path, *path.parents):
        if parent == root:
            break
        _need(not _linked(parent), "delivery_layer_symlink")
    _need(path.resolve().is_relative_to(root), "delivery_layer_path_escape")
    return path


def _encode_webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    settings = {key: value for key, value in ENCODING.items() if key != "format"}
    if image.info.get("icc_profile"):
        settings["icc_profile"] = image.info["icc_profile"]
    image.save(buffer, format="WEBP", **settings)
    return buffer.getvalue()


def _gaussian(value: np.ndarray) -> np.ndarray:
    # Fixed 11x11 sigma-1.5 Gaussian SSIM window; no optional library/defaults.
    coordinates = np.arange(-5, 6, dtype=np.float64)
    kernel = np.exp(-(coordinates * coordinates) / (2 * 1.5**2))
    kernel /= kernel.sum()
    horizontal = np.pad(value, ((0, 0), (5, 5)), mode="reflect")
    result = sum(weight * horizontal[:, index:index + value.shape[1]]
                 for index, weight in enumerate(kernel))
    vertical = np.pad(result, ((5, 5), (0, 0)), mode="reflect")
    return sum(weight * vertical[index:index + value.shape[0], :]
               for index, weight in enumerate(kernel))


def quality_metrics(reference_rgba: np.ndarray, delivery_rgba: np.ndarray) -> dict[str, Any]:
    """Visible-union channel errors in 8-bit premultiplied RGB, masked SSIM.

    SSIM uses Rec.709 luminance, an 11x11 sigma-1.5 Gaussian, population moments,
    data_range=255, K1=.01 and K2=.03. Transparent padding contributes neither
    samples nor artificial similarity. Fully transparent hidden RGB is ignored.
    """
    _need(reference_rgba.dtype == delivery_rgba.dtype == np.uint8
          and reference_rgba.shape == delivery_rgba.shape
          and reference_rgba.ndim == 3 and reference_rgba.shape[2] == 4,
          "comparison_rgba_geometry_invalid")
    a, b = reference_rgba.astype(np.float64), delivery_rgba.astype(np.float64)
    visible = (a[:, :, 3] > 0) | (b[:, :, 3] > 0)
    alpha = int(np.max(np.abs(a[:, :, 3] - b[:, :, 3])))
    count = int(visible.sum())
    if not count:
        return {"visible_pixels": 0, "alpha_max_delta": alpha, "premultiplied_rgb_mae": 0.0,
                "premultiplied_rgb_p99": 0.0, "luminance_ssim": 1.0}
    x, y = a[:, :, :3] * a[:, :, 3:4] / 255, b[:, :, :3] * b[:, :, 3:4] / 255
    differences = np.abs(x - y)[visible]
    lx, ly = x @ np.array([0.2126, 0.7152, 0.0722]), y @ np.array([0.2126, 0.7152, 0.0722])
    mask = visible.astype(np.float64)
    weights = np.maximum(_gaussian(mask), np.finfo(np.float64).eps)
    mx, my = _gaussian(lx) / weights, _gaussian(ly) / weights
    vx = np.maximum(_gaussian(lx * lx) / weights - mx * mx, 0)
    vy = np.maximum(_gaussian(ly * ly) / weights - my * my, 0)
    covariance = _gaussian(lx * ly) / weights - mx * my
    c1, c2 = (0.01 * 255)**2, (0.03 * 255)**2
    ssim = ((2 * mx * my + c1) * (2 * covariance + c2)
            / ((mx * mx + my * my + c1) * (vx + vy + c2)))
    return {"visible_pixels": count, "alpha_max_delta": alpha,
            "premultiplied_rgb_mae": float(differences.mean()),
            "premultiplied_rgb_p99": float(np.percentile(differences, 99)),
            "luminance_ssim": float(np.clip(ssim[visible].mean(), -1, 1))}


def _passes(metrics: dict[str, Any]) -> bool:
    return (metrics["alpha_max_delta"] == THRESHOLDS["alpha_max_delta"]
            and metrics["premultiplied_rgb_mae"] <= THRESHOLDS["premultiplied_rgb_mae_max"]
            and metrics["premultiplied_rgb_p99"] <= THRESHOLDS["premultiplied_rgb_p99_max"]
            and metrics["luminance_ssim"] >= THRESHOLDS["luminance_ssim_min"])


def _heatmap(reference: np.ndarray, delivery: np.ndarray) -> bytes:
    a, b = reference.astype(np.float64), delivery.astype(np.float64)
    delta = np.max(np.abs(a[:, :, :3] * a[:, :, 3:4] / 255
                          - b[:, :, :3] * b[:, :, 3:4] / 255), axis=2)
    delta = np.maximum(delta, np.abs(a[:, :, 3] - b[:, :, 3]))
    visible = (a[:, :, 3] > 0) | (b[:, :, 3] > 0)
    image = np.zeros(reference.shape, dtype=np.uint8)
    image[:, :, 0] = np.clip(delta * 16, 0, 255).astype(np.uint8)
    image[:, :, 1] = np.clip(delta * 4, 0, 255).astype(np.uint8)
    image[:, :, 3] = visible.astype(np.uint8) * 255
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()


def derive_runtime_layers(
    release_root: Path, cid: str, runtime_manifest: dict[str, Any], approved: dict[str, Any],
    review_root: Path, compositor: NativeBrowserCompositor, cache: dict | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a new delivery manifest plus a public-safe provenance summary.

    The caller creates an exclusive private review root outside source/release
    trees and first performs the unchanged strict PNG approval validation. This
    function creates one exclusive character subdirectory, never deletes PNGs,
    and retains report.json plus all completed comparisons on failure.
    """
    release_root, review_root = Path(release_root), Path(review_root)
    _need(re.fullmatch(r"[0-9a-f]{12}", cid) is not None, "character_id_invalid")
    _need(release_root.is_dir() and not _linked(release_root)
          and review_root.is_dir() and not _linked(review_root), "existing_safe_roots_required")
    release_root, review_root = release_root.resolve(), review_root.resolve()
    _need(not review_root.is_relative_to(release_root) and not release_root.is_relative_to(review_root),
          "private_review_must_be_separate")
    # Validate the destination before creating anything: even a rejected review
    # must never leave a failure report inside immutable approval material.
    source_directories = set()
    for section in ("states", "performances"):
        for record in (approved.get(section) or {}).values():
            if isinstance(record, dict):
                for value in [record.get("path"), *(layer.get("path") for layer in record.get("layers", [])
                                                   if isinstance(layer, dict))]:
                    if value is not None:
                        source_directories.add(Path(value).resolve().parent)
    if approved.get("approval_manifest_path"):
        source_directories.add(Path(approved["approval_manifest_path"]).resolve().parent)
    _need(all(not review_root.is_relative_to(path) and not path.is_relative_to(review_root)
              for path in source_directories), "private_review_overlaps_source")
    review_directory = review_root / cid
    review_directory.mkdir(mode=0o700, exist_ok=False)
    report_path = review_directory / "report.json"
    versions = {"pillow": PIL.__version__, "libwebp": features.version("webp"), "chromium": None}
    report: dict[str, Any] = {
        "schema_version": SCHEMA, "character_id": cid, "encoding": dict(ENCODING),
        "thresholds": dict(THRESHOLDS), "engine_versions": versions, "passed": False,
        "technical_qa_only": True, "human_approval": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "layer_mapping": [], "presentations": {},
        "metric_definition": "visible_union_premultiplied_8bit_rgb_channels; masked_gaussian11_sigma1.5_rec709_ssim",
        "difference_amplification": {"red": 16, "green": 4},
        "tool_sha256": {"character_webp_derivatives.py": _digest(Path(__file__).read_bytes()),
                        "character_layered_release.py": _digest(Path(inspect.getfile(NativeBrowserCompositor)).read_bytes())},
    }
    result = copy.deepcopy(runtime_manifest)
    cache = {} if cache is None else cache
    source_bindings: dict[Path, str] = {}
    mappings: dict[str, dict[str, Any]] = {}
    public_presentations: dict[str, Any] = {}
    failure: Exception | None = None
    try:
        _need(isinstance(compositor, NativeBrowserCompositor), "actual_chromium_compositor_required")
        _need(versions["libwebp"] is not None, "webp_encoder_unavailable")
        _need(result.get("character_id") == cid, "runtime_character_mismatch")
        records = {"expression." + key: value for key, value in approved["states"].items()}
        records.update({"performance." + key: value for key, value in approved["performances"].items()})
        _need(set(result.get("presentations", {})) == set(records), "approved_presentation_set_changed")
        for presentation_id, record in records.items():
            _need(re.fullmatch(r"(?:expression|performance)\.[a-z][a-z0-9_]{0,79}", presentation_id),
                  "presentation_id_invalid")
            presentation = result["presentations"][presentation_id]
            canvas = _size(record["canvas"])
            _need(presentation.get("canvas") == record["canvas"]
                  and presentation.get("render_strategy") == "layered_sprite"
                  and presentation.get("stage_layout") == record["stage_layout"], "presentation_geometry_changed")
            validation = record.get("compositing_validation", {})
            _need(validation.get("different_visible_pixels") == 0
                  and all(validation.get(key, 0) == 0 for key in ("alpha_delta", "premultiplied_rgb_delta")),
                  "strict_source_validation_required")
            preview_path = Path(record["path"])
            preview_raw = _checked_bytes(preview_path, record["sha256"])
            _rgba(preview_raw, canvas, "PNG")
            source_bindings[preview_path] = record["sha256"]
            old_layers = presentation.get("layers")
            _need(isinstance(old_layers, list) and len(old_layers) == len(record["layers"])
                  and 1 <= len(old_layers) <= 8, "native_layer_count_changed")
            delivery_layers = []
            for old_layer, native_layer in zip(old_layers, record["layers"]):
                size = _size(native_layer["dimensions"])
                position = native_layer["position"]
                _need(isinstance(position, dict) and set(position) == {"x", "y"}
                      and all(type(position.get(key)) is int and position[key] >= 0 for key in ("x", "y"))
                      and position["x"] + size[0] <= canvas[0] and position["y"] + size[1] <= canvas[1]
                      and native_layer.get("composite") == "source-over"
                      and all(old_layer.get(key) == native_layer.get(key)
                              for key in ("dimensions", "position", "composite", "asset_sha256")),
                      "native_layer_geometry_or_order_changed")
                source_path = _release_path(release_root, cid, old_layer["asset_path"], "png")
                source_raw = _checked_bytes(source_path, native_layer["asset_sha256"])
                native_path = Path(native_layer["path"])
                _checked_bytes(native_path, native_layer["asset_sha256"])
                source_bindings[native_path] = native_layer["asset_sha256"]
                source_bindings[source_path] = native_layer["asset_sha256"]
                image = _rgba(source_raw, size, "PNG")
                cache_key = (native_layer["asset_sha256"], json.dumps(ENCODING, sort_keys=True),
                             versions["pillow"], versions["libwebp"])
                cached = cache.get(cache_key)
                if cached is None:
                    encoded = _encode_webp(image)
                    cached = {"source_sha256": native_layer["asset_sha256"], "payload": encoded,
                              "sha256": _digest(encoded)}
                    cache[cache_key] = cached
                _need(isinstance(cached, dict) and isinstance(cached.get("payload"), bytes)
                      and cached.get("source_sha256") == native_layer["asset_sha256"]
                      and _digest(cached["payload"]) == cached.get("sha256"), "cached_derivative_hash_changed")
                encoded = cached["payload"]
                decoded = _rgba(encoded, size, "WEBP")
                _need(np.array_equal(np.asarray(image)[:, :, 3], np.asarray(decoded)[:, :, 3]),
                      "delivery_layer_alpha_changed")
                sha256 = _digest(encoded)
                stem = PurePosixPath(old_layer["asset_path"]).stem.rsplit(".", 1)[0]
                delivery_relative = f"expressions/{cid}/layers/{stem}.{sha256[:16]}.webp"
                delivery_path = _release_path(release_root, cid, delivery_relative, "webp")
                if delivery_path.exists():
                    _checked_bytes(delivery_path, sha256)
                else:
                    _write_new(delivery_path, encoded, private=False)
                mapping = {"source_asset_path": old_layer["asset_path"],
                           "source_sha256": native_layer["asset_sha256"],
                           "delivery_asset_path": delivery_relative, "delivery_sha256": sha256,
                           "dimensions": dict(native_layer["dimensions"]),
                           "source_bytes": len(source_raw), "delivery_bytes": len(encoded)}
                if old_layer["asset_path"] in mappings:
                    _need(mappings[old_layer["asset_path"]] == mapping, "inconsistent_source_derivative_mapping")
                else:
                    mappings[old_layer["asset_path"]] = mapping
                    report["layer_mapping"].append(mapping)
                new_layer = {**old_layer, "asset_path": delivery_relative, "asset_sha256": sha256}
                delivery_layers.append({**new_layer, "path": delivery_path})
            rendered = compositor.rasterize_delivery(canvas, delivery_layers, preview_path, record["sha256"])
            _need(rendered.get("renderer") == BROWSER_COMPOSITING
                  and isinstance(rendered.get("browser_version"), str) and rendered["browser_version"],
                  "actual_browser_renderer_missing")
            _need(versions["chromium"] in (None, rendered["browser_version"]), "browser_version_changed")
            versions["chromium"] = rendered["browser_version"]
            reference = np.asarray(_rgba(rendered["reference_png"], canvas, "PNG"))
            delivery = np.asarray(_rgba(rendered["delivery_png"], canvas, "PNG"))
            metrics = quality_metrics(reference, delivery)
            name = presentation_id + "-" + _digest(presentation_id.encode())[:10]
            artifacts = {}
            for kind, raw in {"approved": preview_raw, "reference": rendered["reference_png"],
                              "delivery": rendered["delivery_png"], "difference": _heatmap(reference, delivery)}.items():
                filename = name + "." + kind + ".png"
                _write_new(review_directory / filename, raw)
                artifacts[kind] = {"path": filename, "sha256": _digest(raw), "bytes": len(raw)}
            passed = _passes(metrics)
            report["presentations"][presentation_id] = {"passed": passed, "metrics": metrics,
                                                       "artifacts": artifacts, "renderer": rendered["renderer"]}
            public_presentations[presentation_id] = {"passed": passed, "metrics": metrics}
            _need(passed, "delivery_quality_gate_failed")
            presentation["layers"] = [{key: value for key, value in layer.items() if key != "path"}
                                      for layer in delivery_layers]
        for path, digest in source_bindings.items():
            _checked_bytes(path, digest)
        for mapping in mappings.values():
            _checked_bytes(_release_path(release_root, cid, mapping["delivery_asset_path"], "webp"),
                           mapping["delivery_sha256"])
        report["passed"] = True
    except Exception as error:
        failure = error
        report["failure"] = {"type": type(error).__name__,
                             "code": str(error) if isinstance(error, _Rejected) else "derivation_failed"}
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if not report["passed"] and "failure" not in report:
            report["failure"] = {"type": "Interrupted", "code": "derivation_interrupted"}
        report_raw = _json(report)
        _write_new(report_path, report_raw)
    if failure is not None:
        raise DerivativeValidationError(report["failure"]["code"], report_path) from failure
    summary = {key: copy.deepcopy(report[key]) for key in (
        "schema_version", "character_id", "encoding", "thresholds", "engine_versions", "layer_mapping",
        "passed", "technical_qa_only", "human_approval")}
    summary.update(report_sha256=_digest(report_raw), presentations=public_presentations)
    return result, summary
