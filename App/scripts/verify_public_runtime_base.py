"""Refuse reuse unless the approved image still supplies the exact runtime lock.

This build-only check does not install packages, change files or contact a
network. A changed dependency lock requires the fresh dependency build path.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

APPROVED_REVISION = "8f89c0f296d0bc880f786791ac36998c1b734365"
APPROVED_IMAGE = "ghcr.io/x1aob/project_snow-public@sha256:787eaaf0f65eb66d2a0ce89336832c0a9ea0a4909ddf1ad853754a5c0a7a5441"
APPROVED_LOCK_SHA256 = "ce3b52ee7554314ed1f3d70d83f212d538c75113b634c151cefe13b9160640fe"
MAX_LOCK_BYTES = 1024 * 1024


class RuntimeBaseError(ValueError):
    """An approved dependency base cannot be safely reused."""


def require(value: bool, message: str) -> None:
    if not value:
        raise RuntimeBaseError(message)


def lock_bytes(path: Path) -> bytes:
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= MAX_LOCK_BYTES,
            "Dependency lock must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink, opened.st_size)
                == (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size),
                "Dependency lock changed while opening")
        raw = stream.read(MAX_LOCK_BYTES + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()
    require(len(raw) == before.st_size and len(raw) <= MAX_LOCK_BYTES
            and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                == (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            and (final.st_dev, final.st_ino, final.st_mode, final.st_nlink, final.st_size, final.st_mtime_ns)
                == (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns),
            "Dependency lock changed while reading")
    return raw


def snow_uid() -> int:
    import pwd

    return pwd.getpwnam("snow").pw_uid


def verify(installed_lock: Path, candidate_lock: Path, *, revision: str | None = None,
           uid_lookup=None, executable_lookup=None, package_check=None) -> None:
    require((os.environ.get("APP_REVISION", "") if revision is None else revision) == APPROVED_REVISION,
            "Dependency base source differs from the approved release")
    installed = lock_bytes(installed_lock)
    candidate = lock_bytes(candidate_lock)
    require(installed == candidate and hashlib.sha256(installed).hexdigest() == APPROVED_LOCK_SHA256,
            "Dependency lock differs; use the fresh dependency build")
    uid_lookup = uid_lookup or snow_uid
    executable_lookup = executable_lookup or shutil.which
    package_check = package_check or subprocess.run
    uid = uid_lookup()
    require(type(uid) is int and uid == 10001, "Dependency base lacks the approved unprivileged user")
    gosu = executable_lookup("gosu")
    require(isinstance(gosu, str) and bool(gosu) and Path(gosu).is_absolute(),
            "Dependency base lacks the privilege-drop executable")
    result = package_check([sys.executable, "-I", "-B", "-m", "pip", "check"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=60)
    require(type(result.returncode) is int and result.returncode == 0,
            "Dependency base has inconsistent installed packages")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-lock", type=Path, default=Path("/app/requirements-public.txt"))
    parser.add_argument("--candidate-lock", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.installed_lock, args.candidate_lock)
    except (OSError, KeyError, RuntimeBaseError, subprocess.SubprocessError, ImportError):
        parser.exit(1, "Approved public runtime dependency base verification failed.\n")
    print("Approved public runtime dependency base verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
