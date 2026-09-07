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
from typing import Callable


def controlled(path: Path, *, directory: bool = False, link: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISLNK if link else stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid or info.st_gid or (not link and info.st_mode & 0o022) or (not directory and info.st_nlink != 1):
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


def ensure_directory(path: Path, mode: int = 0o755) -> None:
    # Validate ancestors before mkdir so a linked or writable parent cannot
    # redirect even the preparatory writes.
    for parent in reversed((path, *path.parents)):
        if parent.exists() or parent.is_symlink():
            controlled(parent, directory=True)
        else:
            parent.mkdir(mode=mode if parent == path else 0o755)
            controlled(parent, directory=True)


def atomic_link(path: Path, target: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".install-link-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        temporary.symlink_to(target, target_is_directory=path.name == "current")
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_target(path: Path, destination: Path, *, allow_link: bool = False) -> dict:
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "kind": "absent"}
    if path.is_symlink() and allow_link:
        controlled(path, link=True)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(destination):
            raise SystemExit("Installed helper link leaves the controlled installation directory.")
        for parent in (resolved.parent, *resolved.parent.parents):
            controlled(parent, directory=True)
        controlled(resolved, directory=resolved.is_dir())
        return {"path": str(path), "kind": "link", "target": os.readlink(path)}
    controlled(path)
    return {"path": str(path), "kind": "file", "mode": stat.S_IMODE(path.stat().st_mode), "data": path.read_bytes()}


def restore_target(snapshot: dict) -> None:
    path = Path(snapshot["path"])
    if snapshot["kind"] == "absent":
        path.unlink(missing_ok=True)
        sync(path.parent)
    elif snapshot["kind"] == "link":
        atomic_link(path, snapshot["target"])
    else:
        atomic_bytes(path, snapshot["data"], snapshot["mode"])


def install(source: Path, destination: Path, unit_root: Path, *, reload_units: Callable[[], None] | None = None) -> dict:
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
        ensure_directory(directory)
    for parent in (unit_root, *unit_root.parents):
        controlled(parent, directory=True)
    current = destination / "current"
    if current.exists() and not current.is_symlink():
        raise SystemExit("Unexpected non-symlink helper current pointer.")
    # Every mutable destination is validated and captured before publication.
    # In particular, a masked/unsafe late unit must not switch current first.
    snapshots = [snapshot_target(destination / name, destination, allow_link=True) for name in payloads]
    snapshots.extend(snapshot_target(unit_root / name, destination) for name in units)
    snapshots.append(snapshot_target(current, destination, allow_link=True))
    previous = os.readlink(current) if current.is_symlink() else None
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
    receipts = destination / "install-receipts"
    ensure_directory(receipts, 0o700)
    if receipts.stat().st_mode & 0o077:
        raise SystemExit("Installation recovery receipts require a root-only directory.")
    receipt = Path(tempfile.mkdtemp(prefix="transaction-", dir=receipts))
    receipt.chmod(0o700)
    controlled(receipt, directory=True)
    record = {"schema_version": "project-snow-helper-install-1", "status": "prepared",
              "installed_version": identity, "destination": str(destination), "unit_root": str(unit_root), "targets": []}
    for index, snapshot in enumerate(snapshots):
        entry = {key: value for key, value in snapshot.items() if key != "data"}
        if snapshot["kind"] == "file":
            saved = receipt / f"original-{index}.bin"
            atomic_bytes(saved, snapshot["data"], 0o400)
            entry.update(backup=saved.name, sha256=hashlib.sha256(snapshot["data"]).hexdigest())
            if Path(snapshot["path"]) == destination / "maintenance.py":
                previous = str(saved)
        record["targets"].append(entry)
    receipt_path = receipt / "receipt.json"

    def record_status(status: str, errors: list[str] | None = None) -> None:
        record["status"] = status
        if errors:
            record["rollback_errors"] = errors
        atomic_bytes(receipt_path, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode(), 0o400)

    record_status("prepared")
    sync(receipts)
    try:
        for name in payloads:
            atomic_link(destination / name, "current/" + name)
        for name, data in units.items():
            atomic_bytes(unit_root / name, data, 0o644)
        # A running Python process retains the resolved generation in sys.path.
        # Publish current only after every module entry and unit is installed.
        atomic_link(current, "versions/" + identity)
        if reload_units is not None:
            reload_units()
        record_status("committed")
    except BaseException as error:
        failures = []
        for snapshot in reversed(snapshots):
            try:
                restore_target(snapshot)
            except BaseException as rollback_error:
                failures.append(f"{snapshot['path']}: {type(rollback_error).__name__}: {rollback_error}")
        if reload_units is not None:
            try:
                reload_units()
            except BaseException as rollback_error:
                failures.append(f"systemd reload: {type(rollback_error).__name__}: {rollback_error}")
        try:
            record_status("rollback_failed" if failures else "rolled_back", failures)
        except BaseException as receipt_error:
            failures.append(f"recovery receipt: {type(receipt_error).__name__}: {receipt_error}")
        if failures:
            raise RuntimeError(f"Helper installation failed and rollback is incomplete. Recovery receipt: {receipt_path}. " + "; ".join(failures)) from error
        raise RuntimeError(f"Helper installation failed; prior files and units were restored. Recovery receipt: {receipt_path}") from error
    return {"installed_version": identity, "previous_helper": previous, "recovery_receipt": str(receipt_path),
            "runner_changed": False, "timers_enabled": False}


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
    result = install(source, Path("/usr/local/libexec/project-snow"), Path("/etc/systemd/system"),
                     reload_units=lambda: subprocess.run(["systemctl", "daemon-reload"], check=True))
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
