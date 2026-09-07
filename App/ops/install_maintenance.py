#!/usr/bin/env python3
"""Install a reviewed helper generation without replacing the release runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


def controlled(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        raise SystemExit("Installation requires reviewed root-controlled source and destination paths.")


def atomic_bytes(path: Path, payload: bytes, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fchmod(target.fileno(), mode)
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(source: Path, destination: Path, unit_root: Path) -> dict:
    for parent in (source, *source.parents):
        controlled(parent, directory=True)
    files = {name: source / name for name in ("maintenance.py", "release_state.py", "recovery_backup.py", "auto_stage.py", "routing.py", "monitor.py")}
    files["verify_release_proof.py"] = source.parent / "scripts" / "verify_release_proof.py"
    payloads = {}
    for name, path in files.items():
        controlled(path.parent, directory=True)
        controlled(path)
        payloads[name] = path.read_bytes()
        compile(payloads[name], str(path), "exec")
    units = {}
    for kind in ("cleanup", "backup", "auto-stage", "monitor"):
        for suffix in ("service", "timer"):
            name = f"project-snow-{kind}.{suffix}"
            controlled(source / name)
            units[name] = (source / name).read_bytes()
    identity = hashlib.sha256(json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}, sort_keys=True).encode()).hexdigest()
    for directory in (destination, destination / "versions"):
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        controlled(directory, directory=True)
    controlled(unit_root, directory=True)
    for parent in destination.parents:
        controlled(parent, directory=True)
    version = destination / "versions" / identity
    if not version.exists() and not version.is_symlink():
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=version.parent))
        try:
            for name, data in payloads.items():
                atomic_bytes(temporary / name, data, 0o444)
            temporary.chmod(0o555)
            os.rename(temporary, version)
            sync(version.parent)
        finally:
            if temporary.exists():
                temporary.chmod(0o700)
                shutil.rmtree(temporary)
    controlled(version, directory=True)
    for name, data in payloads.items():
        controlled(version / name)
        if (version / name).read_bytes() != data:
            raise SystemExit("Installed immutable helper generation differs from reviewed source.")
    current = destination / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise SystemExit("Unexpected non-symlink helper current pointer.")
    legacy = destination / "maintenance.py"
    if legacy.exists() and not legacy.is_symlink():
        controlled(legacy)
        saved = destination / ("legacy-maintenance-" + hashlib.sha256(legacy.read_bytes()).hexdigest() + ".py")
        if not saved.exists():
            atomic_bytes(saved, legacy.read_bytes(), 0o444)
        previous = str(saved)
    # One pointer selects all imports. Python resolves the entry script's
    # symlink before initializing sys.path, so a running generation is stable.
    link = destination / (".current-" + identity)
    link.unlink(missing_ok=True)
    link.symlink_to("versions/" + identity)
    os.replace(link, current)
    for name in payloads:
        link = destination / (".link-" + name)
        link.unlink(missing_ok=True)
        link.symlink_to("current/" + name)
        os.replace(link, destination / name)
    sync(destination)
    for name, data in units.items():
        existing = unit_root / name
        if existing.exists() or existing.is_symlink():
            controlled(existing)
            saved = destination / ("previous-" + name + "-" + hashlib.sha256(existing.read_bytes()).hexdigest())
            if not saved.exists():
                atomic_bytes(saved, existing.read_bytes(), 0o444)
        atomic_bytes(existing, data, 0o644)
    return {"installed_version": identity, "previous_helper": previous, "runner_changed": False, "timers_enabled": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-timers", action="store_true")
    parser.add_argument("--enable-auto-stage", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Maintenance installation requires root.")
    source = Path(__file__).resolve().parent
    if args.enable_auto_stage:
        result = subprocess.run(["gh", "attestation", "verify", "--help"], capture_output=True, text=True, check=True)
        if any(flag not in result.stdout for flag in ("--bundle", "--signer-workflow", "--source-ref", "--source-digest", "--signer-digest", "--deny-self-hosted-runners")):
            raise SystemExit("Install a trusted modern gh CLI before enabling candidate pull.")
        runner = Path("/usr/local/sbin/project-snow-release")
        controlled(runner)
        if runner.read_bytes() != (source / "project-snow-release").read_bytes():
            raise SystemExit("Review and install the matching proof-enforcing runner before enabling automatic stage.")
    result = install(source, Path("/usr/local/libexec/project-snow"), Path("/etc/systemd/system"))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if args.enable_timers:
        subprocess.run(["systemctl", "enable", "--now", "project-snow-cleanup.timer"], check=True)
        subprocess.run(["systemctl", "enable", "--now", "project-snow-monitor.timer"], check=True)
        config = Path("/etc/project-snow/restic.env")
        if config.exists() and shutil.which("restic"):
            controlled(config)
            if config.stat().st_mode & 0o077:
                raise SystemExit("Backup credentials must have mode 0600 or 0400.")
            subprocess.run(["systemctl", "enable", "--now", "project-snow-backup.timer"], check=True)
        else:
            print("Backup timer unchanged: provision root-only restic.env and restic first.")
    if args.enable_auto_stage:
        subprocess.run(["systemctl", "enable", "--now", "project-snow-auto-stage.timer"], check=True)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
