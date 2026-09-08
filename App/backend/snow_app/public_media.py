"""Versioned, separately deployed character media for the public surface."""

from __future__ import annotations

import json
import re
import threading
from zlib import crc32
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote

EXPRESSION_STATES = frozenset(
    {
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
    }
)
_PUBLIC_EXPRESSION_STATUSES = frozenset(
    {
        "public_runtime_enabled_by_explicit_operator_rights_waiver",
        "public_runtime_enabled_by_verified_source_rights",
    }
)
_EXPRESSION_RUNTIME_SCHEMAS = frozenset(
    {
        "project-snow-character-expression-runtime-1",
        "project-snow-character-expression-runtime-2",
        "project-snow-character-expression-runtime-3",
    }
)
_PRESENTATION_CONTRACT_SCHEMA = "project-snow-stage-presentation-1"
_ACTIVE_PRESENTATION_STRATEGIES = frozenset({"single_sprite", "layered_sprite"})


def _base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    output = ""
    while number:
        number, remainder = divmod(number, 36)
        output = alphabet[remainder] + output
    return output or "0"


def _sha1_evidence_matches(source_sha1: str, original_sha1: str) -> bool:
    source = str(source_sha1 or "").casefold().strip()
    original = str(original_sha1 or "").casefold().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", original):
        return False
    if re.fullmatch(r"[0-9a-f]{40}", source):
        return source == original
    if re.fullmatch(r"[0-9a-z]{1,31}", source):
        return source.lstrip("0") == _base36(int(original, 16)).lstrip("0")
    return False


def _webp_dimensions_and_alpha(path: Path) -> tuple[tuple[int, int], bool]:
    """Read the WebP canvas contract without adding Pillow to the public image."""

    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP container")
    cursor = 12
    alpha_chunk = False
    vp8_size: tuple[int, int] | None = None
    vp8x: tuple[tuple[int, int], bool] | None = None
    vp8l: tuple[tuple[int, int], bool] | None = None
    while cursor + 8 <= len(data):
        kind = data[cursor : cursor + 4]
        size = int.from_bytes(data[cursor + 4 : cursor + 8], "little")
        start = cursor + 8
        end = start + size
        if end > len(data):
            raise ValueError("truncated WebP chunk")
        payload = data[start:end]
        if kind == b"ALPH":
            alpha_chunk = True
        elif kind == b"VP8X" and len(payload) >= 10:
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
            vp8x = ((width, height), bool(payload[0] & 0x10))
        elif kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            vp8l = ((width, height), bool((bits >> 28) & 1))
        elif kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            vp8_size = (width, height)
        cursor = end + (size & 1)
    if vp8x is not None:
        return vp8x[0], vp8x[1] or alpha_chunk
    if vp8l is not None:
        return vp8l
    if vp8_size is not None:
        return vp8_size, alpha_chunk
    raise ValueError("WebP image payload is missing")


def _png_dimensions_and_alpha(path: Path) -> tuple[tuple[int, int], bool]:
    """Verify bounded native PNG chunks without a runtime imaging dependency.

    Native layers use non-interlaced RGBA8. RGB8 is recognized only so the
    caller can report a missing alpha channel; palette/grayscale/16-bit and
    animated PNGs are outside this release contract. CRCs validate the stored
    compressed bytes, while browser decoding remains the pixel-composition gate.
    """

    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("PNG container exceeds native layer budget")
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG container")
    cursor = 8
    dimensions = None
    has_alpha = False
    seen_palette = False
    idat_started = False
    idat_ended = False
    idat_bytes = 0
    while cursor + 12 <= len(data):
        length = int.from_bytes(data[cursor:cursor + 4], "big")
        kind = data[cursor + 4:cursor + 8]
        start, end = cursor + 8, cursor + 8 + length
        if length > 0x7FFFFFFF or end + 4 > len(data):
            raise ValueError("truncated PNG chunk")
        if not re.fullmatch(rb"[A-Za-z]{2}[A-Z][A-Za-z]", kind):
            raise ValueError("invalid PNG chunk type")
        if crc32(memoryview(data)[cursor + 4:end]) != int.from_bytes(data[end:end + 4], "big"):
            raise ValueError("PNG chunk CRC mismatch")
        if dimensions is None and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if idat_started and kind != b"IDAT":
            idat_ended = True
        if kind == b"IHDR":
            if dimensions is not None or length != 13:
                raise ValueError("invalid PNG IHDR")
            width = int.from_bytes(data[start:start + 4], "big")
            height = int.from_bytes(data[start + 4:start + 8], "big")
            depth, color, compression, filtering, interlace = data[start + 8:end]
            if (not 0 < width <= 4096 or not 0 < height <= 4096 or depth != 8
                    or color not in (2, 6) or (compression, filtering, interlace) != (0, 0, 0)):
                raise ValueError("unsupported native PNG header")
            dimensions, has_alpha = (width, height), color == 6
        elif kind == b"PLTE":
            if seen_palette or idat_started or length == 0 or length > 768 or length % 3:
                raise ValueError("invalid PNG palette")
            seen_palette = True
        elif kind == b"IDAT":
            if idat_ended:
                raise ValueError("PNG IDAT chunks must be consecutive")
            idat_started = True
            idat_bytes += length
        elif kind == b"IEND":
            if length or not idat_bytes or end + 4 != len(data):
                raise ValueError("invalid PNG end or image payload")
            return dimensions, has_alpha
        elif kind in (b"acTL", b"fcTL", b"fdAT") or not kind[0] & 32:
            raise ValueError("unsupported PNG chunk")
        cursor = end + 4
    raise ValueError("PNG IEND is missing or truncated")


class PublicMediaCatalog:
    """Read and verify the media package without coupling it to the app image."""

    def __init__(
        self,
        root: Path,
        version: str,
        expected_character_ids: Iterable[str],
        *,
        require_analyst: bool = False,
    ):
        self.root = Path(root)
        self.version = str(version)
        self.expected_character_ids = frozenset(str(value) for value in expected_character_ids)
        self.require_analyst = bool(require_analyst)
        self._lock = threading.RLock()
        self._cached_status: dict[str, Any] | None = None
        self._cached_manifest: dict[str, Any] | None = None

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def checksums_path(self) -> Path:
        return self.root / "SHA256SUMS"

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_asset_entry(
        self,
        identity: str,
        item: dict[str, Any],
        checksum_entries: dict[str, str],
        referenced_paths: set[str],
        errors: list[str],
    ) -> int:
        verified_files = 0
        for path_key, hash_key in (
            ("thumbnail_path", "thumbnail_sha256"),
            ("stage_path", "stage_sha256"),
        ):
            relative = str(item.get(path_key) or "")
            expected_hash = str(item.get(hash_key) or "")
            relative_path = Path(relative.replace("\\", "/"))
            candidate_path = (self.root / relative_path).resolve()
            try:
                candidate_path.relative_to(self.root.resolve())
            except ValueError:
                errors.append(f"unsafe_path:{identity}:{path_key}")
                continue
            normalized_relative = relative_path.as_posix()
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or normalized_relative != relative.replace("\\", "/")
            ):
                errors.append(f"unsafe_path:{identity}:{path_key}")
                continue
            referenced_paths.add(normalized_relative)
            if not candidate_path.is_file():
                errors.append(f"file_missing:{identity}:{path_key}")
                continue
            checksum_hash = checksum_entries.get(normalized_relative)
            if not checksum_hash:
                errors.append(f"checksum_missing:{identity}:{path_key}")
                continue
            actual_hash = self._file_hash(candidate_path)
            if len(expected_hash) != 64 or actual_hash != expected_hash.lower():
                errors.append(f"hash_mismatch:{identity}:{path_key}")
                continue
            if checksum_hash != expected_hash.lower() or checksum_hash != actual_hash:
                errors.append(f"checksum_mismatch:{identity}:{path_key}")
                continue
            verified_files += 1
        return verified_files

    def _verified_release_file(
        self,
        *,
        identity: str,
        path_key: str,
        relative: str,
        expected_hash: str,
        checksum_entries: dict[str, str],
        referenced_paths: set[str],
        errors: list[str],
    ) -> Path | None:
        relative_path = Path(relative.replace("\\", "/"))
        candidate_path = (self.root / relative_path).resolve()
        try:
            candidate_path.relative_to(self.root.resolve())
        except ValueError:
            errors.append(f"unsafe_path:{identity}:{path_key}")
            return None
        normalized_relative = relative_path.as_posix()
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or normalized_relative != relative.replace("\\", "/")
        ):
            errors.append(f"unsafe_path:{identity}:{path_key}")
            return None
        referenced_paths.add(normalized_relative)
        if not candidate_path.is_file():
            errors.append(f"file_missing:{identity}:{path_key}")
            return None
        checksum_hash = checksum_entries.get(normalized_relative)
        if not checksum_hash:
            errors.append(f"checksum_missing:{identity}:{path_key}")
            return None
        actual_hash = self._file_hash(candidate_path)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) or actual_hash != expected_hash.lower():
            errors.append(f"hash_mismatch:{identity}:{path_key}")
            return None
        if checksum_hash != expected_hash.lower() or checksum_hash != actual_hash:
            errors.append(f"checksum_mismatch:{identity}:{path_key}")
            return None
        return candidate_path

    @staticmethod
    def _expression_rights_are_public(manifest: dict[str, Any]) -> bool:
        status = str(manifest.get("publication_status") or "")
        if status not in _PUBLIC_EXPRESSION_STATUSES:
            return False
        rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
        if status.endswith("explicit_operator_rights_waiver"):
            waiver = rights.get("waiver") if isinstance(rights.get("waiver"), dict) else {}
            return waiver.get("granted") is True and bool(str(waiver.get("scope") or "").strip())
        return (
            rights.get("independent_verification") is True
            and str(rights.get("verification_status") or "") == "verified"
        )

    def _verify_presentation_layers(
        self,
        character_id: str,
        state: str,
        presentation: dict[str, Any],
        checksum_entries: dict[str, str],
        referenced_paths: set[str],
        errors: list[str],
    ) -> tuple[int, int]:
        identity = f"{character_id}:{state}"
        canvas = presentation.get("canvas")

        def valid_size(size: Any) -> bool:
            return isinstance(size, dict) and all(
                type(size.get(key)) is int and 0 < size[key] <= 4096
                for key in ("width", "height")
            )

        layers = presentation.get("layers")
        if not valid_size(canvas) or not isinstance(layers, list) or not 1 <= len(layers) <= 8:
            errors.append(f"expression_layers_invalid:{identity}")
            return 0, 0
        verified = 0
        pixels = 0
        for index, layer in enumerate(layers):
            layer_id = f"{identity}:{index}"
            if not isinstance(layer, dict):
                errors.append(f"expression_layer_invalid:{layer_id}")
                continue
            size, position = layer.get("dimensions"), layer.get("position")
            geometry_ok = (
                valid_size(size) and isinstance(position, dict)
                and all(type(position.get(key)) is int and position[key] >= 0 for key in ("x", "y"))
                and position["x"] + size["width"] <= canvas["width"]
                and position["y"] + size["height"] <= canvas["height"]
                and layer.get("composite") == "source-over"
            )
            if not geometry_ok:
                errors.append(f"expression_layer_geometry_invalid:{layer_id}")
            else:
                pixels += size["width"] * size["height"]
            relative = str(layer.get("asset_path") or "")
            expected_hash = str(layer.get("asset_sha256") or "")
            if (
                not relative.startswith(f"expressions/{character_id}/layers/")
                or not re.fullmatch(
                    rf"[a-zA-Z0-9_-]+\.{re.escape(expected_hash[:16])}\.(?:webp|png)",
                    Path(relative).name,
                )
            ):
                errors.append(f"expression_layer_path_invalid:{layer_id}")
            candidate = self._verified_release_file(
                identity=layer_id, path_key="layer_asset_path", relative=relative,
                expected_hash=expected_hash, checksum_entries=checksum_entries,
                referenced_paths=referenced_paths, errors=errors,
            )
            if candidate is None:
                continue
            verified += 1
            try:
                reader = _png_dimensions_and_alpha if candidate.suffix == ".png" else _webp_dimensions_and_alpha
                actual_size, has_alpha = reader(candidate)
                if not has_alpha:
                    errors.append(f"expression_layer_alpha_missing:{layer_id}")
                if not valid_size(size) or actual_size != (size["width"], size["height"]):
                    errors.append(f"expression_layer_dimensions_mismatch:{layer_id}")
            except (OSError, TypeError, ValueError):
                errors.append(f"expression_layer_unreadable:{layer_id}")
        if pixels > canvas["width"] * canvas["height"] * 4:
            errors.append(f"expression_layer_pixel_budget_exceeded:{identity}")
        return verified, len(layers)

    def _verify_expression_entry(
        self,
        character_id: str,
        item: dict[str, Any],
        checksum_entries: dict[str, str],
        referenced_paths: set[str],
        errors: list[str],
    ) -> tuple[int, int]:
        relative = str(item.get("expression_manifest_path") or "")
        expected_hash = str(item.get("expression_manifest_sha256") or "")
        expected_manifest_relative = f"expressions/{character_id}/manifest.json"
        if relative != expected_manifest_relative:
            errors.append(f"expression_manifest_path_invalid:{character_id}")
        if int(item.get("expression_state_count") or 0) != len(EXPRESSION_STATES):
            errors.append(f"expression_state_count_invalid:{character_id}")
        if not relative or not expected_hash:
            errors.append(f"expression_manifest_missing:{character_id}")
            return 0, 1 + len(EXPRESSION_STATES) * 2
        manifest_path = self._verified_release_file(
            identity=character_id,
            path_key="expression_manifest_path",
            relative=relative,
            expected_hash=expected_hash,
            checksum_entries=checksum_entries,
            referenced_paths=referenced_paths,
            errors=errors,
        )
        if manifest_path is None:
            return 0, 1 + len(EXPRESSION_STATES) * 2
        verified_files = 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"expression_manifest_invalid:{character_id}")
            return verified_files, 1 + len(EXPRESSION_STATES) * 2
        if not isinstance(manifest, dict):
            errors.append(f"expression_manifest_invalid:{character_id}")
            return verified_files, 1 + len(EXPRESSION_STATES) * 2
        expression_schema = str(manifest.get("schema_version") or "")
        if expression_schema not in _EXPRESSION_RUNTIME_SCHEMAS:
            errors.append(f"expression_schema_invalid:{character_id}")
        if str(manifest.get("character_id") or "") != character_id:
            errors.append(f"expression_character_mismatch:{character_id}")
        if not self._expression_rights_are_public(manifest):
            errors.append(f"expression_rights_unverified:{character_id}")
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        for nested_key, row_key in (
            ("source_sha256", "expression_source_sha256"),
            ("source_index_sha256", "expression_source_index_sha256"),
            ("approval_manifest_sha256", "expression_approval_manifest_sha256"),
        ):
            nested_hash = str(source.get(nested_key) or "").casefold()
            row_hash = str(item.get(row_key) or "").casefold()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", nested_hash)
                or nested_hash != row_hash
            ):
                errors.append(f"expression_provenance_mismatch:{character_id}:{nested_key}")
        layout = manifest.get("stage_layout") if isinstance(manifest.get("stage_layout"), dict) else {}
        try:
            layout_scale = float(layout.get("scale"))
            layout_focus_x = float(layout.get("focus_x"))
        except (TypeError, ValueError):
            layout_scale = layout_focus_x = -1
        if not 0.7 <= layout_scale <= 1.4 or not 0 <= layout_focus_x <= 100:
            errors.append(f"expression_stage_layout_invalid:{character_id}")
        expressions = (
            manifest.get("expressions")
            if isinstance(manifest.get("expressions"), dict)
            else {}
        )
        if set(expressions) != EXPRESSION_STATES or int(
            manifest.get("expression_state_count") or 0
        ) != len(EXPRESSION_STATES):
            errors.append(f"expression_states_invalid:{character_id}")
        presentations: dict[str, Any] = {}
        referenced_presentations: set[str] = set()
        expected_files = 1 + len(EXPRESSION_STATES) * 2
        layered_schema = expression_schema == "project-snow-character-expression-runtime-3"
        has_presentations = layered_schema or expression_schema == "project-snow-character-expression-runtime-2"
        if has_presentations:
            contract = (
                manifest.get("presentation_contract")
                if isinstance(manifest.get("presentation_contract"), dict)
                else {}
            )
            if (
                contract.get("schema_version") != (
                    "project-snow-stage-presentation-2" if layered_schema else _PRESENTATION_CONTRACT_SCHEMA
                )
                or contract.get("default_render_strategy") != ("layered_sprite" if layered_schema else "single_sprite")
                or contract.get("active_render_strategies") != (
                    ["single_sprite", "layered_sprite"] if layered_schema else ["single_sprite"]
                )
                or contract.get("legacy_stage_asset_fallback_required") is not True
                or (layered_schema and (
                    contract.get("native_pixel_composition") is not True
                    or contract.get("atomic_surface_commit") is not True
                ))
            ):
                errors.append(f"expression_presentation_contract_invalid:{character_id}")
            presentations = (
                manifest.get("presentations")
                if isinstance(manifest.get("presentations"), dict)
                else {}
            )
            if not presentations:
                errors.append(f"expression_presentations_missing:{character_id}")
        performance_rows = manifest.get("performances", {})
        performance_contract = manifest.get("performance_contract")
        has_performances = performance_contract is not None or bool(performance_rows) or bool(item.get("performance_count"))
        if has_performances:
            if (not layered_schema or not isinstance(performance_contract, dict)
                    or performance_contract.get("schema_version") != "project-snow-character-performance-1"
                    or performance_contract.get("character_scoped") is not True
                    or performance_contract.get("base_expression_states_unchanged") is not True
                    or not isinstance(performance_rows, dict) or len(performance_rows) > 6):
                errors.append(f"expression_performance_contract_invalid:{character_id}")
            if not isinstance(performance_rows, dict):
                performance_rows = {}
            if (manifest.get("performance_count") != len(performance_rows)
                    or item.get("performance_count") != len(performance_rows)):
                errors.append(f"expression_performance_count_invalid:{character_id}")
            for cue, performance in performance_rows.items():
                if (not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", cue)
                        or not isinstance(performance, dict)
                        or performance.get("presentation_id") != f"performance.{cue}"
                        or performance.get("fallback_expression") not in EXPRESSION_STATES
                        or not str(performance.get("label") or "").strip()
                        or not str(performance.get("usage_context") or "").strip()):
                    errors.append(f"expression_performance_invalid:{character_id}:{cue}")
            expected_files += len(performance_rows) * 2
        else:
            performance_rows = {}
        asset_rows = {state: expressions.get(state) for state in sorted(EXPRESSION_STATES)}
        asset_rows.update({f"performance.{cue}": row for cue, row in performance_rows.items()})
        for state, expression in asset_rows.items():
            if not isinstance(expression, dict):
                continue
            dimensions = (
                expression.get("dimensions")
                if isinstance(expression.get("dimensions"), dict)
                else {}
            )
            face_dimensions = dimensions.get("face") if isinstance(dimensions.get("face"), dict) else {}
            stage_dimensions = dimensions.get("stage") if isinstance(dimensions.get("stage"), dict) else {}
            if face_dimensions != {"width": 384, "height": 384}:
                errors.append(f"expression_face_dimensions_invalid:{character_id}:{state}")
            try:
                stage_width = int(stage_dimensions.get("width") or 0)
                stage_height = int(stage_dimensions.get("height") or 0)
            except (TypeError, ValueError):
                stage_width = stage_height = 0
            if stage_height != 1024 or not 0 < stage_width <= 4096:
                errors.append(f"expression_stage_dimensions_invalid:{character_id}:{state}")
            if has_presentations:
                presentation_id = str(expression.get("presentation_id") or "")
                if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,95}", presentation_id):
                    errors.append(f"expression_presentation_id_invalid:{character_id}:{state}")
                else:
                    referenced_presentations.add(presentation_id)
                presentation = presentations.get(presentation_id)
                if not isinstance(presentation, dict):
                    errors.append(f"expression_presentation_missing:{character_id}:{state}")
                else:
                    strategy = str(presentation.get("render_strategy") or "")
                    scope = "layered_character_presentation" if strategy == "layered_sprite" else "complete_character_sprite"
                    if (
                        strategy not in _ACTIVE_PRESENTATION_STRATEGIES
                        or (strategy == "layered_sprite" and not layered_schema)
                        or expression.get("render_strategy") != strategy
                        or expression.get("asset_scope") != scope
                        or presentation.get("asset_scope") != scope
                        or presentation.get("stage_asset_path")
                        != expression.get("stage_asset_path")
                        or presentation.get("stage_asset_sha256")
                        != expression.get("stage_asset_sha256")
                        or presentation.get("dimensions") != stage_dimensions
                    ):
                        errors.append(
                            f"expression_presentation_mismatch:{character_id}:{state}"
                        )
                    if strategy == "layered_sprite":
                        layer_verified, layer_expected = self._verify_presentation_layers(
                            character_id, state, presentation, checksum_entries, referenced_paths, errors,
                        )
                        verified_files += layer_verified
                        expected_files += layer_expected
                    presentation_layout = (
                        presentation.get("stage_layout")
                        if isinstance(presentation.get("stage_layout"), dict)
                        else {}
                    )
                    try:
                        presentation_scale = float(presentation_layout.get("scale"))
                        presentation_focus_x = float(presentation_layout.get("focus_x"))
                    except (TypeError, ValueError):
                        presentation_scale = presentation_focus_x = -1
                    if not (
                        0.7 <= presentation_scale <= 1.4
                        and 0 <= presentation_focus_x <= 100
                    ):
                        errors.append(
                            f"expression_presentation_layout_invalid:{character_id}:{state}"
                        )
            for path_key, hash_key in (
                ("face_asset_path", "face_asset_sha256"),
                ("stage_asset_path", "stage_asset_sha256"),
            ):
                relative_asset = str(expression.get(path_key) or "")
                expected_asset_hash = str(expression.get(hash_key) or "")
                expected_directory = (
                    f"expressions/{character_id}/faces/"
                    if path_key == "face_asset_path"
                    else f"expressions/{character_id}/stage/"
                )
                if (
                    not relative_asset.startswith(expected_directory)
                    or expected_asset_hash[:16] not in Path(relative_asset).name
                ):
                    errors.append(
                        f"expression_asset_path_invalid:{character_id}:{state}:{path_key}"
                    )
                candidate = self._verified_release_file(
                    identity=f"{character_id}:{state}",
                    path_key=path_key,
                    relative=relative_asset,
                    expected_hash=expected_asset_hash,
                    checksum_entries=checksum_entries,
                    referenced_paths=referenced_paths,
                    errors=errors,
                )
                if candidate is not None:
                    verified_files += 1
                    declared_dimensions = (
                        face_dimensions if path_key == "face_asset_path" else stage_dimensions
                    )
                    try:
                        declared_size = (
                            int(declared_dimensions.get("width") or 0),
                            int(declared_dimensions.get("height") or 0),
                        )
                        actual_size, has_alpha = _webp_dimensions_and_alpha(candidate)
                        if not has_alpha:
                            errors.append(
                                f"expression_asset_format_invalid:{character_id}:{state}:{path_key}"
                            )
                        if actual_size != declared_size:
                            errors.append(
                                f"expression_asset_dimensions_mismatch:{character_id}:{state}:{path_key}"
                            )
                    except (OSError, TypeError, ValueError):
                        errors.append(
                            f"expression_asset_unreadable:{character_id}:{state}:{path_key}"
                        )
        if has_presentations and (
            set(presentations) != referenced_presentations
        ):
            errors.append(f"expression_presentations_unreferenced:{character_id}")
        return verified_files, expected_files

    @staticmethod
    def _license_is_verified(item: dict[str, Any]) -> bool:
        """Require fixed Wiki and license evidence for every public portrait."""
        status = str(item.get("license_status") or "").casefold().strip()
        if status not in {
            "verified",
            "verified_explicit",
            "verified_site_policy_no_page_exception",
        }:
            return False
        license_name = str(item.get("license") or "").casefold()
        license_version = str(item.get("license_version") or "").strip()
        license_page = str(item.get("license_source_page") or "").strip()
        license_url = str(item.get("license_source_url") or "").strip()
        license_revision = str(item.get("license_source_revision_id") or "").strip()
        file_page = str(item.get("file_page_url") or item.get("source_page") or "").strip()
        source_url = str(item.get("source_image_url") or item.get("source_url") or "").strip()
        source_revision = str(item.get("source_revision_id") or "").strip()
        source_timestamp = str(item.get("source_revision_timestamp") or "").strip()
        source_uploader = str(item.get("source_uploader") or item.get("source_author") or "").strip()
        original_sha1 = str(item.get("original_sha1") or "").casefold().strip()
        source_sha1 = str(item.get("source_sha1") or "").casefold().strip()
        original_sha256 = str(item.get("original_sha256") or "").casefold().strip()
        transformations = item.get("transformations")
        return (
            "cc by-nc-sa" in license_name
            and license_version == "4.0"
            and license_page.startswith("https://wiki.biligame.com/")
            and license_url == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
            and license_revision.isdigit()
            and file_page.startswith("https://wiki.biligame.com/sonw/")
            and "/文件:" in unquote(file_page)
            and source_url.startswith("https://")
            and source_revision.isdigit()
            and bool(source_timestamp)
            and source_uploader.casefold() not in {"", "unknown", "未知"}
            and _sha1_evidence_matches(source_sha1, original_sha1)
            and bool(re.fullmatch(r"[0-9a-f]{64}", original_sha256))
            and isinstance(transformations, list)
            and bool(transformations)
        )

    def verify(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            # Release directories are immutable.  Full hashing is intentionally
            # a startup/deploy boundary, not work repeated by /config polling.
            if not force and self._cached_status is not None:
                return dict(self._cached_status)

            errors: list[str] = []
            manifest: dict[str, Any] = {}
            try:
                candidate = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    manifest = candidate
                else:
                    errors.append("manifest_invalid")
            except FileNotFoundError:
                errors.append("manifest_missing")
            except (OSError, json.JSONDecodeError):
                errors.append("manifest_invalid")

            manifest_version = str(manifest.get("media_version") or "")
            if manifest and manifest_version != self.version:
                errors.append("media_version_mismatch")
            schema_version = str(manifest.get("schema_version") or "")
            if manifest and schema_version not in {
                "project-snow-avatar-media-3",
                "project-snow-character-media-4",
            }:
                errors.append("schema_version_mismatch")
            if manifest and manifest.get("private_candidate") is not False:
                errors.append("license_review_incomplete")
            if manifest and str(manifest.get("license_review_status") or "") != "verified_public_release":
                errors.append("license_review_status_invalid")
            if schema_version == "project-snow-character-media-4":
                expected_character_count = len(self.expected_character_ids)
                if int(manifest.get("expression_character_count") or 0) != expected_character_count:
                    errors.append("expression_character_count_invalid")
                if int(manifest.get("expression_state_count") or 0) != expected_character_count * len(
                    EXPRESSION_STATES
                ):
                    errors.append("expression_state_total_invalid")
                if int(manifest.get("expression_asset_count") or 0) != expected_character_count * len(
                    EXPRESSION_STATES
                ) * 2:
                    errors.append("expression_asset_total_invalid")
                if "performance_count" in manifest or "performance_asset_count" in manifest:
                    character_rows = manifest.get("characters") or []
                    counts = [row.get("performance_count") for row in character_rows if isinstance(row, dict)]
                    valid_counts = len(counts) == expected_character_count and all(type(count) is int and 0 <= count <= 6 for count in counts)
                    if (not valid_counts or manifest.get("performance_count") != sum(counts)
                            or manifest.get("performance_asset_count") != sum(counts) * 2):
                        errors.append("expression_performance_total_invalid")
                for key in (
                    "expression_source_index_sha256",
                    "expression_rights_manifest_sha256",
                ):
                    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(key) or "")):
                        errors.append(f"{key}_invalid")

            # SHA256SUMS is part of the release boundary, rather than merely
            # a convenience for the download script.  Verifying its manifest
            # entry here prevents a compromised or partial mount from
            # advertising otherwise valid-looking avatar URLs.
            checksum_entries: dict[str, str] = {}
            checksum_file_ok = False
            try:
                checksum_lines = self.checksums_path.read_text(encoding="utf-8").splitlines()
                for line_number, raw_line in enumerate(checksum_lines, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(.+?)", line)
                    if not match:
                        errors.append(f"checksum_line_invalid:{line_number}")
                        continue
                    digest, relative = match.groups()
                    relative = relative.replace("\\", "/")
                    relative_path = Path(relative)
                    candidate_path = (self.root / relative_path).resolve()
                    try:
                        candidate_path.relative_to(self.root.resolve())
                    except ValueError:
                        errors.append(f"unsafe_checksum_path:{line_number}")
                        continue
                    if relative_path.is_absolute() or ".." in relative_path.parts:
                        errors.append(f"unsafe_checksum_path:{line_number}")
                        continue
                    normalized = relative_path.as_posix()
                    if normalized in checksum_entries:
                        errors.append(f"checksum_duplicate:{normalized}")
                        continue
                    checksum_entries[normalized] = digest.lower()
                checksum_file_ok = bool(checksum_lines) and not any(
                    error.startswith("checksum_") or error.startswith("unsafe_checksum")
                    for error in errors
                )
            except FileNotFoundError:
                errors.append("checksums_missing")
            except OSError:
                errors.append("checksums_unreadable")

            if checksum_file_ok:
                manifest_relative = "manifest.json"
                expected_manifest_hash = checksum_entries.get(manifest_relative)
                if not expected_manifest_hash:
                    errors.append("manifest_checksum_missing")
                elif (
                    not self.manifest_path.is_file()
                    or self._file_hash(self.manifest_path) != expected_manifest_hash
                ):
                    errors.append("manifest_checksum_mismatch")

            entries = manifest.get("characters") if isinstance(manifest.get("characters"), list) else []
            indexed: dict[str, dict[str, Any]] = {}
            verified_files = 0
            expected_expression_files = 0
            referenced_paths: set[str] = {"manifest.json"}
            for item in entries:
                if not isinstance(item, dict):
                    errors.append("character_entry_invalid")
                    continue
                character_id = str(item.get("character_id") or "")
                if not character_id or character_id in indexed:
                    errors.append("character_entry_duplicate")
                    continue
                indexed[character_id] = item
                if not self._license_is_verified(item):
                    errors.append(f"character_license_unverified:{character_id}")
                verified_files += self._verify_asset_entry(
                    character_id,
                    item,
                    checksum_entries,
                    referenced_paths,
                    errors,
                )
                if schema_version == "project-snow-character-media-4":
                    expression_verified, expression_expected = self._verify_expression_entry(
                        character_id,
                        item,
                        checksum_entries,
                        referenced_paths,
                        errors,
                    )
                    verified_files += expression_verified
                    expected_expression_files += expression_expected

            analyst_item = manifest.get("analyst")
            analyst_valid = isinstance(analyst_item, dict)
            if self.require_analyst and not analyst_valid:
                errors.append("analyst_missing")
            if analyst_valid:
                analyst_id = str(analyst_item.get("asset_id") or "analyst-default")
                if analyst_id != "analyst-default":
                    errors.append("analyst_asset_id_invalid")
                if not self._license_is_verified(analyst_item):
                    errors.append("analyst_license_unverified")
                verified_files += self._verify_asset_entry(
                    analyst_id,
                    analyst_item,
                    checksum_entries,
                    referenced_paths,
                    errors,
                )

            if checksum_entries:
                unexpected_checksums = sorted(set(checksum_entries) - referenced_paths)
                if unexpected_checksums:
                    errors.append("checksum_unexpected:" + ",".join(unexpected_checksums[:8]))

            missing = sorted(self.expected_character_ids - set(indexed))
            unexpected = sorted(set(indexed) - self.expected_character_ids)
            if missing:
                errors.append("characters_missing")
            if unexpected:
                errors.append("characters_unexpected")
            expected_count = len(self.expected_character_ids)
            expected_file_count = (
                expected_count * 2
                + (2 if analyst_valid else 0)
                + expected_expression_files
            )
            analyst_error = any(
                error.startswith(
                    (
                        "analyst_",
                        "file_missing:analyst",
                        "hash_mismatch:analyst",
                        "checksum_missing:analyst",
                        "checksum_mismatch:analyst",
                        "unsafe_path:analyst",
                    )
                )
                for error in errors
            )
            checksum_errors = tuple(
                error
                for error in errors
                if error.startswith(("checksum", "unsafe_checksum", "manifest_checksum"))
            )
            status = {
                "status": "ok" if not errors else "unavailable",
                "media_version": self.version,
                "manifest_version": manifest_version,
                "schema_version": schema_version,
                "manifest": (
                    "ok"
                    if manifest and "manifest_missing" not in errors and "manifest_invalid" not in errors
                    else "unavailable"
                ),
                "checksums": "ok" if checksum_file_ok and not checksum_errors else "unavailable",
                "character_count": len(indexed),
                "expected_character_count": expected_count,
                "verified_file_count": verified_files,
                "expected_file_count": expected_file_count,
                "expression_character_count": (
                    sum(
                        1
                        for item in indexed.values()
                        if item.get("expression_manifest_path")
                    )
                    if schema_version == "project-snow-character-media-4"
                    else 0
                ),
                "analyst": "ok" if analyst_valid and not analyst_error else "unavailable",
                "missing_character_ids": missing,
                "unexpected_character_ids": unexpected,
                "errors": errors[:24],
            }
            self._cached_status = status
            self._cached_manifest = manifest if manifest else None
            return dict(status)

    def avatar(self, character_id: str) -> dict[str, Any] | None:
        status = self.verify()
        if status["status"] != "ok" or self._cached_manifest is None:
            return None
        item = next(
            (
                value
                for value in self._cached_manifest.get("characters") or []
                if str(value.get("character_id") or "") == character_id
            ),
            None,
        )
        if not isinstance(item, dict):
            return None
        prefix = f"/media/{self.version}/"
        return {
            "src": prefix + str(item["stage_path"]).lstrip("/"),
            "thumbnail_src": prefix + str(item["thumbnail_path"]).lstrip("/"),
            "portrait_kind": str(item.get("portrait_kind") or "headshot"),
            "portrait_scale": float(item.get("portrait_scale") or 1.0),
            "portrait_focus_x": int(item.get("portrait_focus_x") or 50),
            "portrait_focus_y": int(item.get("portrait_focus_y") or 50),
            "source_page": str(item.get("file_page_url") or item.get("source_page") or ""),
            "license": str(item.get("license") or "CC BY-NC-SA"),
            "license_version": str(
                item.get("license_version") or "version unspecified by source"
            ),
            "license_source_page": str(item.get("license_source_page") or ""),
            "license_source_revision_id": str(item.get("license_source_revision_id") or ""),
            "source_revision_id": str(item.get("source_revision_id") or ""),
            "source_uploader": str(item.get("source_uploader") or item.get("source_author") or ""),
        }

    def expression_manifest(self, character_id: str) -> dict[str, Any] | None:
        """Expose only a fully verified, public expression manifest URL."""

        status = self.verify()
        if (
            status["status"] != "ok"
            or status.get("schema_version") != "project-snow-character-media-4"
            or self._cached_manifest is None
        ):
            return None
        item = next(
            (
                value
                for value in self._cached_manifest.get("characters") or []
                if str(value.get("character_id") or "") == character_id
            ),
            None,
        )
        if not isinstance(item, dict) or not item.get("expression_manifest_path"):
            return None
        return {
            "url": f"/media/{self.version}/"
            + str(item["expression_manifest_path"]).lstrip("/"),
            "state_count": int(item.get("expression_state_count") or 0),
        }

    def performance_catalog(self, character_id: str) -> list[dict[str, str]]:
        """Expose only the verified current character's optional performances."""
        if self.expression_manifest(character_id) is None or self._cached_manifest is None:
            return []
        item = next((row for row in self._cached_manifest.get("characters") or []
                     if isinstance(row, dict) and row.get("character_id") == character_id), None)
        if not item or not item.get("performance_count"):
            return []
        path = self.root / item["expression_manifest_path"]
        try:
            payload = path.read_bytes()
            if sha256(payload).hexdigest() != item["expression_manifest_sha256"]:
                return []
            manifest = json.loads(payload)
            rows = manifest.get("performances") or {}
            if not isinstance(rows, dict) or len(rows) > 6:
                return []
            return [{"character_id": character_id, "performance_id": cue,
                     "label": row["label"], "usage_context": row["usage_context"],
                     "fallback_expression": row["fallback_expression"]} for cue, row in rows.items()]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def analyst_avatar(self) -> dict[str, Any] | None:
        status = self.verify()
        if status["status"] != "ok" or self._cached_manifest is None:
            return None
        item = self._cached_manifest.get("analyst")
        if not isinstance(item, dict):
            return None
        prefix = f"/media/{self.version}/"
        return {
            "asset_id": str(item.get("asset_id") or "analyst-default"),
            "src": prefix + str(item["stage_path"]).lstrip("/"),
            "thumbnail_src": prefix + str(item["thumbnail_path"]).lstrip("/"),
            "portrait_kind": str(item.get("portrait_kind") or "headshot"),
            "portrait_scale": float(item.get("portrait_scale") or 1.0),
            "portrait_focus_x": int(item.get("portrait_focus_x") or 50),
            "portrait_focus_y": int(item.get("portrait_focus_y") or 50),
            "source_page": str(item.get("file_page_url") or item.get("source_page") or ""),
            "source_url": str(item.get("source_image_url") or item.get("source_url") or ""),
            "source_revision_id": str(item.get("source_revision_id") or ""),
            "license": str(item.get("license") or ""),
            "license_version": str(item.get("license_version") or ""),
            "license_status": str(item.get("license_status") or ""),
            "license_source_page": str(item.get("license_source_page") or ""),
            "license_source_url": str(item.get("license_source_url") or ""),
            "license_source_revision_id": str(item.get("license_source_revision_id") or ""),
        }

    def attributions(self) -> list[dict[str, Any]]:
        status = self.verify()
        if status["status"] != "ok" or self._cached_manifest is None:
            return []
        entries = [
            value
            for value in self._cached_manifest.get("characters") or []
            if isinstance(value, dict)
        ]
        analyst = self._cached_manifest.get("analyst")
        if isinstance(analyst, dict):
            entries.append(analyst)
        prefix = f"/media/{self.version}/"
        return [
            {
                "package_version": self.version,
                "asset_id": str(item.get("character_id") or item.get("asset_id") or ""),
                "display_name": str(item.get("character_name") or item.get("display_name") or ""),
                "preview_url": prefix + str(item.get("thumbnail_path") or "").lstrip("/"),
                "file_page_url": str(item.get("file_page_url") or item.get("source_page") or ""),
                "source_image_url": str(
                    item.get("source_image_url") or item.get("source_url") or ""
                ),
                "source_revision_id": str(item.get("source_revision_id") or ""),
                "source_revision_timestamp": str(
                    item.get("source_revision_timestamp") or ""
                ),
                "source_uploader": str(item.get("source_uploader") or item.get("source_author") or ""),
                "creator": "源页未提供",
                "source_sha1": str(item.get("source_sha1") or ""),
                "original_sha1": str(item.get("original_sha1") or ""),
                "source_sha256": str(item.get("original_sha256") or ""),
                "original_sha256": str(item.get("original_sha256") or ""),
                "thumbnail_sha256": str(item.get("thumbnail_sha256") or ""),
                "stage_sha256": str(item.get("stage_sha256") or ""),
                "dimensions": {
                    "source": {
                        "width": int(item.get("original_width") or 0),
                        "height": int(item.get("original_height") or 0),
                    },
                    "thumbnail": {
                        "width": int(item.get("thumbnail_width") or 0),
                        "height": int(item.get("thumbnail_height") or 0),
                    },
                    "stage": {
                        "width": int(item.get("stage_width") or 0),
                        "height": int(item.get("stage_height") or 0),
                    },
                },
                "license": str(item.get("license") or ""),
                "license_version": str(item.get("license_version") or ""),
                "license_source_page": str(item.get("license_source_page") or ""),
                "license_source_revision_id": str(item.get("license_source_revision_id") or ""),
                "modifications": list(item.get("transformations") or []),
            }
            for item in entries
        ]
