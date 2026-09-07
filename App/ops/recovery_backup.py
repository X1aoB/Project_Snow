"""Encrypted recovery snapshots; production database restore is disabled."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable

from maintenance import (OBJECT_ID, PROJECT, MaintenanceError, Paths, active_release,
                         docker_json, read_environment, run)
from release_state import archive_colours, atomic_write, protected_image_references

BACKUP_TAG = "project-snow-production"
WRITERS = {"public-api-blue", "public-api-green", "feedback-mailer", "admin"}


def host_recovery_sources() -> list[Path]:
    return [Path("/usr/local/libexec/project-snow"), Path("/usr/local/sbin/project-snow-release"),
            Path("/etc/sudoers.d/project-snow-release"),
            *sorted(Path("/etc/systemd/system").glob("project-snow-*.service")),
            *sorted(Path("/etc/systemd/system").glob("project-snow-*.timer"))]


def hash_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def postgres_container(paths: Paths, execute: Callable[[list[str]], str] = run) -> str:
    identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                           "--filter", "label=com.docker.compose.service=postgres"]).split()
    if len(identifiers) != 1 or not OBJECT_ID.fullmatch(identifiers[0]):
        raise MaintenanceError("Expected exactly one running production PostgreSQL container")
    containers = docker_json(["inspect", identifiers[0]], execute)
    expected = read_environment(paths.root / "runtime" / "compose.env").get("POSTGRES_IMAGE", "")
    images = docker_json(["image", "inspect", expected], execute)
    if len(containers) != 1 or len(images) != 1 or containers[0].get("Image") != images[0].get("Id"):
        raise MaintenanceError("PostgreSQL image differs from the promoted recovery pin")
    return identifiers[0]


def write_dump(command: list[str], destination: Path) -> None:
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, check=False, timeout=600)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode or destination.stat().st_size == 0:
        raise MaintenanceError("PostgreSQL dump failed")


def consume_dump(command: list[str], source: Path) -> None:
    with source.open("rb") as dump:
        result = subprocess.run(command, stdin=dump, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=1800)
    if result.returncode:
        raise MaintenanceError("PostgreSQL archive validation or restore failed")


def backup(paths: Paths, execute: Callable[[list[str]], str] = run,
           dump_writer: Callable[[list[str], Path], None] = write_dump,
           dump_consumer: Callable[[list[str], Path], None] = consume_dump, *, pin: bool = False) -> dict[str, Any]:
    active = active_release(paths)
    anchors = archive_colours(paths)
    postgres = postgres_container(paths, execute)
    database_size = execute(["docker", "exec", "--user", "postgres", postgres, "psql", "-U", "project_snow", "-d", "project_snow",
                             "-At", "-v", "ON_ERROR_STOP=1", "-c", "SELECT pg_database_size(current_database())"]).strip()
    if not database_size.isdigit():
        raise MaintenanceError("Could not estimate backup staging capacity")
    if shutil.disk_usage(paths.root).free < max(512 * 1024**2, 2 * int(database_size)):
        raise MaintenanceError("Insufficient space for a safe PostgreSQL dump")
    staging = paths.root / "backups" / "staging"
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    require_private_staging(staging)
    work = Path(tempfile.mkdtemp(prefix="recovery-", dir=staging))
    try:
        dump = work / "postgres.dump"
        dump_writer(["docker", "exec", "--user", "postgres", postgres, "pg_dump", "-U", "project_snow", "-d", "project_snow",
                     "--lock-wait-timeout=5s", "--format=custom"], dump)
        dump_consumer(["docker", "exec", "-i", "--user", "postgres", postgres, "pg_restore", "--list"], dump)
        dump_hash = hash_file(dump)
        atomic_write(work / "postgres.dump.sha256", (dump_hash + "\n").encode())
        # Unknown older anchor layouts are backed up verbatim. Only destructive
        # GC requires interpreting every anchor before it may proceed.
        references = sorted(protected_image_references(paths, strict_unknown=False))
        recovery = {"schema_version": "project-snow-recovery-1", "created_at": datetime.now(timezone.utc).isoformat(),
                    "active": active, "anchors": anchors, "postgres_dump_sha256": dump_hash,
                    "image_references": references, "image_recovery": "Pull exact digests from the retained registry; this snapshot does not contain image tar archives."}
        atomic_write(work / "recovery.json", (json.dumps(recovery, sort_keys=True, indent=2) + "\n").encode())
        # Immutable packages are read directly instead of copying large data or
        # media archives onto the shared host. The release lock fixes metadata.
        sources = [work, paths.root / "releases", paths.root / "runtime", paths.root / "repo", paths.secrets.parent,
                   *host_recovery_sources()]
        for required in sources[1:5]:
            if not required.exists() or required.is_symlink():
                raise MaintenanceError("A required recovery source is missing or linked")
        for relative in ("data/releases", "media/releases", "media/stickers/releases"):
            source = paths.root / relative
            if source.exists():
                if source.is_symlink():
                    raise MaintenanceError("Immutable package root must not be a symlink")
                sources.append(source)
        sources = [source for source in sources if source.exists()]
        tag = f"project-snow-pinned-{active['commit_sha']}" if pin else BACKUP_TAG
        output = execute(["restic", "backup", "--json", "--tag", tag, "--", *map(str, sources)])
        summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        snapshots = [item.get("snapshot_id") for item in summaries if item.get("message_type") == "summary" and item.get("snapshot_id")]
        if len(snapshots) != 1:
            raise MaintenanceError("Restic did not acknowledge exactly one recovery snapshot")
        # Metadata integrity first; do not retire previous snapshots if the new
        # repository state cannot be checked. Explicit tags avoid path grouping.
        execute(["restic", "check"])
        execute(["restic", "forget", "--tag", BACKUP_TAG, "--group-by", "tags", "--keep-within", "7d", "--prune"])
        atomic_write(paths.root / "backups" / "last-success.json", (json.dumps({
            "schema_version": "project-snow-backup-success-1", "completed_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": snapshots[0], "commit_sha": active["commit_sha"], "dump_sha256": dump_hash,
        }, sort_keys=True) + "\n").encode())
        return {"status": "ok", "snapshot_id": snapshots[0], "commit_sha": active["commit_sha"], "dump_sha256": dump_hash,
                "retention": "pinned until explicitly retired" if pin else "daily 7 days"}
    finally:
        # work is created by this root process beneath a checked private parent.
        if work.parent != staging or work.is_symlink():
            raise MaintenanceError("Refusing cleanup outside the owned backup staging directory")
        shutil.rmtree(work)


def require_private_staging(staging: Path) -> None:
    info = staging.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o077:
        raise MaintenanceError("Backup staging must be a root-only directory")


def require_stopped_writers(execute: Callable[[list[str]], str] = run) -> None:
    identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
    if any(not OBJECT_ID.fullmatch(value) for value in identifiers):
        raise MaintenanceError("Unexpected production container identity")
    containers = docker_json(["inspect", *identifiers], execute) if identifiers else []
    for container in containers:
        service = ((container.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service")
        if service in WRITERS:
            raise MaintenanceError("Stop both API colours, admin and the mailer before an in-place database restore")


def restore_postgres(paths: Paths, dump: Path, expected_sha256: str, execute: Callable[[list[str]], str] = run,
                     dump_consumer: Callable[[list[str], Path], None] = consume_dump) -> dict[str, Any]:
    # Keep the historical call signature so old automation fails clearly. Even
    # stopping writers does not authorize overwriting feedback accepted since
    # a backup. Restore into an independent target and review reconciliation.
    raise MaintenanceError("In-place production restore is disabled. Use ops/restore_drill.py for an isolated target; reconcile newer feedback before any separately reviewed database switch.")
