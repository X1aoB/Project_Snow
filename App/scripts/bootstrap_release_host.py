"""Schedule exact-SHA host preparation from the legacy Docker-capable login.

This program is streamed from the selected origin/main Git object into a
digest-pinned Project Snow image.  It never imports or executes the historical
deploy-owned server checkout.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile


SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
EXPECTED_FILES = {
    "App/ops/bootstrap-release-runner.sh",
    "App/ops/docker-daemon.json",
    "App/ops/feedback-mailer.env.example",
    "App/ops/prepare_debian.sh",
    "App/ops/project-snow-release",
    "App/ops/project-snow-release.sudoers",
    "App/ops/sysctl-project-snow.conf",
}
EXPECTED_DIRECTORIES = {
    "App",
    "App/ops",
}
EXECUTABLE_FILES = {
    "App/ops/bootstrap-release-runner.sh",
    "App/ops/prepare_debian.sh",
    "App/ops/project-snow-release",
}


class BootstrapError(RuntimeError):
    pass


def _read_owned_bundle(path: Path, expected_uid: int, expected_sha256: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_nlink != 1
            or metadata.st_size < 512
            or metadata.st_size > MAX_BUNDLE_BYTES
        ):
            raise BootstrapError("host preparation bundle metadata is invalid")
        payload = bytearray()
        while len(payload) <= MAX_BUNDLE_BYTES:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size or len(payload) > MAX_BUNDLE_BYTES:
            raise BootstrapError("host preparation bundle changed or exceeded its limit")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise BootstrapError("host preparation bundle does not match the exact Git archive")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _remove_same_bundle(path: Path, *, device: int, inode: int) -> None:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == device
            and metadata.st_ino == inode
        ):
            path.unlink()
    except FileNotFoundError:
        return


def _safe_member_name(member: tarfile.TarInfo) -> str:
    raw = member.name.rstrip("/") if member.isdir() else member.name
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or "\x00" in raw
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != raw
    ):
        raise BootstrapError(f"unsafe host preparation member: {member.name!r}")
    return raw


def _extract_exact_bundle(payload: bytes, destination: Path) -> None:
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    with tarfile.open(fileobj=BytesIO(payload), mode="r:") as bundle:
        members = bundle.getmembers()
        if len(members) > 16:
            raise BootstrapError("host preparation bundle contains too many members")
        for member in members:
            name = _safe_member_name(member)
            if member.isdir():
                if name not in EXPECTED_DIRECTORIES or name in seen_directories:
                    raise BootstrapError(f"unexpected host preparation directory: {name}")
                seen_directories.add(name)
                continue
            if not member.isreg() or name not in EXPECTED_FILES or name in seen_files:
                raise BootstrapError(f"unexpected host preparation file: {name}")
            if member.size < 1 or member.size > 1024 * 1024:
                raise BootstrapError(f"invalid host preparation file size: {name}")
            seen_files.add(name)
        if seen_files != EXPECTED_FILES:
            raise BootstrapError("host preparation bundle is incomplete")

        for member in members:
            name = _safe_member_name(member)
            output = destination.joinpath(*PurePosixPath(name).parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = bundle.extractfile(member)
            if source is None:
                raise BootstrapError(f"cannot read host preparation file: {name}")
            data = source.read(member.size + 1)
            source.close()
            if len(data) != member.size:
                raise BootstrapError(f"host preparation file size changed: {name}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(output, flags, 0o700 if name in EXECUTABLE_FILES else 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)


def _write_root_file(path: Path, content: str, mode: int) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BootstrapError(f"refusing unsafe host bootstrap path: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _host_systemctl(host_root: Path, *arguments: str, check: bool = True) -> None:
    subprocess.run(
        ["/usr/sbin/chroot", str(host_root), "/bin/systemctl", *arguments],
        check=check,
        stdin=subprocess.DEVNULL,
    )


def schedule_host_prepare(
    *,
    host_root: Path,
    sha: str,
    archive_path: Path,
    archive_sha256: str,
    expected_owner_uid: int,
) -> None:
    if not SHA_PATTERN.fullmatch(sha) or not DIGEST_PATTERN.fullmatch(archive_sha256):
        raise BootstrapError("exact SHA and archive SHA256 are required")
    if host_root.is_symlink():
        raise BootstrapError("host root mount is invalid")
    host_root = host_root.resolve(strict=True)
    if not host_root.is_dir():
        raise BootstrapError("host root mount is invalid")
    if not archive_path.is_absolute() or ".." in archive_path.parts:
        raise BootstrapError("host preparation bundle path is invalid")
    if not re.fullmatch(
        rf"project-snow-prepare-{sha}-[0-9a-f]{{32}}\.tar", archive_path.name
    ):
        raise BootstrapError("host preparation bundle name is invalid")
    archive_parent = archive_path.parent.resolve(strict=True)
    try:
        archive_parent.relative_to((host_root / "tmp").resolve(strict=True))
    except ValueError as exc:
        raise BootstrapError("host preparation bundle must be below host /tmp") from exc
    archive_path = archive_parent / archive_path.name

    archive_metadata = archive_path.lstat()
    payload = _read_owned_bundle(archive_path, expected_owner_uid, archive_sha256)
    _remove_same_bundle(
        archive_path, device=archive_metadata.st_dev, inode=archive_metadata.st_ino
    )

    source_root = host_root / "run" / f"project-snow-prepare-{sha}"
    if source_root.is_symlink():
        raise BootstrapError("host preparation source root must not be a symlink")
    if source_root.exists():
        raise BootstrapError("host preparation for this SHA already exists; use root recovery")
    source_root.mkdir(parents=True, mode=0o700)
    _extract_exact_bundle(payload, source_root)

    project_root = host_root / "srv" / "project-snow"
    if project_root.is_symlink() or (project_root.exists() and not project_root.is_dir()):
        raise BootstrapError("/srv/project-snow must be a real directory")
    project_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(project_root, 0, 0)
    os.chmod(project_root, 0o755)
    status_host_path = project_root / f"prepare-{sha}.status"
    status_path = f"/srv/project-snow/prepare-{sha}.status"
    source_path = f"/run/project-snow-prepare-{sha}/App/ops"
    launcher_host_path = source_root / "run-host-prepare.sh"
    launcher_path = f"/run/project-snow-prepare-{sha}/run-host-prepare.sh"
    launcher = f"""#!/bin/sh
set -u
umask 077
status_path='{status_path}'
write_status() {{
  status_tmp="$status_path.tmp.$$"
  printf '%s\\n' "$1" > "$status_tmp"
  chmod 0644 "$status_tmp"
  mv -f "$status_tmp" "$status_path"
}}
write_status running
sleep 5
prepare_result=0
/bin/sh '{source_path}/prepare_debian.sh' --controller-sha '{sha}' || prepare_result=$?
if [ "$prepare_result" -eq 0 ]; then
  write_status success
else
  write_status "failed:$prepare_result"
fi
exit "$prepare_result"
"""
    _write_root_file(launcher_host_path, launcher, 0o700)
    _write_root_file(status_host_path, "scheduled\n", 0o644)

    unit_name = f"project-snow-prepare-{sha}.service"
    unit_host_path = host_root / "run" / "systemd" / "system" / unit_name
    unit_host_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    unit = f"""[Unit]
Description=Prepare and restrict Project Snow host at {sha}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh {launcher_path}
TimeoutStartSec=30min
StandardOutput=journal
StandardError=journal
"""
    _write_root_file(unit_host_path, unit, 0o644)
    _host_systemctl(host_root, "daemon-reload")
    _host_systemctl(host_root, "reset-failed", unit_name, check=False)
    _host_systemctl(host_root, "--no-block", "start", unit_name)
    print(f"scheduled exact-SHA host preparation as {unit_name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-root", type=Path, default=Path("/host"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--expected-owner-uid", type=int, required=True)
    arguments = parser.parse_args()
    try:
        schedule_host_prepare(
            host_root=arguments.host_root,
            sha=arguments.sha,
            archive_path=arguments.archive,
            archive_sha256=arguments.archive_sha256,
            expected_owner_uid=arguments.expected_owner_uid,
        )
    except (BootstrapError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"host preparation bootstrap failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
