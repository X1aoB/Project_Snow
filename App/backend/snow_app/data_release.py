"""Validation helpers for immutable production data releases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


class DataReleaseError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DataReleaseError(f"invalid JSONL in {path.name} at line {line_number}") from exc
            if not isinstance(row, dict):
                raise DataReleaseError(f"non-object JSONL row in {path.name} at line {line_number}")
            yield row


def _release_file(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DataReleaseError(f"unsafe release path: {relative}")
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DataReleaseError(f"release path escapes root: {relative}") from exc
    return resolved


def verify_data_release(root: Path, expected_version: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataReleaseError("missing or invalid data release manifest") from exc
    if manifest.get("schema_version") != "project-snow-data-release-2":
        raise DataReleaseError("unsupported data release schema")
    version = str(manifest.get("data_version") or "")
    if not version or (expected_version is not None and version != expected_version):
        raise DataReleaseError("data release version mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise DataReleaseError("data release contains no files")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise DataReleaseError("invalid data release file entry")
        relative = str(item.get("path") or "")
        if not relative or relative in seen:
            raise DataReleaseError("duplicate or empty data release path")
        seen.add(relative)
        path = _release_file(root, relative)
        if not path.is_file():
            raise DataReleaseError(f"missing data release file: {relative}")
        if path.stat().st_size != int(item.get("bytes") or -1):
            raise DataReleaseError(f"data release size mismatch: {relative}")
        if file_sha256(path) != str(item.get("sha256") or ""):
            raise DataReleaseError(f"data release digest mismatch: {relative}")
    return manifest
