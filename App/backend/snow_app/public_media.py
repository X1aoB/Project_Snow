"""Versioned, separately deployed character media for the public surface."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote


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
            if manifest and str(manifest.get("schema_version") or "") != "project-snow-avatar-media-3":
                errors.append("schema_version_mismatch")
            if manifest and manifest.get("private_candidate") is not False:
                errors.append("license_review_incomplete")
            if manifest and str(manifest.get("license_review_status") or "") != "verified_public_release":
                errors.append("license_review_status_invalid")

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
            expected_file_count = expected_count * 2 + (2 if analyst_valid else 0)
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
