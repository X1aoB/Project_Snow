"""Signed, versioned chat-sticker media for the public immersive surface."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
from typing import Any


_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}\Z")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class PublicStickerCatalog:
    """Validate a sticker release and expose only manifest-backed URLs."""

    def __init__(self, root: Path, version: str):
        self.root = Path(root)
        self.version = str(version)
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
    def _hash(path: Path) -> str:
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
                value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    manifest = value
                else:
                    errors.append("manifest_invalid")
            except FileNotFoundError:
                errors.append("manifest_missing")
            except (OSError, json.JSONDecodeError):
                errors.append("manifest_invalid")

            if manifest and str(manifest.get("media_version") or "") != self.version:
                errors.append("media_version_mismatch")
            if manifest and str(manifest.get("schema_version") or "") != "project-snow-sticker-1":
                errors.append("schema_version_mismatch")
            if manifest and manifest.get("private_candidate") is not False:
                errors.append("license_review_incomplete")
            if manifest and str(manifest.get("license_review_status") or "") != "verified_public_release":
                errors.append("license_review_status_invalid")
            if manifest and not str(manifest.get("license_policy") or "").strip():
                errors.append("license_policy_missing")
            checksum_entries: dict[str, str] = {}
            try:
                lines = self.checksums_path.read_text(encoding="utf-8").splitlines()
                for number, raw in enumerate(lines, 1):
                    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(.+?)", raw.strip())
                    if not match:
                        errors.append(f"checksum_line_invalid:{number}")
                        continue
                    digest, relative = match.groups()
                    relative_path = Path(relative.replace("\\", "/"))
                    if relative_path.is_absolute() or ".." in relative_path.parts:
                        errors.append(f"unsafe_checksum_path:{number}")
                        continue
                    candidate = (self.root / relative_path).resolve()
                    try:
                        candidate.relative_to(self.root.resolve())
                    except ValueError:
                        errors.append(f"unsafe_checksum_path:{number}")
                        continue
                    checksum_entries[relative_path.as_posix()] = digest.lower()
            except FileNotFoundError:
                errors.append("checksums_missing")
            except OSError:
                errors.append("checksums_unreadable")

            if "manifest.json" not in checksum_entries:
                errors.append("manifest_checksum_missing")
            elif self.manifest_path.is_file() and self._hash(self.manifest_path) != checksum_entries["manifest.json"]:
                errors.append("manifest_checksum_mismatch")

            entries = manifest.get("stickers") if isinstance(manifest.get("stickers"), list) else []
            indexed: dict[str, dict[str, Any]] = {}
            referenced = {"manifest.json"}
            verified = 0
            for item in entries:
                if not isinstance(item, dict):
                    errors.append("sticker_entry_invalid")
                    continue
                asset_id = str(item.get("asset_id") or "")
                if not _ASSET_ID.fullmatch(asset_id) or asset_id in indexed:
                    errors.append(f"sticker_asset_id_invalid:{asset_id[:24]}")
                    continue
                indexed[asset_id] = item
                owners = item.get("character_ids")
                tags = item.get("emotion_tags")
                scope = str(item.get("candidate_scope") or "")
                if not isinstance(owners, list) or any(
                    not re.fullmatch(r"[0-9a-f]{12}", str(value)) for value in owners
                ):
                    errors.append(f"sticker_character_scope_invalid:{asset_id}")
                    continue
                if not isinstance(tags, list) or not tags or any(
                    not str(value).strip() for value in tags
                ):
                    errors.append(f"sticker_emotion_tags_invalid:{asset_id}")
                    continue
                if scope not in {"character", "generic"} or (scope == "character") != bool(owners):
                    errors.append(f"sticker_candidate_scope_invalid:{asset_id}")
                    continue
                source_fields = ("file_page_url", "source_page_url", "source_image_url")
                if any(
                    not str(item.get(field) or "").startswith("https://")
                    for field in source_fields
                ):
                    errors.append(f"sticker_source_invalid:{asset_id}")
                    continue
                if (
                    "CC BY-NC-SA 4.0" not in str(item.get("license") or "")
                    or str(item.get("license_version") or "") != "4.0"
                    or str(item.get("license_status") or "") != "verified"
                    or not str(item.get("attribution") or "").strip()
                ):
                    errors.append(f"sticker_license_invalid:{asset_id}")
                    continue
                relative = str(item.get("path") or "").replace("\\", "/")
                relative_path = Path(relative)
                if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
                    errors.append(f"unsafe_sticker_path:{asset_id}")
                    continue
                normalized = relative_path.as_posix()
                referenced.add(normalized)
                candidate = (self.root / relative_path).resolve()
                try:
                    candidate.relative_to(self.root.resolve())
                except ValueError:
                    errors.append(f"unsafe_sticker_path:{asset_id}")
                    continue
                expected = str(item.get("sha256") or "").lower()
                if str(item.get("content_hash") or "").lower() != expected:
                    errors.append(f"sticker_content_hash_mismatch:{asset_id}")
                    continue
                if not candidate.is_file():
                    errors.append(f"sticker_file_missing:{asset_id}")
                    continue
                if len(expected) != 64 or self._hash(candidate) != expected:
                    errors.append(f"sticker_hash_mismatch:{asset_id}")
                    continue
                if checksum_entries.get(normalized) != expected:
                    errors.append(f"sticker_checksum_mismatch:{asset_id}")
                    continue
                thumbnail = str(item.get("thumbnail_path") or "").replace("\\", "/")
                thumbnail_path = Path(thumbnail)
                if not thumbnail or thumbnail_path.is_absolute() or ".." in thumbnail_path.parts:
                    errors.append(f"unsafe_sticker_thumbnail_path:{asset_id}")
                    continue
                thumbnail_normalized = thumbnail_path.as_posix()
                referenced.add(thumbnail_normalized)
                thumbnail_candidate = (self.root / thumbnail_path).resolve()
                try:
                    thumbnail_candidate.relative_to(self.root.resolve())
                except ValueError:
                    errors.append(f"unsafe_sticker_thumbnail_path:{asset_id}")
                    continue
                thumbnail_expected = str(item.get("thumbnail_sha256") or "").lower()
                if not thumbnail_candidate.is_file():
                    errors.append(f"sticker_thumbnail_missing:{asset_id}")
                    continue
                if len(thumbnail_expected) != 64 or self._hash(thumbnail_candidate) != thumbnail_expected:
                    errors.append(f"sticker_thumbnail_hash_mismatch:{asset_id}")
                    continue
                if checksum_entries.get(thumbnail_normalized) != thumbnail_expected:
                    errors.append(f"sticker_thumbnail_checksum_mismatch:{asset_id}")
                    continue
                verified += 1

            unexpected = sorted(set(checksum_entries) - referenced)
            if unexpected:
                errors.append("checksum_unexpected:" + ",".join(unexpected[:8]))
            try:
                expected_count = int(manifest.get("count") or len(entries))
            except (TypeError, ValueError):
                expected_count = len(entries)
                errors.append("sticker_count_invalid")
            if expected_count != len(entries):
                errors.append("sticker_count_mismatch")
            status = {
                "status": "ok" if not errors else "unavailable",
                "media_version": self.version,
                "manifest": "ok" if manifest else "unavailable",
                "checksums": "ok" if "checksums_missing" not in errors and "manifest_checksum_mismatch" not in errors else "unavailable",
                # Never advertise a partial or unreviewed package as usable.
                "sticker_count": verified if not errors else 0,
                "expected_sticker_count": expected_count,
                "verified_file_count": verified,
                "errors": errors[:24],
            }
            self._cached_at = now
            self._cached_status = status
            self._cached_manifest = manifest if manifest else None
            return dict(status)

    def list(self, *, section: str = "", cursor: int = 0, limit: int = 60) -> dict[str, Any]:
        status = self.verify()
        if status["status"] != "ok" or self._cached_manifest is None:
            return {"stickers": [], "next_cursor": None, "status": status["status"]}
        values = [item for item in self._cached_manifest.get("stickers") or [] if isinstance(item, dict)]
        if section:
            values = [item for item in values if str(item.get("section") or "") == section]
        start = max(0, int(cursor))
        page = values[start : start + max(1, min(int(limit), 500))]
        prefix = f"/media/{self.version}/"
        output = []
        for item in page:
            output.append({
                "asset_id": str(item.get("asset_id") or ""),
                "caption": str(item.get("caption") or ""),
                "section": str(item.get("section") or "未分类"),
                "src": prefix + str(item.get("path") or "").lstrip("/"),
                "thumbnail_src": prefix + str(item.get("thumbnail_path") or item.get("path") or "").lstrip("/"),
                "mime_type": str(item.get("mime_type") or "image/png"),
                "animated": bool(item.get("animated")),
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
                "character_ids": _string_list(item.get("character_ids") or item.get("character_scope")),
                "emotion_tags": _string_list(item.get("emotion_tags") or item.get("tags")),
                "candidate_scope": str(item.get("candidate_scope") or ("character" if item.get("character_ids") else "generic")),
            })
        next_cursor = start + len(page) if start + len(page) < len(values) else None
        return {"stickers": output, "next_cursor": next_cursor, "status": status["status"]}

    def resolve(self, asset_id: str) -> dict[str, Any] | None:
        status = self.verify()
        if status["status"] != "ok" or not self._cached_manifest:
            return None
        item = next((value for value in self._cached_manifest.get("stickers") or [] if value.get("asset_id") == asset_id), None)
        if not isinstance(item, dict):
            return None
        prefix = f"/media/{self.version}/"
        return {
            "asset_id": asset_id,
            "caption": str(item.get("caption") or ""),
            "section": str(item.get("section") or "未分类"),
            "src": prefix + str(item.get("path") or "").lstrip("/"),
            "thumbnail_src": prefix + str(item.get("thumbnail_path") or item.get("path") or "").lstrip("/"),
            "mime_type": str(item.get("mime_type") or "image/png"),
            "animated": bool(item.get("animated")),
            "character_ids": _string_list(item.get("character_ids") or item.get("character_scope")),
            "emotion_tags": _string_list(item.get("emotion_tags") or item.get("tags")),
            "candidate_scope": str(item.get("candidate_scope") or ("character" if item.get("character_ids") else "generic")),
        }
