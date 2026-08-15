"""Versioned, separately deployed character media for the public surface."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable


class PublicMediaCatalog:
    """Read and verify the media package without coupling it to the app image."""

    def __init__(self, root: Path, version: str, expected_character_ids: Iterable[str]):
        self.root = Path(root)
        self.version = str(version)
        self.expected_character_ids = frozenset(str(value) for value in expected_character_ids)
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
                elif not self.manifest_path.is_file() or self._file_hash(self.manifest_path) != expected_manifest_hash:
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
                        errors.append(f"unsafe_path:{character_id}:{path_key}")
                        continue
                    normalized_relative = relative_path.as_posix()
                    if (
                        not relative
                        or relative_path.is_absolute()
                        or ".." in relative_path.parts
                        or normalized_relative != relative.replace("\\", "/")
                    ):
                        errors.append(f"unsafe_path:{character_id}:{path_key}")
                        continue
                    referenced_paths.add(normalized_relative)
                    if not candidate_path.is_file():
                        errors.append(f"file_missing:{character_id}:{path_key}")
                        continue
                    checksum_hash = checksum_entries.get(normalized_relative)
                    if not checksum_hash:
                        errors.append(f"checksum_missing:{character_id}:{path_key}")
                        continue
                    if len(expected_hash) != 64 or self._file_hash(candidate_path) != expected_hash.lower():
                        errors.append(f"hash_mismatch:{character_id}:{path_key}")
                        continue
                    if checksum_hash != expected_hash.lower() or checksum_hash != self._file_hash(candidate_path):
                        errors.append(f"checksum_mismatch:{character_id}:{path_key}")
                        continue
                    verified_files += 1

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
            checksum_errors = tuple(
                error
                for error in errors
                if error.startswith(("checksum", "unsafe_checksum", "manifest_checksum"))
            )
            status = {
                "status": "ok" if not errors else "unavailable",
                "media_version": self.version,
                "manifest_version": manifest_version,
                "manifest": "ok" if manifest and "manifest_missing" not in errors and "manifest_invalid" not in errors else "unavailable",
                "checksums": "ok" if checksum_file_ok and not checksum_errors else "unavailable",
                "character_count": len(indexed),
                "expected_character_count": expected_count,
                "verified_file_count": verified_files,
                "expected_file_count": expected_count * 2,
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
