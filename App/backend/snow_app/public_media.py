"""Versioned, separately deployed character media for the public surface."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any


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
        self._cached_at = 0.0
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
    def _analyst_license_is_verified(item: dict[str, Any]) -> bool:
        """Require auditable license evidence before exposing the analyst URL.

        The Wiki's global notice is acceptable only when the manifest records
        the exact notice URL/revision and explicitly says that the file page
        has no overriding exception.  A missing or guessed license must keep
        the independent asset out of the public catalog.
        """

        status = str(item.get("license_status") or "").casefold().strip()
        if status not in {
            "verified",
            "verified_explicit",
            "verified_site_policy_no_page_exception",
        }:
            return False
        license_name = str(item.get("license") or "").casefold()
        license_version = str(item.get("license_version") or "").strip()
        source_page = str(item.get("license_source_page") or "").strip()
        source_url = str(item.get("license_source_url") or "").strip()
        source_revision = str(item.get("license_source_revision_id") or "").strip()
        return (
            "cc by-nc-sa" in license_name
            and license_version == "4.0"
            and source_page.startswith("https://wiki.biligame.com/")
            and source_url == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
            and bool(source_revision)
        )

    def verify(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cached_status is not None and now - self._cached_at < 30:
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
                if not self._analyst_license_is_verified(analyst_item):
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
            self._cached_at = now
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
            "source_page": str(item.get("source_page") or ""),
            "license": str(item.get("license") or "CC BY-NC-SA"),
            "license_version": str(
                item.get("license_version") or "version unspecified by source"
            ),
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
            "source_page": str(item.get("source_page") or ""),
            "source_url": str(item.get("source_url") or ""),
            "source_revision_id": str(item.get("source_revision_id") or ""),
            "license": str(item.get("license") or ""),
            "license_version": str(item.get("license_version") or ""),
            "license_status": str(item.get("license_status") or ""),
            "license_source_page": str(item.get("license_source_page") or ""),
            "license_source_url": str(item.get("license_source_url") or ""),
            "license_source_revision_id": str(item.get("license_source_revision_id") or ""),
        }
