"""Safely install immutable data/avatar/sticker archives from the deploy inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.snow_app.data_release import verify_data_release  # noqa: E402
from backend.snow_app.public_media import PublicMediaCatalog  # noqa: E402
from backend.snow_app.public_stickers import PublicStickerCatalog  # noqa: E402


MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 20_000
KINDS = {"avatar", "sticker", "data"}
HEX_DIGITS = frozenset("0123456789abcdef")


class InstallError(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallError(f"trusted release metadata is missing: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise InstallError(f"trusted release metadata is not a single regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_sha256(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in HEX_DIGITS for character in value):
        raise InstallError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _safe_member_name(raw_name: str, *, directory: bool) -> str | None:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        raise InstallError("archive contains an empty or non-POSIX member name")
    name = raw_name
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/") if directory else name
    if not name:
        return None if directory else ""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise InstallError(f"unsafe archive member path: {raw_name!r}")
    if any(part in {"", "."} for part in path.parts):
        raise InstallError(f"non-canonical archive member path: {raw_name!r}")
    return path.as_posix()


def _open_archive(path: Path, expected_owner_uid: int):
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise InstallError("release archive is not a regular file")
    if metadata.st_uid != expected_owner_uid or metadata.st_nlink != 1:
        os.close(descriptor)
        raise InstallError("release archive ownership or link count is invalid")
    if metadata.st_size < 512 or metadata.st_size > MAX_ARCHIVE_BYTES:
        os.close(descriptor)
        raise InstallError("release archive size is outside the allowed range")
    return os.fdopen(descriptor, "rb", closefd=True)


def _extract_archive(archive: Path, destination: Path, expected_owner_uid: int) -> None:
    seen: set[str] = set()
    expanded_bytes = 0
    member_count = 0
    with _open_archive(archive, expected_owner_uid) as archive_file:
        with tarfile.open(fileobj=archive_file, mode="r:*") as bundle:
            members = bundle.getmembers()
            if len(members) > MAX_MEMBERS:
                raise InstallError("release archive contains too many members")
            for member in members:
                member_count += 1
                if not (member.isdir() or member.isreg()):
                    raise InstallError(f"unsupported archive member type: {member.name!r}")
                normalized = _safe_member_name(member.name, directory=member.isdir())
                if normalized is None:
                    continue
                if not normalized or normalized in seen:
                    raise InstallError(f"duplicate archive member: {member.name!r}")
                seen.add(normalized)
                if member.isreg():
                    if member.size < 0:
                        raise InstallError("archive contains a negative file size")
                    expanded_bytes += member.size
                    if expanded_bytes > MAX_EXPANDED_BYTES:
                        raise InstallError("expanded release archive is too large")

            if member_count == 0:
                raise InstallError("release archive is empty")

            for member in members:
                normalized = _safe_member_name(member.name, directory=member.isdir())
                if normalized is None:
                    continue
                output = destination.joinpath(*PurePosixPath(normalized).parts)
                try:
                    output.relative_to(destination)
                except ValueError as exc:  # pragma: no cover - defensive after lexical checks
                    raise InstallError("archive member escapes extraction root") from exc
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = bundle.extractfile(member)
                if source is None:
                    raise InstallError(f"cannot read archive member: {member.name!r}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(output, flags, 0o600)
                written = 0
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > member.size:
                                raise InstallError("archive member exceeds its declared size")
                            target.write(chunk)
                finally:
                    source.close()
                if written != member.size:
                    raise InstallError("archive member is shorter than its declared size")


def _walk_regular_files(root: Path) -> set[str]:
    if not root.is_dir() or root.is_symlink():
        raise InstallError("release root is missing or is a symlink")
    files: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise InstallError(f"release contains a directory symlink: {candidate}")
        for name in names:
            candidate = current_path / name
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise InstallError(
                    f"release contains a non-regular or multiply-linked file: {candidate}"
                )
            files.add(candidate.relative_to(root).as_posix())
    return files


def _checksum_paths(root: Path) -> set[str]:
    checksum_file = root / "SHA256SUMS"
    try:
        lines = checksum_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InstallError("release has no readable SHA256SUMS") from exc
    paths: set[str] = set()
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise InstallError("release contains an invalid SHA256SUMS entry")
        relative = parts[1].lstrip("*")
        normalized = _safe_member_name(relative, directory=False)
        if not normalized or normalized in paths:
            raise InstallError("release contains a duplicate checksum path")
        paths.add(normalized)
    if not paths:
        raise InstallError("release checksum list is empty")
    return paths


def _verify_avatar(root: Path, version: str) -> dict[str, Any]:
    registry = json.loads(
        (APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json").read_text(
            encoding="utf-8"
        )
    )
    character_ids = [
        str(item.get("character_id") or "")
        for item in registry.get("characters", [])
        if isinstance(item, dict)
    ]
    status = PublicMediaCatalog(
        root, version, character_ids, require_analyst=True
    ).verify(force=True)
    if status.get("status") != "ok" or status.get("character_count") != 22:
        raise InstallError(f"avatar release verification failed: {status.get('errors')}")
    expected_files = _checksum_paths(root) | {"SHA256SUMS"}
    if _walk_regular_files(root) != expected_files:
        raise InstallError("avatar release files are not covered exactly by SHA256SUMS")
    return status


def _verify_sticker(root: Path, version: str) -> dict[str, Any]:
    status = PublicStickerCatalog(root, version).verify(force=True)
    if status.get("status") != "ok" or status.get("sticker_count") != 363:
        raise InstallError(f"sticker release verification failed: {status.get('errors')}")
    expected_files = _checksum_paths(root) | {"SHA256SUMS"}
    if _walk_regular_files(root) != expected_files:
        raise InstallError("sticker release files are not covered exactly by SHA256SUMS")
    return status


def _verify_data(root: Path, version: str) -> dict[str, Any]:
    manifest = verify_data_release(root, version)
    entries = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    expected_files = {"manifest.json"} | {
        str(item.get("path") or "") for item in entries if isinstance(item, dict)
    }
    if "" in expected_files or _walk_regular_files(root) != expected_files:
        raise InstallError("data release contains files outside its signed manifest")
    return {"status": "ok", "data_version": version}


def verify_release(kind: str, root: Path, version: str) -> dict[str, Any]:
    if kind == "avatar":
        return _verify_avatar(root, version)
    if kind == "sticker":
        return _verify_sticker(root, version)
    if kind == "data":
        return _verify_data(root, version)
    raise InstallError(f"unsupported release kind: {kind}")


def verify_trusted_binding(
    kind: str,
    root: Path,
    *,
    expected_manifest_sha256: str,
    expected_checksums_sha256: str | None,
) -> None:
    expected_manifest_sha256 = _validated_sha256(
        expected_manifest_sha256, "manifest SHA256"
    )
    if _file_sha256(root / "manifest.json") != expected_manifest_sha256:
        raise InstallError("release manifest does not match the trusted Git/CI binding")
    if kind == "data":
        if expected_checksums_sha256 is not None:
            raise InstallError("data releases must not supply a SHA256SUMS binding")
        return
    if expected_checksums_sha256 is None:
        raise InstallError(f"{kind} release is missing its trusted SHA256SUMS binding")
    expected_checksums_sha256 = _validated_sha256(
        expected_checksums_sha256, "SHA256SUMS SHA256"
    )
    if _file_sha256(root / "SHA256SUMS") != expected_checksums_sha256:
        raise InstallError("SHA256SUMS does not match the trusted Git/CI binding")


def install_release(
    *,
    kind: str,
    version: str,
    destination_root: Path,
    archive: Path | None,
    expected_owner_uid: int,
    expected_manifest_sha256: str,
    expected_checksums_sha256: str | None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise InstallError("release kind must be avatar, sticker or data")
    if not version or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in version):
        raise InstallError("unsafe release version")
    if destination_root.is_symlink():
        raise InstallError("destination root must not be a symlink")
    destination_root = destination_root.resolve(strict=True)
    if not destination_root.is_dir():
        raise InstallError("destination root must be a real directory")
    destination_metadata = destination_root.stat()
    if destination_metadata.st_uid != 0 or destination_metadata.st_mode & 0o022:
        raise InstallError("destination root must be root-owned and not group/world writable")
    target = destination_root / version
    if target.is_symlink():
        raise InstallError("installed release target must not be a symlink")
    if target.exists():
        if not target.is_dir():
            raise InstallError("installed release target must be a directory")
        verify_trusted_binding(
            kind,
            target,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_checksums_sha256=expected_checksums_sha256,
        )
        status = verify_release(kind, target, version)
        return {"result": "already-installed", **status}
    if archive is None:
        raise InstallError(f"missing archive for {kind} release {version}")

    staging = Path(tempfile.mkdtemp(prefix=f".{version}.candidate.", dir=destination_root))
    try:
        _extract_archive(archive, staging, expected_owner_uid)
        verify_trusted_binding(
            kind,
            staging,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_checksums_sha256=expected_checksums_sha256,
        )
        status = verify_release(kind, staging, version)
        for current, directories, files in os.walk(staging):
            for directory in directories:
                os.chmod(Path(current) / directory, 0o755)
            for file_name in files:
                os.chmod(Path(current) / file_name, 0o644)
        os.chmod(staging, 0o755)
        try:
            staging.rename(target)
        except FileExistsError:
            verify_trusted_binding(
                kind,
                target,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_checksums_sha256=expected_checksums_sha256,
            )
            verify_release(kind, target, version)
        return {"result": "installed", **status}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--expected-owner-uid", type=int, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--checksums-sha256")
    arguments = parser.parse_args()
    try:
        result = install_release(
            kind=arguments.kind,
            version=arguments.version,
            destination_root=arguments.destination_root,
            archive=arguments.archive,
            expected_owner_uid=arguments.expected_owner_uid,
            expected_manifest_sha256=arguments.manifest_sha256,
            expected_checksums_sha256=arguments.checksums_sha256,
        )
    except (InstallError, OSError, tarfile.TarError, ValueError, json.JSONDecodeError) as exc:
        print(f"release archive installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
