#!/usr/bin/env python3
"""Bounded host maintenance; never reconcile a production Compose project.

Install this file root-owned at /usr/local/libexec/project-snow/maintenance.py.
Only image references, counts and resource metadata may reach its output.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/srv/project-snow")
    secrets: Path = Path("/etc/project-snow/secrets")
    lock: Path = Path("/run/lock/project-snow-release.lock")
    docker_storage: Path = Path("/var/lib/docker")
    embedding_swap: Path = Path("/etc/project-snow/embedding-swap")
    egress_swap: Path = Path("/etc/project-snow/egress-swap")


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

EMBEDDING_SWAP_SCHEMA = "project-snow-embedding-swap-1"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _candidate_embedding_digest(candidate_embedding: str) -> str:
    """Return the 64-hex digest of an embedding image reference.

    The authorization file is named by the destination digest so a single
    directory holds at most one authorization per target image.
    """
    if not DIGEST.fullmatch(candidate_embedding):
        raise MaintenanceError("Candidate embedding reference is not an immutable digest")
    return candidate_embedding.rsplit(":", 1)[1]


def _require_private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or info.st_mode & 0o077):
        raise MaintenanceError("Embedding-swap authorization directory is not root-only")


def embedding_swap_authorization(paths: Paths, current: str, candidate: str) -> dict[str, str] | None:
    """Return the root-only authorization iff it names exactly current -> candidate.

    A missing directory or file means "not authorized" and returns None; any
    present-but-inconsistent file (wrong ownership/mode, wrong schema, mutable
    or mismatched refs, bad receipt pointer) raises so a swap never proceeds on
    a malformed grant. The file is pinned to the destination digest by name.
    """
    directory = paths.embedding_swap
    if not directory.exists() and not directory.is_symlink():
        return None
    _require_private_directory(directory)
    path = directory / f"{_candidate_embedding_digest(candidate)}.env"
    if not path.exists() and not path.is_symlink():
        return None
    require_regular(path, private=True)
    fields: dict[str, str] = {}
    for line in read_text(path).splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in fields:
            raise MaintenanceError("Invalid or duplicate embedding-swap authorization field")
        fields[key] = value
    if set(fields) != {"schema", "from", "to", "approved_at", "probe_sha256"}:
        raise MaintenanceError("Embedding-swap authorization has an unexpected field set")
    if fields["schema"] != EMBEDDING_SWAP_SCHEMA:
        raise MaintenanceError("Embedding-swap authorization schema is unknown")
    if not DIGEST.fullmatch(fields["from"]) or not DIGEST.fullmatch(fields["to"]):
        raise MaintenanceError("Embedding-swap authorization refs are not immutable digests")
    if not SHA256_HEX.fullmatch(fields["probe_sha256"]):
        raise MaintenanceError("Embedding-swap authorization has an invalid probe receipt hash")
    if fields["from"] != current or fields["to"] != candidate:
        raise MaintenanceError("Embedding-swap authorization does not match the running and candidate digests")
    return fields


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
    # A root-authorized, probe-backed embedding swap is the one shared change
    # ordinary stage may accept, and only when it is the ONLY changed item.
    if changed == ["EMBEDDING_IMAGE"]:
        authorization = embedding_swap_authorization(paths, current["EMBEDDING_IMAGE"], candidate.get("EMBEDDING_IMAGE", ""))
        if authorization is not None:
            return {"status": "ok", "shared_dependencies": "embedding-swap-authorized",
                    "from": authorization["from"], "to": authorization["to"]}
    # The egress squid conf is the one shared CONFIG change ordinary stage may
    # accept, mirroring the embedding image swap and only when it is the ONLY
    # changed item. Keyed on the conf's content hash (from->to).
    if changed == [EGRESS_SWAP_PATH]:
        egress_authorization = egress_swap_authorization(
            paths,
            str(previous_hashes.get(EGRESS_SWAP_PATH, "")),
            str(next_hashes.get(EGRESS_SWAP_PATH, "")),
        )
        if egress_authorization is not None:
            return {"status": "ok", "shared_dependencies": "egress-config-swap-authorized",
                    "from": egress_authorization["from_sha256"], "to": egress_authorization["to_sha256"]}
    if changed:
        raise MaintenanceError("Shared dependency changes require an independent backed-up maintenance release: " + ", ".join(changed))
    return {"status": "ok", "shared_dependencies": "unchanged"}


def _require_probe_receipt(path: Path, current: str, candidate: str) -> str:
    """Validate a root-only probe receipt for exactly current -> candidate.

    Returns the sha256 of the receipt bytes, which the authorization pins so a
    later edited receipt cannot be presented as the basis of an existing grant.
    """
    require_regular(path, private=True)
    payload = path.read_bytes()
    if len(payload) > 1024 * 1024:
        raise MaintenanceError("Oversized probe receipt")
    receipt = json.loads(payload)
    if not isinstance(receipt, dict):
        raise MaintenanceError("Probe receipt is not an object")
    if receipt.get("schema") != "project-snow-embedding-probe-1":
        raise MaintenanceError("Probe receipt schema is unknown")
    if receipt.get("status") != "pass" or receipt.get("passed") is not True:
        raise MaintenanceError("Probe receipt does not record a passing comparison")
    if receipt.get("from") != current or receipt.get("to") != candidate:
        raise MaintenanceError("Probe receipt is for different embedding digests")
    return hashlib.sha256(payload).hexdigest()


def embedding_swap_authorize(paths: Paths, from_ref: str, to_ref: str, probe_receipt: Path) -> dict[str, Any]:
    if not DIGEST.fullmatch(from_ref) or not DIGEST.fullmatch(to_ref):
        raise MaintenanceError("Both embedding references must be immutable digests")
    if from_ref == to_ref:
        raise MaintenanceError("The embedding swap source and destination are identical")
    current = active_release(paths)
    running = read_environment(paths.root / "runtime" / "compose.env").get("EMBEDDING_IMAGE", "")
    if running != from_ref:
        raise MaintenanceError("The authorization source must equal the running embedding digest")
    del current
    receipt_sha = _require_probe_receipt(probe_receipt, from_ref, to_ref)
    directory = paths.embedding_swap
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(directory)
    path = directory / f"{_candidate_embedding_digest(to_ref)}.env"
    payload = (
        f"schema={EMBEDDING_SWAP_SCHEMA}\n"
        f"from={from_ref}\n"
        f"to={to_ref}\n"
        f"approved_at={_now_iso()}\n"
        f"probe_sha256={receipt_sha}\n"
    ).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"status": "authorized", "from": from_ref, "to": to_ref, "probe_sha256": receipt_sha}


def embedding_swap_clear(paths: Paths) -> dict[str, Any]:
    directory = paths.embedding_swap
    if not directory.exists() and not directory.is_symlink():
        return {"status": "absent"}
    _require_private_directory(directory)
    removed = []
    for child in sorted(directory.iterdir()):
        if child.is_symlink() or not child.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.env", child.name):
            raise MaintenanceError("Unexpected entry in the embedding-swap authorization directory")
        child.unlink()
        removed.append(child.name)
    return {"status": "cleared", "removed": removed}


# ---------------------------------------------------------------------------
# Egress-config swap: the single shared-config change ordinary stage may accept,
# mirroring the embedding image swap. The one shared path it governs is
# infra/egress-squid.conf; the authorization is a root-only file named by the
# candidate conf's sha256 and pinned to the exact from->to content hashes.
# ---------------------------------------------------------------------------
EGRESS_SWAP_SCHEMA = "project-snow-egress-swap-1"
EGRESS_SWAP_PATH = "infra/egress-squid.conf"


def _require_egress_private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or info.st_mode & 0o077):
        raise MaintenanceError("Egress-swap authorization directory is not root-only")


def _config_hashes(binding_or_manifest: Path) -> dict[str, str]:
    document = json.loads(read_text(binding_or_manifest))
    hashes = document.get("configuration_sha256") or {}
    if not isinstance(hashes, dict):
        raise MaintenanceError("configuration_sha256 is not an object")
    return hashes


def egress_swap_authorization(paths: Paths, current: str, candidate: str) -> dict[str, str] | None:
    """Return the root-only authorization iff it names exactly current -> candidate.

    ``current``/``candidate`` are the sha256 of the egress conf as recorded in
    the live config binding and the candidate manifest. Semantics mirror
    ``embedding_swap_authorization``: missing => None; present-but-inconsistent
    => raise so a swap never proceeds on a malformed grant.
    """
    directory = paths.egress_swap
    if not directory.exists() and not directory.is_symlink():
        return None
    _require_egress_private_directory(directory)
    if not SHA256_HEX.fullmatch(candidate):
        raise MaintenanceError("Candidate egress configuration hash is invalid")
    path = directory / f"{candidate}.env"
    if not path.exists() and not path.is_symlink():
        return None
    require_regular(path, private=True)
    fields: dict[str, str] = {}
    for line in read_text(path).splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in fields:
            raise MaintenanceError("Invalid or duplicate egress-swap authorization field")
        fields[key] = value
    if set(fields) != {"schema", "from_sha256", "to_sha256", "approved_at", "parse_ok"}:
        raise MaintenanceError("Egress-swap authorization has an unexpected field set")
    if fields["schema"] != EGRESS_SWAP_SCHEMA:
        raise MaintenanceError("Egress-swap authorization schema is unknown")
    if not SHA256_HEX.fullmatch(fields["from_sha256"]) or not SHA256_HEX.fullmatch(fields["to_sha256"]):
        raise MaintenanceError("Egress-swap authorization hashes are invalid")
    if fields["parse_ok"] != "true":
        raise MaintenanceError("Egress-swap authorization does not record a passing squid parse")
    if fields["from_sha256"] != current or fields["to_sha256"] != candidate:
        raise MaintenanceError("Egress-swap authorization does not match the running and candidate conf hashes")
    return fields


def egress_swap_authorize(paths: Paths, candidate_manifest: Path, *, parse_ok: bool) -> dict[str, Any]:
    """Record a root-only authorization for the running -> candidate egress conf.

    ``parse_ok`` must be True: the caller is expected to have run ``squid -k
    parse`` against the candidate conf first, mirroring the embedding probe
    receipt. The current hash is read from the live config binding, the
    candidate hash from the candidate manifest.
    """
    if not parse_ok:
        raise MaintenanceError("The candidate egress configuration must pass squid -k parse first")
    current_hashes = _config_hashes(paths.root / "releases" / "current-config.json")
    candidate_hashes = _config_hashes(candidate_manifest)
    current = str(current_hashes.get(EGRESS_SWAP_PATH, ""))
    candidate = str(candidate_hashes.get(EGRESS_SWAP_PATH, ""))
    if not SHA256_HEX.fullmatch(current) or not SHA256_HEX.fullmatch(candidate):
        raise MaintenanceError("Both egress configuration hashes must be present and valid")
    if current == candidate:
        raise MaintenanceError("The egress swap source and destination are identical")
    directory = paths.egress_swap
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_egress_private_directory(directory)
    path = directory / f"{candidate}.env"
    payload = (
        f"schema={EGRESS_SWAP_SCHEMA}\n"
        f"from_sha256={current}\n"
        f"to_sha256={candidate}\n"
        f"approved_at={_now_iso()}\n"
        f"parse_ok=true\n"
    ).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"status": "authorized", "from_sha256": current, "to_sha256": candidate}


def egress_swap_clear(paths: Paths) -> dict[str, Any]:
    directory = paths.egress_swap
    if not directory.exists() and not directory.is_symlink():
        return {"status": "absent"}
    _require_egress_private_directory(directory)
    removed = []
    for child in sorted(directory.iterdir()):
        if child.is_symlink() or not child.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.env", child.name):
            raise MaintenanceError("Unexpected entry in the egress-swap authorization directory")
        child.unlink()
        removed.append(child.name)
    return {"status": "cleared", "removed": removed}


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
    authorize_parser = commands.add_parser("embedding-swap-authorize")
    authorize_parser.add_argument("--from", dest="from_ref", required=True)
    authorize_parser.add_argument("--to", dest="to_ref", required=True)
    authorize_parser.add_argument("--probe-receipt", type=Path, required=True)
    authorize_parser.add_argument("--lock-held", action="store_true")
    clear_parser = commands.add_parser("embedding-swap-clear")
    clear_parser.add_argument("--lock-held", action="store_true")
    egress_authorize_parser = commands.add_parser("egress-config-swap-authorize")
    egress_authorize_parser.add_argument("--manifest", type=Path, required=True)
    egress_authorize_parser.add_argument("--parse-ok", action="store_true")
    egress_authorize_parser.add_argument("--lock-held", action="store_true")
    egress_clear_parser = commands.add_parser("egress-config-swap-clear")
    egress_clear_parser.add_argument("--lock-held", action="store_true")
    anchor_parser = commands.add_parser("anchor")
    anchor_parser.add_argument("--lock-held", action="store_true")
    pin_parser = commands.add_parser("pin-recovery-env")
    pin_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    pin_parser.add_argument("--data-root", type=Path, required=True)
    pin_parser.add_argument("--lock-held", action="store_true")
    restore_parser = commands.add_parser("restore-anchor")
    restore_parser.add_argument("--release-id", required=True)
    restore_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    baseline_parser = commands.add_parser("baseline-mark")
    baseline_parser.add_argument("--release-id", required=True)
    baseline_parser.add_argument("--lock-held", action="store_true")
    commands.add_parser("baseline-list")
    commands.add_parser("retire-plan")
    for retire_operation in ("anchor-retire", "anchor-unretire"):
        retire_parser = commands.add_parser(retire_operation)
        retire_parser.add_argument("--release-id", required=True)
        retire_parser.add_argument("--lock-held", action="store_true")
    origin_retire_parser = commands.add_parser("origin-edge-retire")
    origin_retire_parser.add_argument("--lock-held", action="store_true")
    origin_unretire_parser = commands.add_parser("origin-edge-unretire")
    origin_unretire_parser.add_argument("--name", required=True)
    origin_unretire_parser.add_argument("--lock-held", action="store_true")
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
        elif args.operation in {"embedding-swap-authorize", "embedding-swap-clear"}:
            if os.geteuid() != 0:
                raise MaintenanceError("Embedding-swap authorization requires root")

            def _run_embedding_swap() -> dict[str, Any]:
                if args.operation == "embedding-swap-authorize":
                    return embedding_swap_authorize(paths, args.from_ref, args.to_ref, args.probe_receipt)
                return embedding_swap_clear(paths)

            if args.lock_held:
                require_inherited_release_lock(paths.lock)
                result = _run_embedding_swap()
            else:
                with release_lock(paths.lock):
                    result = _run_embedding_swap()
        elif args.operation in {"egress-config-swap-authorize", "egress-config-swap-clear"}:
            if os.geteuid() != 0:
                raise MaintenanceError("Egress-config-swap authorization requires root")

            def _run_egress_swap() -> dict[str, Any]:
                if args.operation == "egress-config-swap-authorize":
                    return egress_swap_authorize(paths, args.manifest, parse_ok=args.parse_ok)
                return egress_swap_clear(paths)

            if args.lock_held:
                require_inherited_release_lock(paths.lock)
                result = _run_egress_swap()
            else:
                with release_lock(paths.lock):
                    result = _run_egress_swap()
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
                anchor_retire,
                anchor_unretire,
                archive_colours,
                baseline_mark,
                baseline_registry,
                delete_planned_images,
                discard_candidate,
                image_gc_plan,
                origin_edge_snapshot_retire,
                origin_edge_snapshot_unretire,
                pin_recovery_environment,
                record_candidate,
                restore_colour,
                retire_plan,
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
                elif args.operation == "baseline-mark":
                    result = baseline_mark(paths, args.release_id)
                elif args.operation == "anchor-retire":
                    result = anchor_retire(paths, args.release_id)
                elif args.operation == "anchor-unretire":
                    result = anchor_unretire(paths, args.release_id)
                elif args.operation == "origin-edge-retire":
                    result = origin_edge_snapshot_retire(paths)
                elif args.operation == "origin-edge-unretire":
                    result = origin_edge_snapshot_unretire(paths, args.name)
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
                    elif args.operation == "baseline-mark":
                        result = baseline_mark(paths, args.release_id)
                    elif args.operation == "baseline-list":
                        result = baseline_registry(paths)
                    elif args.operation == "retire-plan":
                        result = retire_plan(paths)
                    elif args.operation == "anchor-retire":
                        result = anchor_retire(paths, args.release_id)
                    elif args.operation == "anchor-unretire":
                        result = anchor_unretire(paths, args.release_id)
                    elif args.operation == "origin-edge-retire":
                        result = origin_edge_snapshot_retire(paths)
                    elif args.operation == "origin-edge-unretire":
                        result = origin_edge_snapshot_unretire(paths, args.name)
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
