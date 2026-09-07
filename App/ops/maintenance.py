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


def cleanup(paths: Paths, execute: Callable[[list[str]], str] = run) -> dict[str, Any]:
    release = active_release(paths)
    network = existing_data_network(execute)
    images = docker_json(["image", "inspect", release["image"]], execute)
    if not isinstance(images, list) or len(images) != 1 or not OBJECT_ID.fullmatch(str(images[0].get("Id", ""))):
        raise MaintenanceError("Active application image is not installed")
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
               "--label", "io.project-snow.maintenance=cleanup", "--read-only",
               "--user", "0:0", "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID",
               "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--mount",
               f"type=bind,source={secret},target=/run/maintenance/database_url,readonly",
               "--entrypoint", "python", release["image"], "-c", CLEANUP_CODE]
    result = json.loads(execute(command))
    deleted = result.get("deleted")
    if not isinstance(deleted, dict) or any(not isinstance(value, int) or value < 0 for value in deleted.values()):
        raise MaintenanceError("Cleanup returned invalid counters")
    return {"status": "ok", "commit_sha": release["commit_sha"], "deleted": deleted}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("cleanup")
    capacity_parser = commands.add_parser("capacity")
    capacity_parser.add_argument("--estimated-new-bytes", type=int, default=0)
    args = parser.parse_args()
    paths = Paths()
    try:
        if args.operation == "cleanup":
            if os.geteuid() != 0:
                raise MaintenanceError("Host cleanup requires the installed root-owned helper")
            with release_lock(paths.lock):
                result = cleanup(paths)
        else:
            result = capacity(paths, args.estimated_new_bytes)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MaintenanceError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not print exception messages from parsers, Docker or database code.
        message = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
        print("Project Snow maintenance failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
