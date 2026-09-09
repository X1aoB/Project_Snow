"""Build the rights-gated v4 character media release.

The v4 package extends the independently deployed avatar bundle with one
18-state expression manifest per character.  Private review files are never
copied into the release.  The build fails closed unless every source,
individual layered approval and the separate 22-character rights decision
agree by SHA256. Rejected whole-character redraws remain ineligible.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:
    from scripts import character_layered_release as layered_release
    from scripts import verified_media_inputs as verified_inputs
    from scripts.build_avatar_media_release import (
        DEFAULT_ANALYST_SOURCE_ROOT,
        DEFAULT_OUTPUT_ROOT,
        DEFAULT_SOURCE_ROOT,
    )
    from scripts.build_avatar_media_release import (
        build_release as build_avatar_release,
    )
except ModuleNotFoundError:  # Direct ``python App/scripts/...`` execution.
    import character_layered_release as layered_release
    import verified_media_inputs as verified_inputs
    from build_avatar_media_release import (  # type: ignore[no-redef]
        DEFAULT_ANALYST_SOURCE_ROOT,
        DEFAULT_OUTPUT_ROOT,
        DEFAULT_SOURCE_ROOT,
    )
    from build_avatar_media_release import (  # type: ignore[no-redef]
        build_release as build_avatar_release,
    )


APP_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json"
DEFAULT_EXPRESSION_ROOT = APP_ROOT / "media" / "character_expressions" / "v0.9.7"
DEFAULT_RIGHTS_PATH = DEFAULT_EXPRESSION_ROOT / "RIGHTS.public.json"
DEFAULT_MIA_RUNTIME_ROOT = APP_ROOT / "public_frontend" / "assets" / "expressions" / "mia"
DEFAULT_VERSION = "2026.09.07.character.1"
MIA_CHARACTER_ID = "702f4375675b"
MIA_SOURCE_SHA256 = "f5898dfebbbe8fa10a1206c77342013001d4947311d27700ac999cafc2b49d30"
EXPRESSION_STATES = (
    "neutral",
    "gentle_smile",
    "happy",
    "amused",
    "teasing",
    "relieved",
    "serious",
    "focused",
    "thinking",
    "confused",
    "skeptical",
    "concerned",
    "surprised",
    "embarrassed",
    "sad",
    "disappointed",
    "annoyed",
    "angry",
)
FACE_OUTPUT_SIZE = 384
FACE_CONTENT_SIZE = 368
PUBLIC_EXPRESSION_STATUSES = {
    "public_runtime_enabled_by_explicit_operator_rights_waiver",
    "public_runtime_enabled_by_verified_source_rights",
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPRESSION_RUNTIME_SCHEMA = "project-snow-character-expression-runtime-3"
PRESENTATION_CONTRACT_SCHEMA = "project-snow-stage-presentation-2"
ACTIVE_RENDER_STRATEGY = "layered_sprite"
COMPLETE_SPRITE_SCOPE = "complete_character_sprite"


def _digest_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _private_file(root: Path, relative: object, label: str) -> Path:
    raw = str(relative or "").replace("\\", "/")
    relative_path = Path(raw)
    if (
        not raw
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() != raw
    ):
        raise ValueError(f"unsafe private path for {label}")
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"unsafe private path for {label}") from exc
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _hashed_private_file(
    root: Path,
    relative: object,
    expected_sha256: object,
    label: str,
) -> Path:
    path = _private_file(root, relative, label)
    expected = str(expected_sha256 or "").casefold()
    if not SHA256_PATTERN.fullmatch(expected) or _digest_path(path) != expected:
        raise ValueError(f"SHA256 mismatch for {label}")
    return path


def _registry() -> list[dict[str, Any]]:
    payload = _json_object(REGISTRY_PATH)
    characters = [
        item
        for item in payload.get("characters") or []
        if isinstance(item, dict) and item.get("selector_enabled")
    ]
    if len(characters) != 22:
        raise ValueError(f"expected exactly 22 selectable characters, found {len(characters)}")
    return characters


def _source_index(expression_root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    path = expression_root / "source-index.pending.json"
    manifest = _json_object(path)
    if manifest.get("schema_version") != "project-snow-character-expression-source-index-1":
        raise ValueError("expression source index schema mismatch")
    if manifest.get("publication_status") != "pending_not_public":
        raise ValueError("private expression sources must remain pending_not_public")
    rows = manifest.get("characters") if isinstance(manifest.get("characters"), list) else []
    by_id = {
        str(row.get("character_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("character_id")
    }
    expected_ids = {str(item["character_id"]) for item in _registry()}
    if len(rows) != 22 or set(by_id) != expected_ids:
        raise ValueError("expression source index must map the exact 22-character registry")
    for character_id, row in by_id.items():
        expected = str(row.get("source_sha256") or "").casefold()
        if not SHA256_PATTERN.fullmatch(expected):
            raise ValueError(f"invalid source SHA256: {character_id}")
        source = _hashed_private_file(
            expression_root,
            row.get("imported_path"),
            expected,
            f"source:{character_id}",
        )
        with Image.open(source) as image:
            if image.size != (2560, 1600) or image.mode != "RGB" or image.format != "PNG":
                raise ValueError(f"source contract mismatch: {character_id}")
    if by_id[MIA_CHARACTER_ID]["source_sha256"] != MIA_SOURCE_SHA256:
        raise ValueError("Mia source SHA256 no longer matches the approved source")
    return manifest, by_id, _digest_path(path)


def _rights_header(path: Path, source_index_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(
            "a separate public rights decision is required; pending review material cannot be packaged"
        )
    value = _json_object(path)
    if value.get("schema_version") != "project-snow-character-expression-rights-1":
        raise ValueError("expression rights schema mismatch")
    if value.get("source_index_sha256") != source_index_sha256:
        raise ValueError("expression rights decision does not match the source index")
    if int(value.get("character_count") or 0) != 22:
        raise ValueError("expression rights decision must cover 22 characters")
    if int(value.get("approved_expression_count") or 0) != 22 * len(EXPRESSION_STATES):
        raise ValueError("expression rights decision must cover all 396 approved states")
    status = str(value.get("publication_status") or "")
    rights = value.get("rights") if isinstance(value.get("rights"), dict) else {}
    if status not in PUBLIC_EXPRESSION_STATUSES:
        raise ValueError("expression rights are not approved for public runtime use")
    if status.endswith("explicit_operator_rights_waiver"):
        waiver = rights.get("waiver") if isinstance(rights.get("waiver"), dict) else {}
        if waiver.get("granted") is not True or not str(waiver.get("scope") or "").strip():
            raise ValueError("the public expression rights waiver is incomplete")
        if (
            rights.get("independent_verification") is not False
            or rights.get("verification_status") != "not_performed"
            or rights.get("public_use_authorized") is not True
        ):
            raise ValueError("a publication waiver must not claim independent rights verification")
    elif not (
        rights.get("independent_verification") is True and rights.get("verification_status") == "verified"
    ):
        raise ValueError("independent expression rights verification is incomplete")
    return value


def _publication_authorization(rights: dict[str, Any], package: Any) -> None:
    """A new user publication decision binds this exact reviewed private package."""
    block = rights.get("rights", {})
    authorization = block.get("publication_authorization", {})
    required = {
        "method",
        "source_thread_id",
        "source_turn_id",
        "recorded_at",
        "statement_sha256",
        "approval_event_sha256",
        "authorization_record_sha256",
        "private_package_manifest_sha256",
        "private_package_checksums_sha256",
    }
    if not isinstance(authorization, dict) or set(authorization) != required:
        raise ValueError("exact private package publication authorization is required")
    if authorization["method"] != "user_instruction":
        raise ValueError("publication authorization must identify the user's instruction")
    for field in ("source_thread_id", "source_turn_id"):
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(authorization[field])):
            raise ValueError("publication authorization source identity is invalid")
    try:
        stamp = datetime.fromisoformat(authorization["recorded_at"].replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("publication authorization timestamp is invalid") from exc
    for field in ("statement_sha256", "approval_event_sha256", "authorization_record_sha256"):
        if not SHA256_PATTERN.fullmatch(str(authorization[field])):
            raise ValueError("publication authorization hash is invalid")
    if (
        authorization["private_package_manifest_sha256"] != package.manifest_sha256
        or authorization["private_package_checksums_sha256"] != package.checksums_sha256
        or authorization["approval_event_sha256"] != package.manifest.get("approval_event_sha256")
    ):
        raise ValueError("publication authorization belongs to another private package")


def _private_expression_manifests(
    package: Any,
    rights: dict[str, Any],
    approved_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    manifest = package.manifest
    total = sum(len(row["performances"]) for row in approved_by_id.values())
    if (
        manifest.get("schema_version") != "project-snow-private-character-derivatives-1"
        or manifest.get("publication_status") != "pending_not_public"
        or manifest.get("public_runtime_eligible") is not False
        or manifest.get("production_deployed") is not False
        or manifest.get("character_count") != len(approved_by_id)
        or manifest.get("base_state_count") != len(approved_by_id) * len(EXPRESSION_STATES)
        or manifest.get("performance_count") != total
        or manifest.get("presentation_count") != len(approved_by_id) * len(EXPRESSION_STATES) + total
    ):
        raise ValueError("private expression package is not the complete approved delivery")
    _publication_authorization(rights, package)
    rows = manifest.get("characters", [])
    if not isinstance(rows, list) or len(rows) != len(approved_by_id):
        raise ValueError("private expression package character coverage differs")
    result = {}
    referenced = {"manifest.json"}
    for row in rows:
        cid = row.get("character_id")
        if cid not in approved_by_id or cid in result:
            raise ValueError("private expression package character identity differs")
        approved = approved_by_id[cid]
        path = f"expressions/{cid}/manifest.json"
        if row.get("manifest_path") != path or row.get("manifest_sha256") != package.files.get(path):
            raise ValueError("private expression manifest binding differs")
        runtime = verified_inputs.strict_object(package.read(path))
        _public_runtime_shape(runtime)
        referenced.add(path)
        if (
            runtime.get("schema_version") != EXPRESSION_RUNTIME_SCHEMA
            or runtime.get("character_id") != cid
            or runtime.get("publication_status") != "pending_not_public"
            or runtime.get("source", {}).get("approval_manifest_sha256")
            != approved["approval_manifest_sha256"]
            or runtime.get("stage_layout") != approved["stage_layout"]
            or set(runtime.get("expressions", {})) != set(approved["states"])
            or set(runtime.get("performances", {})) != set(approved["performances"])
            or set(runtime.get("presentations", {}))
            != (
                {f"expression.{state}" for state in approved["states"]}
                | {f"performance.{cue}" for cue in approved["performances"]}
            )
        ):
            raise ValueError("private runtime does not bind the exact character approval")
        for kind, records in (
            ("expressions", approved["states"]),
            ("performances", approved["performances"]),
        ):
            for key, record in records.items():
                entry = runtime[kind][key]
                expected_id = f"{'expression' if kind == 'expressions' else 'performance'}.{key}"
                if entry.get("presentation_id") != expected_id:
                    raise ValueError("private presentation identity differs")
                if any(
                    entry.get(field) != record[source]
                    for field, source in (
                        ("source_sha256", "sha256"),
                        ("approved_variant", "approved_variant"),
                        ("approved_round", "approved_round"),
                    )
                ):
                    raise ValueError("private presentation differs from the approved preview")
                presentation = runtime.get("presentations", {}).get(entry.get("presentation_id"), {})
                if (
                    presentation.get("canvas") != record["canvas"]
                    or presentation.get("stage_layout") != record["stage_layout"]
                ):
                    raise ValueError("private presentation canvas or layout differs from its approval")
                for prefix in ("face", "stage"):
                    asset_path = entry.get(prefix + "_asset_path", "")
                    if not asset_path.startswith(f"expressions/{cid}/") or package.files.get(
                        asset_path
                    ) != entry.get(prefix + "_asset_sha256"):
                        raise ValueError("private derivative path or checksum differs")
                    referenced.add(asset_path)
                expected_layers = [
                    {
                        "asset_path": (
                            f"expressions/{cid}/layers/{layer['role']}.{layer['asset_sha256'][:16]}.png"
                        ),
                        "asset_sha256": layer["asset_sha256"],
                        "dimensions": layer["dimensions"],
                        "position": layer["position"],
                        "composite": "source-over",
                    }
                    for layer in record["layers"]
                ]
                if presentation.get("layers") != expected_layers:
                    raise ValueError("private native layers differ from the approved bytes or positions")
                for layer in expected_layers:
                    if package.files.get(layer["asset_path"]) != layer["asset_sha256"]:
                        raise ValueError("private native layer checksum differs")
                    referenced.add(layer["asset_path"])
                if kind == "performances" and any(
                    entry.get(field) != record[field]
                    for field in ("label", "usage_context", "fallback_expression")
                ):
                    raise ValueError("private performance fallback or context differs")
        result[cid] = runtime
    if set(package.files) != referenced:
        raise ValueError("private expression package includes unrelated files")
    return result


def _runtime_static_metadata() -> dict[str, Any]:
    """Fixed public descriptions shared by the producer and private reuse gate."""
    return {
        "presentation_contract": {
            "schema_version": PRESENTATION_CONTRACT_SCHEMA,
            "default_render_strategy": ACTIVE_RENDER_STRATEGY,
            "active_render_strategies": ["single_sprite", "layered_sprite"],
            "reserved_render_strategies": ["rigged_2d"],
            "legacy_stage_asset_fallback_required": True,
            "native_pixel_composition": True,
            "atomic_surface_commit": True,
        },
        "performance_contract": {
            "schema_version": "project-snow-character-performance-1",
            "character_scoped": True,
            "base_expression_states_unchanged": True,
        },
        "transforms": {
            "face": "approved square crop contained in transparent 384x384 canvas",
            "stage": "approved flattened layer preview; contain_no_padding_height_1024",
            "layers": "approved_native_dimensions_without_resampling",
            "format": "original_native_png_layers_with_lossless_webp_derivatives",
            "body_completion": False,
            "micro_feature_mask_compositing": False,
            "partial_limb_compositing": False,
        },
    }


def _public_runtime_shape(runtime: dict[str, Any]) -> None:
    """Private metadata cannot become public merely because its package is pinned."""

    def exact(value: object, fields: set[str]) -> None:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("private runtime contains fields outside the public metadata contract")

    exact(
        runtime,
        {
            "schema_version",
            "character_id",
            "display_name",
            "expression_state_count",
            "performance_count",
            "publication_status",
            "rights",
            "source",
            "stage_layout",
            "presentation_contract",
            "performance_contract",
            "transforms",
            "expressions",
            "performances",
            "presentations",
        },
    )
    # Source and rights are replaced as complete objects by the new public decision.
    exact(runtime["stage_layout"], {"scale", "focus_x"})
    for key, expected in _runtime_static_metadata().items():
        if runtime[key] != expected:
            raise ValueError("private runtime contains values outside the public metadata contract")
    expression_fields = {
        "approved_variant",
        "approved_round",
        "source_sha256",
        "presentation_id",
        "face_asset_path",
        "face_asset_sha256",
        "stage_asset_path",
        "stage_asset_sha256",
        "dimensions",
        "asset_scope",
        "render_strategy",
        "stage_layout",
    }
    for kind in ("expressions", "performances"):
        if not isinstance(runtime[kind], dict):
            raise ValueError("private runtime entries must be objects")
        for entry in runtime[kind].values():
            exact(
                entry,
                expression_fields
                | ({"label", "usage_context", "fallback_expression"} if kind == "performances" else set()),
            )
            exact(entry["dimensions"], {"face", "stage"})
            for shape in entry["dimensions"].values():
                exact(shape, {"width", "height"})
            exact(entry["stage_layout"], {"scale", "focus_x"})
    if not isinstance(runtime["presentations"], dict):
        raise ValueError("private runtime presentations must be an object")
    for presentation in runtime["presentations"].values():
        exact(
            presentation,
            {
                "render_strategy",
                "asset_scope",
                "stage_asset_path",
                "stage_asset_sha256",
                "dimensions",
                "stage_layout",
                "canvas",
                "layers",
            },
        )
        exact(presentation["dimensions"], {"width", "height"})
        exact(presentation["canvas"], {"width", "height"})
        exact(presentation["stage_layout"], {"scale", "focus_x"})
        if not isinstance(presentation["layers"], list):
            raise ValueError("private runtime layers must be a list")
        for layer in presentation["layers"]:
            exact(layer, {"asset_path", "asset_sha256", "dimensions", "position", "composite"})
            exact(layer["dimensions"], {"width", "height"})
            exact(layer["position"], {"x", "y"})


def _rgba_array(path: Path, label: str) -> tuple[Image.Image, np.ndarray]:
    with Image.open(path) as candidate:
        if candidate.mode != "RGBA" or candidate.format != "PNG":
            raise ValueError(f"approved expression must be an RGBA PNG: {label}")
        image = candidate.copy()
    array = np.asarray(image)
    if not np.any(array[:, :, 3]) or not np.any(array[:, :, 3] == 0):
        raise ValueError(f"approved expression must contain a transparent background: {label}")
    return image, array


def _stage_layout(value: object, character_id: str) -> dict[str, float]:
    layout = value if isinstance(value, dict) else {}
    try:
        scale = float(layout.get("scale"))
        focus_x = float(layout.get("focus_x"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stage layout is incomplete: {character_id}") from exc
    if not 0.7 <= scale <= 1.4 or not 0 <= focus_x <= 100:
        raise ValueError(f"stage layout is outside the browser contract: {character_id}")
    return {"scale": scale, "focus_x": focus_x}


def _face_crop(
    value: object,
    image_size: tuple[int, int],
    label: str,
) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"face crop is missing: {label}")
    try:
        left, top, right, bottom = (int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"face crop is invalid: {label}") from exc
    if (
        left < 0
        or top < 0
        or right > image_size[0]
        or bottom > image_size[1]
        or right <= left
        or bottom <= top
        or right - left != bottom - top
    ):
        raise ValueError(f"face crop must be a square inside the approved source: {label}")
    return [left, top, right, bottom]


def _validate_character_approval(
    expression_root: Path,
    source_row: dict[str, Any],
    *,
    approval_path: Path | None = None,
    compositor: Any = None,
) -> dict[str, Any]:
    return layered_release.validate_character(
        expression_root,
        source_row,
        sys.modules[__name__],
        approval_path=approval_path,
        compositor=compositor,
    )


def _validated_mia_runtime(
    source_row: dict[str, Any],
    runtime_root: Path,
) -> dict[str, Any]:
    manifest_path = runtime_root / "manifest.json"
    manifest = _json_object(manifest_path)
    if manifest.get("character_id") != MIA_CHARACTER_ID:
        raise ValueError("bundled Mia runtime character mismatch")
    expressions = manifest.get("expressions") if isinstance(manifest.get("expressions"), dict) else {}
    if set(expressions) != set(EXPRESSION_STATES):
        raise ValueError("bundled Mia runtime does not contain the exact 18-state contract")
    approval_sha256 = str(
        (manifest.get("source_package") or {}).get("approval_snapshot_sha256")
        if isinstance(manifest.get("source_package"), dict)
        else ""
    ).casefold()
    if not SHA256_PATTERN.fullmatch(approval_sha256):
        raise ValueError("bundled Mia approval snapshot SHA256 is missing")
    states: dict[str, dict[str, Any]] = {}
    for state in EXPRESSION_STATES:
        row = expressions[state]
        if not isinstance(row, dict):
            raise ValueError(f"bundled Mia expression is invalid: {state}")
        face = runtime_root / Path(str(row.get("face_asset_path") or "")).name
        stage = runtime_root / Path(str(row.get("stage_asset_path") or "")).name
        if not face.is_file() or _digest_path(face) != row.get("face_asset_sha256"):
            raise ValueError(f"bundled Mia face asset hash mismatch: {state}")
        if not stage.is_file() or _digest_path(stage) != row.get("stage_asset_sha256"):
            raise ValueError(f"bundled Mia stage asset hash mismatch: {state}")
        with Image.open(face) as face_image, Image.open(stage) as stage_image:
            if face_image.size != (384, 384) or face_image.mode != "RGBA":
                raise ValueError(f"bundled Mia face asset contract mismatch: {state}")
            if stage_image.height != 1024 or stage_image.mode != "RGBA":
                raise ValueError(f"bundled Mia stage asset contract mismatch: {state}")
            stage_size = stage_image.size
        states[state] = {
            "face_path": face,
            "stage_path": stage,
            "source_sha256": str(row.get("source_sha256") or ""),
            "approved_variant": str(row.get("approved_variant") or "neutral"),
            "approved_round": int(row.get("approved_round") or 1),
            "stage_dimensions": {"width": stage_size[0], "height": stage_size[1]},
        }
    if str(source_row.get("source_sha256") or "") != MIA_SOURCE_SHA256:
        raise ValueError("Mia source cannot be reused because its source hash changed")
    return {
        "approval_manifest_path": manifest_path,
        "approval_manifest_sha256": approval_sha256,
        "stage_layout": {"scale": 1.0, "focus_x": 50.0},
        "states": states,
        "source_dimensions": {"width": 877, "height": 1449},
        "mia_runtime_reused": True,
    }


def _validate_rights_rows(
    rights_manifest: dict[str, Any],
    source_by_id: dict[str, dict[str, Any]],
    approved_by_id: dict[str, dict[str, Any]],
) -> None:
    rows = rights_manifest.get("characters") if isinstance(rights_manifest.get("characters"), list) else []
    by_id = {
        str(row.get("character_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("character_id")
    }
    if len(rows) != 22 or set(by_id) != set(source_by_id):
        raise ValueError("expression rights decision must list each character exactly once")
    for character_id, row in by_id.items():
        if row.get("source_sha256") != source_by_id[character_id].get("source_sha256"):
            raise ValueError(f"rights source hash mismatch: {character_id}")
        if row.get("approval_manifest_sha256") != approved_by_id[character_id].get(
            "approval_manifest_sha256"
        ):
            raise ValueError(f"rights approval hash mismatch: {character_id}")


def _webp_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    # In lossless mode these settings control compression effort, not pixels.
    # Avoid the near-lossless-size gain / very large build-time cost of method 6.
    image.save(output, format="WEBP", lossless=True, quality=80, method=4, exact=True)
    return output.getvalue()


def _write_hashed_asset(directory: Path, stem: str, content: bytes) -> tuple[str, str]:
    digest = _digest_bytes(content)
    name = f"{stem}.{digest[:16]}.webp"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    return name, digest


def _render_non_mia_assets(
    release_root: Path,
    character_id: str,
    approved: dict[str, Any],
    records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    expression_root = release_root / "expressions" / character_id
    face_root = expression_root / "faces"
    stage_root = expression_root / "stage"
    output: dict[str, dict[str, Any]] = {}
    for state, state_approval in (records if records is not None else approved["states"]).items():
        source_path = state_approval["path"]
        left, top, right, bottom = state_approval["face_crop_box_xyxy"]
        with Image.open(source_path) as candidate:
            image = candidate.copy()
        face_content = image.crop((left, top, right, bottom)).resize(
            (FACE_CONTENT_SIZE, FACE_CONTENT_SIZE),
            Image.Resampling.LANCZOS,
        )
        face_image = Image.new(
            "RGBA",
            (FACE_OUTPUT_SIZE, FACE_OUTPUT_SIZE),
            (0, 0, 0, 0),
        )
        face_inset = (FACE_OUTPUT_SIZE - FACE_CONTENT_SIZE) // 2
        face_image.alpha_composite(face_content, (face_inset, face_inset))
        stage_width = max(1, round(image.width * 1024 / image.height))
        stage_image = image.resize((stage_width, 1024), Image.Resampling.LANCZOS)
        face_name, face_sha256 = _write_hashed_asset(
            face_root,
            state,
            _webp_bytes(face_image),
        )
        stage_name, stage_sha256 = _write_hashed_asset(
            stage_root,
            f"{state}.stage",
            _webp_bytes(stage_image),
        )
        output[state] = {
            "face_asset_path": f"expressions/{character_id}/faces/{face_name}",
            "face_asset_sha256": face_sha256,
            "stage_asset_path": f"expressions/{character_id}/stage/{stage_name}",
            "stage_asset_sha256": stage_sha256,
            "dimensions": {
                "face": {"width": FACE_OUTPUT_SIZE, "height": FACE_OUTPUT_SIZE},
                "stage": {"width": stage_width, "height": 1024},
            },
            "asset_scope": layered_release.SCOPE,
            "render_strategy": ACTIVE_RENDER_STRATEGY,
            "stage_layout": dict(state_approval["stage_layout"]),
            "canvas": dict(state_approval["canvas"]),
            "layers": layered_release.render_layers(
                release_root, character_id, state_approval, sys.modules[__name__]
            ),
        }
    return output


def _copy_mia_assets(
    release_root: Path,
    approved: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    character_id = MIA_CHARACTER_ID
    expression_root = release_root / "expressions" / character_id
    face_root = expression_root / "faces"
    stage_root = expression_root / "stage"
    output: dict[str, dict[str, Any]] = {}
    for state in EXPRESSION_STATES:
        row = approved["states"][state]
        face_content = row["face_path"].read_bytes()
        stage_content = row["stage_path"].read_bytes()
        face_name, face_sha256 = _write_hashed_asset(face_root, state, face_content)
        stage_name, stage_sha256 = _write_hashed_asset(
            stage_root,
            f"{state}.stage",
            stage_content,
        )
        output[state] = {
            "face_asset_path": f"expressions/{character_id}/faces/{face_name}",
            "face_asset_sha256": face_sha256,
            "stage_asset_path": f"expressions/{character_id}/stage/{stage_name}",
            "stage_asset_sha256": stage_sha256,
            "dimensions": {
                "face": {"width": 384, "height": 384},
                "stage": dict(row["stage_dimensions"]),
            },
            "asset_scope": COMPLETE_SPRITE_SCOPE,
            "render_strategy": ACTIVE_RENDER_STRATEGY,
            "stage_layout": dict(approved["stage_layout"]),
        }
    return output


def _render_runtime_manifest(
    release_root: Path,
    character_id: str,
    display_name: str,
    approved: dict[str, Any],
    source: dict[str, Any],
    publication_status: str,
    rights: dict[str, Any],
) -> dict[str, Any]:
    records = dict(approved["states"])
    records.update({f"performance.{cue}": row for cue, row in approved["performances"].items()})
    assets = _render_non_mia_assets(release_root, character_id, approved, records)
    expressions, performances, presentations = {}, {}, {}
    for key, record in records.items():
        performance = key.startswith("performance.")
        presentation_id = key if performance else f"expression.{key}"
        asset = assets[key]
        presentations[presentation_id] = {
            "render_strategy": ACTIVE_RENDER_STRATEGY,
            "asset_scope": layered_release.SCOPE,
            "stage_asset_path": asset["stage_asset_path"],
            "stage_asset_sha256": asset["stage_asset_sha256"],
            "dimensions": dict(asset["dimensions"]["stage"]),
            "stage_layout": dict(asset["stage_layout"]),
            "canvas": asset["canvas"],
            "layers": asset["layers"],
        }
        item = {
            "approved_variant": record["approved_variant"],
            "approved_round": record["approved_round"],
            "source_sha256": record["sha256"],
            "presentation_id": presentation_id,
            **{name: value for name, value in asset.items() if name not in {"canvas", "layers"}},
        }
        if performance:
            item.update({name: record[name] for name in ("label", "usage_context", "fallback_expression")})
            performances[key.removeprefix("performance.")] = item
        else:
            expressions[key] = item
    return {
        "schema_version": EXPRESSION_RUNTIME_SCHEMA,
        "character_id": character_id,
        "display_name": display_name,
        "expression_state_count": len(EXPRESSION_STATES),
        "performance_count": len(performances),
        "publication_status": publication_status,
        "rights": rights,
        "source": source,
        "stage_layout": approved["stage_layout"],
        **_runtime_static_metadata(),
        "expressions": expressions,
        "performances": performances,
        "presentations": presentations,
    }


def build_release(
    *,
    avatar_source_root: Path,
    analyst_source_root: Path,
    expression_root: Path,
    rights_path: Path,
    mia_runtime_root: Path,
    output_root: Path,
    version: str,
    approval_root: Path | None = None,
    existing_avatar_release_root: Path | None = None,
    existing_avatar_manifest_sha256: str | None = None,
    existing_avatar_checksums_sha256: str | None = None,
    expression_package_root: Path | None = None,
    expression_package_manifest_sha256: str | None = None,
    expression_package_checksums_sha256: str | None = None,
    rights_sha256: str | None = None,
) -> Path:
    """Build an immutable v4 package after every private gate passes."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("media version must be one safe directory name")
    output_root = output_root.resolve()
    release_target = output_root / version
    if release_target.exists():
        raise FileExistsError(f"release already exists; choose an empty output root: {release_target}")
    if existing_avatar_release_root is None and any(
        (existing_avatar_manifest_sha256, existing_avatar_checksums_sha256)
    ):
        raise ValueError("avatar package hash pins require an existing avatar release")
    if expression_package_root is None and any(
        (expression_package_manifest_sha256, expression_package_checksums_sha256)
    ):
        raise ValueError("expression package hash pins require a private expression package")
    rights_path = verified_inputs.regular_path(rights_path)
    rights_digest = _digest_path(rights_path)
    if rights_sha256 is not None and rights_digest != rights_sha256:
        raise ValueError("rights decision SHA256 mismatch")
    if expression_package_root is not None and not SHA256_PATTERN.fullmatch(rights_sha256 or ""):
        raise ValueError("private expression reuse requires an explicit rights SHA256 pin")
    expression_root = expression_root.resolve()
    _, source_by_id, source_index_sha256 = _source_index(expression_root)
    rights_manifest = _rights_header(rights_path.resolve(), source_index_sha256)
    characters = _registry()
    avatar_package = None
    if existing_avatar_release_root is not None:
        avatar_package = verified_inputs.VerifiedPackage(
            existing_avatar_release_root, existing_avatar_manifest_sha256, existing_avatar_checksums_sha256
        )
        verified_inputs.validate_avatar(avatar_package, {str(c["character_id"]) for c in characters})
    approved_by_id: dict[str, dict[str, Any]] = {}
    if approval_root is not None:
        approval_root = approval_root.resolve()
        if not approval_root.is_relative_to(expression_root):
            raise ValueError("release approvals must stay inside the private expression root")
        expected_ids = {str(c["character_id"]) for c in characters}
        if {p.stem for p in approval_root.glob("*.json")} != expected_ids:
            raise ValueError("release approvals must cover the exact 22-character registry")
    context = layered_release.NativeBrowserCompositor() if approval_root else nullcontext(None)
    with context as compositor:
        for character in characters:
            character_id = str(character["character_id"])
            approved_by_id[character_id] = _validate_character_approval(
                expression_root,
                source_by_id[character_id],
                approval_path=approval_root / f"{character_id}.json" if approval_root else None,
                compositor=compositor,
            )
    _validate_rights_rows(rights_manifest, source_by_id, approved_by_id)
    performance_count = sum(len(row["performances"]) for row in approved_by_id.values())
    if rights_manifest.get("approved_performance_count") != performance_count:
        raise ValueError("expression rights decision must cover all approved character performances")
    expression_package = None
    reused_expressions = {}
    if expression_package_root is not None:
        expression_package = verified_inputs.VerifiedPackage(
            expression_package_root, expression_package_manifest_sha256, expression_package_checksums_sha256
        )
        reused_expressions = _private_expression_manifests(
            expression_package, rights_manifest, approved_by_id
        )

    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{version}.candidate.", dir=output_root) as temporary:
        temporary_root = Path(temporary)
        if avatar_package is not None:
            release_root = temporary_root / version
            manifest = verified_inputs.copy_avatar(avatar_package, release_root, version)
            manifest["generated_at"] = datetime.now(UTC).isoformat()
        else:
            release_root = build_avatar_release(
                source_root=avatar_source_root.resolve(),
                analyst_source_root=analyst_source_root.resolve(),
                output_root=temporary_root,
                version=version,
            )
            manifest = _json_object(release_root / "manifest.json")
        manifest_path = release_root / "manifest.json"
        if expression_package is not None:
            for path in expression_package.files:
                if path != "manifest.json" and not path.endswith("/manifest.json"):
                    expression_package.copy_file(path, release_root)
        rows = {
            str(row["character_id"]): row for row in manifest.get("characters") or [] if isinstance(row, dict)
        }
        rights_block = dict(rights_manifest.get("rights") or {})
        publication_status = str(rights_manifest["publication_status"])
        display_names = {
            str(character["character_id"]): str(character["display_name"]) for character in characters
        }
        for character_id in sorted(source_by_id):
            approved = approved_by_id[character_id]
            source = {
                "original_filename": source_by_id[character_id].get("original_filename"),
                "source_sha256": source_by_id[character_id]["source_sha256"],
                "source_index_sha256": source_index_sha256,
                "approval_manifest_sha256": approved["approval_manifest_sha256"],
            }
            if expression_package is not None:
                runtime_manifest = dict(reused_expressions[character_id])
                runtime_manifest.update(
                    {
                        "display_name": display_names[character_id],
                        "source": source,
                        "publication_status": publication_status,
                        "rights": rights_block,
                    }
                )
            else:
                runtime_manifest = _render_runtime_manifest(
                    release_root,
                    character_id,
                    display_names[character_id],
                    approved,
                    source,
                    publication_status,
                    rights_block,
                )
            runtime_manifest_path = release_root / "expressions" / character_id / "manifest.json"
            _write_json(runtime_manifest_path, runtime_manifest)
            row = rows[character_id]
            row["expression_manifest_path"] = f"expressions/{character_id}/manifest.json"
            row["expression_manifest_sha256"] = _digest_path(runtime_manifest_path)
            row["expression_state_count"] = len(EXPRESSION_STATES)
            row["performance_count"] = len(approved["performances"])
            row["expression_source_sha256"] = source_by_id[character_id]["source_sha256"]
            row["expression_source_index_sha256"] = source_index_sha256
            row["expression_approval_manifest_sha256"] = approved["approval_manifest_sha256"]

        manifest.update(
            {
                "schema_version": "project-snow-character-media-4",
                "release_basis": "verified_avatar_sources_plus_separately_approved_expression_derivatives",
                "expression_character_count": 22,
                "expression_state_count": 22 * len(EXPRESSION_STATES),
                "expression_asset_count": 22 * len(EXPRESSION_STATES) * 2,
                "performance_count": performance_count,
                "performance_asset_count": performance_count * 2,
                "expression_source_index_sha256": source_index_sha256,
                "expression_rights_manifest_sha256": rights_digest,
                "expression_rights_status": publication_status,
            }
        )
        comparisons = [
            record["compositing_validation"]
            for approved in approved_by_id.values()
            for record in [*approved["states"].values(), *approved["performances"].values()]
        ]
        manifest["expression_build_validation"] = {
            "approved_presentation_count": len(comparisons),
            "renderers": sorted({comparison["renderer"] for comparison in comparisons}),
            "browser_versions": sorted(
                {
                    comparison["browser_version"]
                    for comparison in comparisons
                    if comparison.get("browser_version")
                }
            ),
            "max_different_visible_pixels": max(
                comparison["different_visible_pixels"] for comparison in comparisons
            ),
        }
        if expression_package is not None:
            manifest["expression_source_package"] = {
                "schema_version": expression_package.manifest["schema_version"],
                "manifest_sha256": expression_package.manifest_sha256,
                "checksums_sha256": expression_package.checksums_sha256,
                "approval_event_sha256": expression_package.manifest["approval_event_sha256"],
                "reuse": "original_approved_png_and_reviewed_webp_bytes_preserved",
            }
        _write_json(manifest_path, manifest)
        checksums = [path for path in release_root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"]
        (release_root / "SHA256SUMS").write_text(
            "\n".join(
                f"{_digest_path(path)}  {path.relative_to(release_root).as_posix()}"
                for path in sorted(checksums, key=lambda value: value.as_posix())
            )
            + "\n",
            encoding="utf-8",
        )
        status = verified_inputs.PublicMediaCatalog(
            release_root, version, {str(c["character_id"]) for c in characters}, require_analyst=True
        ).verify(force=True)
        if status.get("status") != "ok":
            raise ValueError(f"combined character media verification failed: {status.get('errors')}")
        # Validate complete file coverage and immutable inputs once more before publication.
        verified_inputs.VerifiedPackage(
            release_root, _digest_path(manifest_path), _digest_path(release_root / "SHA256SUMS")
        )
        for package in (avatar_package, expression_package):
            if package is not None:
                package.verify()
        if _digest_path(rights_path) != rights_digest:
            raise ValueError("rights decision changed during media build")
        for approval in approved_by_id.values():
            if _digest_path(approval["approval_manifest_path"]) != approval["approval_manifest_sha256"]:
                raise ValueError("character approval changed during media build")
        if _digest_path(expression_root / "source-index.pending.json") != source_index_sha256:
            raise ValueError("expression source index changed during media build")
        if release_target.exists():
            raise FileExistsError("release appeared during build; immutable output was preserved")
        release_root.rename(release_target)
    return release_target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--avatar-source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--analyst-source-root",
        type=Path,
        default=DEFAULT_ANALYST_SOURCE_ROOT,
    )
    parser.add_argument("--expression-root", type=Path, default=DEFAULT_EXPRESSION_ROOT)
    parser.add_argument("--rights-path", type=Path, default=DEFAULT_RIGHTS_PATH)
    parser.add_argument(
        "--approval-root", type=Path, help="Versioned approved manifests; originals stay unchanged"
    )
    parser.add_argument("--existing-avatar-release-root", type=Path)
    parser.add_argument("--existing-avatar-manifest-sha256")
    parser.add_argument("--existing-avatar-checksums-sha256")
    parser.add_argument("--expression-package-root", type=Path)
    parser.add_argument("--expression-package-manifest-sha256")
    parser.add_argument("--expression-package-checksums-sha256")
    parser.add_argument("--rights-sha256")
    parser.add_argument("--mia-runtime-root", type=Path, default=DEFAULT_MIA_RUNTIME_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    arguments = parser.parse_args()
    release = build_release(
        avatar_source_root=arguments.avatar_source_root,
        analyst_source_root=arguments.analyst_source_root,
        expression_root=arguments.expression_root,
        rights_path=arguments.rights_path,
        mia_runtime_root=arguments.mia_runtime_root,
        output_root=arguments.output_root,
        version=str(arguments.version),
        approval_root=arguments.approval_root,
        existing_avatar_release_root=arguments.existing_avatar_release_root,
        existing_avatar_manifest_sha256=arguments.existing_avatar_manifest_sha256,
        existing_avatar_checksums_sha256=arguments.existing_avatar_checksums_sha256,
        expression_package_root=arguments.expression_package_root,
        expression_package_manifest_sha256=arguments.expression_package_manifest_sha256,
        expression_package_checksums_sha256=arguments.expression_package_checksums_sha256,
        rights_sha256=arguments.rights_sha256,
    )
    print(
        json.dumps(
            {"status": "ok", "media_version": arguments.version, "path": str(release)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
