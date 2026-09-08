#!/usr/bin/env python3
"""Bounded host maintenance; never reconcile a production Compose project.

Install this file root-owned at /usr/local/libexec/project-snow/maintenance.py.
Only image references, counts and resource metadata may reach its output.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from typing import Any, Callable, Iterator

GIB = 1024**3
DIGEST = re.compile(r"^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
OBJECT_ID = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
PROJECT = "project-snow-public"


class MaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/srv/project-snow")
    secrets: Path = Path("/etc/project-snow/secrets")
    lock: Path = Path("/run/lock/project-snow-release.lock")
    docker_storage: Path = Path("/var/lib/docker")


def require_regular(path: Path, *, private: bool = False) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise MaintenanceError(f"Expected a single-link regular file: {path.name}")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
        raise MaintenanceError(f"Expected a root-controlled file: {path.name}")
    if private and metadata.st_mode & 0o077:
        raise MaintenanceError(f"Expected a root-only file: {path.name}")


def read_text(path: Path) -> str:
    require_regular(path)
    if path.stat().st_size > 1024 * 1024:
        raise MaintenanceError(f"Oversized metadata: {path.name}")
    return path.read_text(encoding="utf-8")


def read_environment(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in read_text(path).splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise MaintenanceError("Invalid or duplicate release environment field")
        result[key] = value
    return result


def run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        # Docker and database diagnostics can include credential-bearing URLs.
        raise MaintenanceError(f"Maintenance command failed (exit {completed.returncode})")
    return completed.stdout


def docker_json(arguments: list[str], execute: Callable[[list[str]], str] = run) -> Any:
    try:
        return json.loads(execute(["docker", *arguments]))
    except (ValueError, TypeError) as error:
        raise MaintenanceError("Docker returned invalid metadata") from error


@contextmanager
def release_lock(path: Path) -> Iterator[None]:
    import fcntl

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_nlink != 1:
            raise MaintenanceError("Unsafe maintenance lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceError("A release or maintenance operation is already running") from error
        yield
    finally:
        os.close(descriptor)


def require_inherited_release_lock(path: Path) -> None:
    import fcntl

    inherited = os.fstat(9)
    expected = path.lstat()
    if (not stat.S_ISREG(inherited.st_mode) or inherited.st_uid != 0
            or inherited.st_nlink != 1 or (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino)):
        raise MaintenanceError("The release runner lock was not inherited")
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)


def active_release(paths: Paths) -> dict[str, str]:
    releases = paths.root / "releases"
    colour = read_text(releases / "active-colour").strip()
    marker = read_text(releases / "current").split()
    if colour not in {"blue", "green"} or len(marker) != 4 or marker[0] != colour:
        raise MaintenanceError("Active release marker is inconsistent")
    _, commit, image, embedding = marker
    if not SHA.fullmatch(commit) or not DIGEST.fullmatch(image) or not DIGEST.fullmatch(embedding):
        raise MaintenanceError("Active release must contain immutable identities")
    manifest = json.loads(read_text(releases / "current-manifest.json"))
    manifest_image = f"{manifest.get('application', {}).get('image', '')}@{manifest.get('application', {}).get('digest', '')}"
    environment = read_environment(paths.root / "runtime" / "compose.env")
    if manifest.get("commit_sha") != commit or manifest_image != image or environment.get("PUBLIC_API_IMAGE") != image:
        raise MaintenanceError("Active manifest, marker and environment disagree")
    return {"colour": colour, "commit_sha": commit, "image": image}


def existing_data_network(execute: Callable[[list[str]], str] = run) -> str:
    networks = docker_json(["network", "inspect", f"{PROJECT}_data"], execute)
    if not isinstance(networks, list) or len(networks) != 1:
        raise MaintenanceError("Expected exactly one existing data network")
    network = networks[0]
    labels = network.get("Labels") or {}
    identifier = str(network.get("Id", ""))
    if (not OBJECT_ID.fullmatch(identifier) or network.get("Name") != f"{PROJECT}_data"
            or network.get("Driver") != "bridge" or network.get("Internal") is not True
            or labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.network") != "data"):
        raise MaintenanceError("Existing data network does not match the production boundary")
    return identifier


CLEANUP_CODE = """import json, os, sys
from pathlib import Path
try:
    database_url = Path('/run/maintenance/database_url').read_text().strip()
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
    from backend.snow_app.public_store import PublicStore
    deleted = PublicStore(database_url).cleanup()
    del database_url
    print(json.dumps({'deleted': {str(k): int(v) for k, v in deleted.items()}}, sort_keys=True))
except Exception as error:
    print('Retention cleanup failed: ' + type(error).__name__, file=sys.stderr)
    sys.exit(1)
"""


RECOVER_REQUESTS_CODE = """import json, os, sys, time
from pathlib import Path
try:
    database_url = Path('/run/maintenance/database_url').read_text().strip()
    os.setgroups([]); os.setgid(10001); os.setuid(10001)
    from backend.snow_app.public_store import PublicStore
    from sqlalchemy import text
    store = PublicStore(database_url)
    del database_url
    recovered = 0
    deadline = time.monotonic() + 46
    while True:
        with store.begin() as connection:
            present = connection.execute(text("SELECT to_regclass('public_request_leases')")).scalar()
            pending = int(connection.execute(text('SELECT count(*) FROM public_request_leases')).scalar()) if present else 0
        if not pending:
            break
        recover = getattr(store, 'recover_expired_requests', None)
        if recover is None:
            raise RuntimeError('A trusted lease-aware recovery image is required')
        recovered += recover()
        with store.begin() as connection:
            pending = int(connection.execute(text('SELECT count(*) FROM public_request_leases')).scalar())
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Live request leases remain; recovery was not forced')
        time.sleep(1)
    print(json.dumps({'recovered': recovered, 'remaining_leases': 0}))
except Exception as error:
    print('Request recovery failed: ' + type(error).__name__, file=sys.stderr)
    sys.exit(1)
"""


def database_job(paths: Paths, execute: Callable, *, require_running_api: bool, operation: str, code: str) -> tuple[dict, dict]:
    release = active_release(paths)
    network = existing_data_network(execute)
    images = docker_json(["image", "inspect", release["image"]], execute)
    if not isinstance(images, list) or len(images) != 1 or not OBJECT_ID.fullmatch(str(images[0].get("Id", ""))):
        raise MaintenanceError("Active application image is not installed")
    if require_running_api:
        identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                               "--filter", f"label=com.docker.compose.service=public-api-{release['colour']}"]).split()
        if len(identifiers) != 1 or not OBJECT_ID.fullmatch(identifiers[0]):
            raise MaintenanceError("Expected exactly one running active API")
        containers = docker_json(["inspect", identifiers[0]], execute)
        if len(containers) != 1 or containers[0].get("Image") != images[0]["Id"]:
            raise MaintenanceError("Running API does not match the promoted image")
    secret = paths.secrets / "public_database_url"
    require_regular(secret, private=True)
    command = ["docker", "run", "--rm", "--pull=never", "--network", network,
               "--label", f"io.project-snow.maintenance={operation}", "--read-only",
               "--user", "0:0", "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID",
               "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--mount",
               f"type=bind,source={secret},target=/run/maintenance/database_url,readonly",
               "--entrypoint", "python", release["image"], "-c", code]
    result = json.loads(execute(command))
    return release, result


def cleanup(paths: Paths, execute: Callable[[list[str]], str] = run, *, require_running_api: bool = True) -> dict[str, Any]:
    release, result = database_job(paths, execute, require_running_api=require_running_api, operation="cleanup", code=CLEANUP_CODE)
    deleted = result.get("deleted")
    if not isinstance(deleted, dict) or any(not isinstance(value, int) or value < 0 for value in deleted.values()):
        raise MaintenanceError("Cleanup returned invalid counters")
    return {"status": "ok", "commit_sha": release["commit_sha"], "deleted": deleted}


def recover_requests(paths: Paths, execute: Callable[[list[str]], str] = run) -> dict[str, Any]:
    release = active_release(paths)
    identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                           "--filter", f"label=com.docker.compose.service=public-api-{release['colour']}"]).split()
    if identifiers:
        raise MaintenanceError("Stop and drain the previously active API before lease recovery")
    _, result = database_job(paths, execute, require_running_api=False, operation="recover-requests", code=RECOVER_REQUESTS_CODE)
    if type(result.get("recovered")) is not int or result["recovered"] < 0 or result.get("remaining_leases") != 0:
        raise MaintenanceError("Request recovery did not establish terminal results for all previous leases")
    return {"status": "ok", "commit_sha": release["commit_sha"], **result}


def capacity(paths: Paths, estimated_new_bytes: int, disk_usage: Callable = shutil.disk_usage) -> dict[str, Any]:
    if estimated_new_bytes < 0:
        raise MaintenanceError("Estimated new bytes must be nonnegative")
    minimum = max(10 * GIB, 2 * estimated_new_bytes)
    filesystems = []
    for path in (paths.root, paths.docker_storage):
        usage = disk_usage(path)
        filesystems.append({"path": str(path), "free_bytes": usage.free, "required_free_bytes": minimum})
    if any(item["free_bytes"] < minimum for item in filesystems):
        raise MaintenanceError(f"Insufficient capacity: require {minimum} free bytes on application and Docker filesystems")
    return {"status": "ok", "estimated_new_bytes": estimated_new_bytes, "filesystems": filesystems}


def estimate_release_growth(paths: Paths, manifest_path: Path) -> int:
    manifest = json.loads(read_text(manifest_path))
    # Reserve a bounded application-image/build-layer allowance in addition to
    # all uploaded archive members. Shared model image upgrades are a separate
    # maintenance operation and are rejected by ordinary stage.
    estimated = 2 * GIB
    for kind, field in (("data", "data_version"), ("avatar", "media_version"), ("sticker", "sticker_version")):
        version = str(manifest.get(field, ""))
        if not re.fullmatch(r"[0-9A-Za-z._-]+", version):
            raise MaintenanceError("Invalid release package version")
        archive = paths.root / "inbox" / f"{kind}-{version}.tar"
        if archive.exists() or archive.is_symlink():
            info = archive.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise MaintenanceError("Unsafe release archive during capacity check")
            # The controlled client creates uncompressed tar files. Explicitly
            # rejecting compression avoids an unbounded decompression estimate.
            with tarfile.open(archive, mode="r:") as package:
                total = 0
                for count, member in enumerate(package, 1):
                    if count > 100000 or member.size < 0:
                        raise MaintenanceError("Release archive exceeds capacity inspection bounds")
                    total += member.size
            estimated += max(info.st_size, total)
    return estimated


SHARED_IMAGE_KEYS = ("POSTGRES_IMAGE", "QDRANT_IMAGE", "NEO4J_IMAGE", "EMBEDDING_IMAGE", "EGRESS_PROXY_IMAGE")
SHARED_CONFIG_PATHS = ("infra/postgres/postgresql.conf", "infra/neo4j-entrypoint.sh", "infra/egress-squid.conf")


def shared_dependency_gate(paths: Paths, candidate_environment: Path, candidate_manifest: Path) -> dict[str, Any]:
    current = read_environment(paths.root / "runtime" / "compose.env")
    candidate = read_environment(candidate_environment)
    changed = [key for key in SHARED_IMAGE_KEYS if not DIGEST.fullmatch(current.get(key, ""))
               or candidate.get(key) != current[key]]
    previous_binding = json.loads(read_text(paths.root / "releases" / "current-config.json"))
    next_manifest = json.loads(read_text(candidate_manifest))
    previous_hashes = previous_binding.get("configuration_sha256") or {}
    next_hashes = next_manifest.get("configuration_sha256") or {}
    changed.extend(path for path in SHARED_CONFIG_PATHS if not re.fullmatch(r"[0-9a-f]{64}", str(previous_hashes.get(path, "")))
                   or next_hashes.get(path) != previous_hashes[path])
    if changed:
        raise MaintenanceError("Shared dependency changes require an independent backed-up maintenance release: " + ", ".join(changed))
    return {"status": "ok", "shared_dependencies": "unchanged"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("cleanup")
    recovery_parser = commands.add_parser("recover-requests")
    recovery_parser.add_argument("--lock-held", action="store_true")
    capacity_parser = commands.add_parser("capacity")
    capacity_parser.add_argument("--estimated-new-bytes", type=int, default=0)
    capacity_parser.add_argument("--manifest", type=Path)
    stage_parser = commands.add_parser("shared-dependency-gate")
    stage_parser.add_argument("--candidate-environment", type=Path, required=True)
    stage_parser.add_argument("--manifest", type=Path, required=True)
    anchor_parser = commands.add_parser("anchor")
    anchor_parser.add_argument("--lock-held", action="store_true")
    pin_parser = commands.add_parser("pin-recovery-env")
    pin_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    pin_parser.add_argument("--data-root", type=Path, required=True)
    pin_parser.add_argument("--lock-held", action="store_true")
    restore_parser = commands.add_parser("restore-anchor")
    restore_parser.add_argument("--release-id", required=True)
    restore_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    commands.add_parser("gc-plan")
    gc_parser = commands.add_parser("gc-remove")
    gc_parser.add_argument("--image-id", action="append", required=True)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--pin", action="store_true", help="Keep a baseline snapshot outside daily retention")
    auto_parser = commands.add_parser("auto-stage")
    auto_parser.add_argument("--retry", action="store_true")
    commands.add_parser("monitor")
    restore_db_parser = commands.add_parser("restore-postgres")
    restore_db_parser.add_argument("--dump", type=Path, required=True)
    restore_db_parser.add_argument("--sha256", required=True)
    record_parser = commands.add_parser("candidate-record")
    record_parser.add_argument("--manifest", type=Path, required=True)
    record_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    record_parser.add_argument("--lock-held", action="store_true")
    for operation in ("candidate-complete", "discard-candidate"):
        candidate_parser = commands.add_parser(operation)
        candidate_parser.add_argument("--sha", required=True)
        candidate_parser.add_argument("--lock-held", action="store_true")
    args = parser.parse_args()
    paths = Paths()
    try:
        if args.operation == "recover-requests":
            if os.geteuid() != 0:
                raise MaintenanceError("Request recovery requires root")
            if args.lock_held:
                require_inherited_release_lock(paths.lock)
                result = recover_requests(paths)
            else:
                with release_lock(paths.lock):
                    result = recover_requests(paths)
        elif args.operation == "cleanup":
            if os.geteuid() != 0:
                raise MaintenanceError("Host cleanup requires the installed root-owned helper")
            with release_lock(paths.lock):
                result = cleanup(paths)
        elif args.operation == "capacity":
            estimated = max(args.estimated_new_bytes, estimate_release_growth(paths, args.manifest) if args.manifest else 0)
            result = capacity(paths, estimated)
        elif args.operation == "shared-dependency-gate":
            result = shared_dependency_gate(paths, args.candidate_environment, args.manifest)
        elif args.operation == "monitor":
            from monitor import monitor

            if os.geteuid() != 0:
                raise MaintenanceError("Host monitoring requires root")
            with release_lock(paths.lock.with_name("project-snow-monitor.lock")):
                result = monitor(paths)
            if result["status"] == "unchanged":
                return 0
        elif args.operation == "auto-stage":
            from auto_stage import auto_stage

            if os.geteuid() != 0:
                raise MaintenanceError("Candidate pull requires root")
            with release_lock(paths.lock.with_name("project-snow-auto-stage.lock")):
                result = auto_stage(paths, retry=args.retry)
        elif args.operation in {"backup", "restore-postgres"}:
            from recovery_backup import backup, restore_postgres

            if os.geteuid() != 0:
                raise MaintenanceError("Database recovery requires root")
            with release_lock(paths.lock):
                result = backup(paths, pin=args.pin) if args.operation == "backup" else restore_postgres(paths, args.dump, args.sha256)
        else:
            from release_state import (
                archive_colours,
                delete_planned_images,
                discard_candidate,
                image_gc_plan,
                pin_recovery_environment,
                record_candidate,
                restore_colour,
            )

            if os.geteuid() != 0:
                raise MaintenanceError("Release maintenance requires root")
            if getattr(args, "lock_held", False):
                require_inherited_release_lock(paths.lock)
                if args.operation == "anchor":
                    result = archive_colours(paths)
                elif args.operation == "pin-recovery-env":
                    result = pin_recovery_environment(paths, args.colour, args.data_root)
                elif args.operation == "candidate-record":
                    result = record_candidate(paths, args.colour, args.manifest)
                else:
                    result = discard_candidate(paths, args.sha, promoted=args.operation == "candidate-complete")
            else:
                with release_lock(paths.lock):
                    if args.operation == "anchor":
                        result = archive_colours(paths)
                    elif args.operation == "pin-recovery-env":
                        result = pin_recovery_environment(paths, args.colour, args.data_root)
                    elif args.operation == "restore-anchor":
                        result = restore_colour(paths, args.release_id, args.colour)
                    elif args.operation == "gc-plan":
                        result = image_gc_plan(paths)
                    elif args.operation == "candidate-record":
                        result = record_candidate(paths, args.colour, args.manifest)
                    elif args.operation in {"candidate-complete", "discard-candidate"}:
                        result = discard_candidate(paths, args.sha, promoted=args.operation == "candidate-complete")
                    else:
                        result = delete_planned_images(paths, args.image_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MaintenanceError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        # Do not print exception messages from parsers, Docker or database code.
        message = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
        print("Project Snow maintenance failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
