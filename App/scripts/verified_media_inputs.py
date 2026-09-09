"""Read immutable, explicitly pinned media packages without following links."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.snow_app.public_media import PublicMediaCatalog  # noqa: E402

HASH = re.compile(r"[0-9a-f]{64}")
MAX_FILES = 5000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024


def regular_path(path: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("media input cannot contain symbolic links or reparse points")
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("media package root must be a directory")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
        raise ValueError("media input must be a bounded single regular file")
    return path


def read_regular(path: Path) -> bytes:
    path = regular_path(path)
    before = path.stat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("media input changed while opening")
        payload = stream.read(MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("media input exceeds the file size limit")
    return payload


def relative_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if (
        not raw
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", raw)
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("media checksum path is not canonical")
    return raw


def strict_object(payload: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate media JSON field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite media JSON value")

    value = json.loads(payload, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("media JSON object required")
    return value


class VerifiedPackage:
    def __init__(self, root: Path, manifest_sha256: str, checksums_sha256: str):
        if not HASH.fullmatch(manifest_sha256 or "") or not HASH.fullmatch(checksums_sha256 or ""):
            raise ValueError("explicit media manifest and checksum SHA256 pins are required")
        self.root = regular_path(root, directory=True)
        self.manifest_sha256, self.checksums_sha256 = manifest_sha256, checksums_sha256
        manifest_bytes, checksums_bytes = (
            read_regular(self.root / "manifest.json"),
            read_regular(self.root / "SHA256SUMS"),
        )
        if (
            sha256(manifest_bytes).hexdigest() != manifest_sha256
            or sha256(checksums_bytes).hexdigest() != checksums_sha256
        ):
            raise ValueError("media package metadata hash mismatch")
        self.manifest = strict_object(manifest_bytes)
        self.files: dict[str, str] = {}
        for line in checksums_bytes.decode("utf-8").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match:
                raise ValueError("media checksum line is invalid")
            digest, raw = match.groups()
            name = relative_path(raw)
            if name in self.files or name == "SHA256SUMS":
                raise ValueError("media checksum file list has duplicate or recursive entries")
            self.files[name] = digest
        if self.files.get("manifest.json") != manifest_sha256 or len(self.files) > MAX_FILES:
            raise ValueError("media checksums must bind the exact manifest")
        self.verify()

    def read(self, relative: str) -> bytes:
        relative_path(relative)
        if relative not in self.files:
            raise ValueError("media file is outside the pinned package")
        data = read_regular(self.root / relative)
        if sha256(data).hexdigest() != self.files[relative]:
            raise ValueError("media input file hash mismatch")
        return data

    def verify(self) -> None:
        actual, total = set(), 0
        for directory, children, names in os.walk(self.root, followlinks=False):
            for name in children:
                regular_path(Path(directory) / name, directory=True)
            for name in names:
                path = regular_path(Path(directory) / name)
                total += path.stat().st_size
                actual.add(path.relative_to(self.root).as_posix())
                if len(actual) > MAX_FILES + 1 or total > MAX_PACKAGE_BYTES:
                    raise ValueError("media package exceeds the size or file count limit")
        if actual != set(self.files) | {"SHA256SUMS"}:
            raise ValueError("media package file set does not match its checksums")
        if sha256(read_regular(self.root / "SHA256SUMS")).hexdigest() != self.checksums_sha256:
            raise ValueError("media package checksum file changed")
        for relative in self.files:
            self.read(relative)

    def copy_file(self, relative: str, destination: Path) -> None:
        data = self.read(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)


def validate_avatar(package: VerifiedPackage, character_ids: set[str]) -> None:
    manifest = package.manifest
    if (
        manifest.get("schema_version") != "project-snow-avatar-media-3"
        or manifest.get("private_candidate") is not False
        or manifest.get("release_basis") != "verified_public_release"
        or manifest.get("license_review_status") != "verified_public_release"
        or manifest.get("character_count") != len(character_ids)
    ):
        raise ValueError("existing avatar input is not a verified public avatar release")
    status = PublicMediaCatalog(
        package.root, str(manifest.get("media_version") or ""), character_ids, require_analyst=True
    ).verify(force=True)
    if status.get("status") != "ok" or status.get("verified_file_count") != 2 * (len(character_ids) + 1):
        raise ValueError(f"existing avatar release verification failed: {status.get('errors')}")
    expected = {"manifest.json"}
    for row, prefix in [(r, "avatars") for r in manifest["characters"]] + [(manifest["analyst"], "analyst")]:
        identifier = row["character_id"] if prefix == "avatars" else "analyst-default"
        for kind, size in (("thumbnail", 96), ("stage", 200)):
            path = f"{prefix}/{identifier}-{size}.webp"
            if row.get(kind + "_path") != path or package.files.get(path) != row.get(kind + "_sha256"):
                raise ValueError("existing avatar path or hash binding differs")
            with Image.open(BytesIO(package.read(path))) as image:
                if image.format != "WEBP" or image.size != (size, size) or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("existing avatar image has invalid format or dimensions")
                image.load()
            expected.add(path)
    if set(package.files) != expected:
        raise ValueError("existing avatar package contains unrelated files")


def copy_avatar(package: VerifiedPackage, destination: Path, version: str) -> dict[str, Any]:
    destination.mkdir()
    for path in package.files:
        if path != "manifest.json":
            package.copy_file(path, destination)
    manifest = copy.deepcopy(package.manifest)
    manifest["media_version"] = version
    manifest["avatar_source_package"] = {
        "schema_version": package.manifest["schema_version"],
        "media_version": package.manifest["media_version"],
        "manifest_sha256": package.manifest_sha256,
        "checksums_sha256": package.checksums_sha256,
        "reuse": "original_image_bytes_and_attribution_preserved",
        "independent_rights_verification_performed": False,
    }
    return manifest
