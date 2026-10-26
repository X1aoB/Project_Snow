"""Immutable recovery anchors and conservative, cross-project image inventory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

from maintenance import (DIGEST, OBJECT_ID, SHA, SHARED_IMAGE_KEYS, MaintenanceError,
                         Paths, active_release, docker_json, read_environment, read_text, require_regular, run)

ANCHOR_ID = re.compile(r"^[0-9a-f]{40}-[0-9a-f]{64}$")
SNOW_REPOSITORIES = {"ghcr.io/x1aob/project_snow-public", "ghcr.io/x1aob/project_snow-embedding"}
# x.Y.0 feature-release version, the only app_version a baseline may carry
# (see the owner version rule), plus the grandfathered 1.0.0 baseline below.
FEATURE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.0$")
# Baseline #1: the last 1.0.0 release. PUBLIC_APP_VERSION stayed 1.0.0 across
# many builds since 2026-09-09, so its manifest app_version is 1.0.0 (a z.0
# release by the feature rule only accidentally) and this specific release id
# is grandfathered: baseline-mark accepts 1.0.0 only for exactly this id.
GRANDFATHERED_BASELINE_ID = "0c27109f1b02fbbcb409366f5fb1bbf7c850d2d7-6dcfa1b28547eb8d875b6912f9b27b1e4b0745e77f3ad5ecea325e96dc4c7681"
MAX_ACTIVE_BASELINES = 2
BASELINE_REGISTRY_SCHEMA = "project-snow-baseline-registry-1"
# Immutable origin-edge listener snapshots, named by their own sha256
# (promote.sh persist_live_origin_edge_binding). The origin-edge service runs only
# ${CADDY_IMAGE} (compose.prod.yml), and every compose command promote.sh runs
# with such a snapshot targets origin-edge alone, so only that key pins an image.
ORIGIN_EDGE_SNAPSHOT = re.compile(r"^[0-9a-f]{64}\.compose\.env$")
ORIGIN_EDGE_PROTECTED_KEYS = ("CADDY_IMAGE",)
LIVE_ORIGIN_EDGE_SCHEMA = "project-snow-live-origin-edge-1"
LEGACY_BASELINE_ID = "baseline-502ec99-0.9.6-20260907"
LEGACY_BASELINE_SHA = "502ec99412bef843c37e4b31a53df8fa9faeb33c"
LEGACY_BASELINE_FILES = frozenset({
    "active-colour", "current", "containers.json", "current-config.json", "current-manifest.json",
})
# Frozen configuration projection of the protected 0.9.6 release, not the
# current application's configuration schema. None of these paths hold secrets.
LEGACY_BASELINE_CONFIGURATION = frozenset({
    "compose.prod.yml", "infra/Caddyfile", "infra/OriginEdge.Caddyfile",
    "config/origin-edge/origin-cert.pem", "config/origin-edge/aop-ca.pem",
    "scripts/install_origin_tls.py", "scripts/cloudflare_origin_firewall.py",
    "ops/project-snow-origin-firewall.service", "ops/project-snow-origin-firewall.timer",
    "infra/egress-squid.conf", "infra/neo4j-entrypoint.sh", "infra/postgres/postgresql.conf",
    "infra/public-api.Dockerfile", "requirements-public.txt",
})


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _controlled_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
        raise MaintenanceError("Recovery directory is not root-controlled")


def pin_recovery_environment(paths: Paths, colour: str, data_root: Path) -> dict[str, str]:
    """Pin active recovery settings atomically; keep already-correct bytes intact.

    The caller holds the release lock. A candidate must never truncate the
    active colour's recovery file while preparing its own independent slot.
    """
    active = active_release(paths)
    if colour != active["colour"]:
        raise MaintenanceError("Recovery settings must belong to the active colour")
    manifest = json.loads(read_text(paths.root / "releases" / "current-manifest.json"))
    version = manifest.get("data_version", "")
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z._-]+", version)
            or version in {".", ".."} or data_root != paths.root / "data" / "releases" / version):
        raise MaintenanceError("Recovery data path differs from the active release")
    for parent in (data_root, *data_root.parents):
        _controlled_directory(parent)
    if not data_root.is_dir() or data_root.is_symlink():
        raise MaintenanceError("Recovery data directory is missing or linked")
    data_manifest = json.loads(read_text(data_root / "manifest.json"))
    if data_manifest.get("data_version") != version:
        raise MaintenanceError("Recovery data manifest differs from the active release")
    path = paths.root / "runtime" / "colours" / f"{colour}.compose.env"
    for parent in (path.parent, *path.parent.parents):
        _controlled_directory(parent)
    require_regular(path, private=True)
    values = read_environment(path)
    if values.get("PUBLIC_API_IMAGE") != active["image"]:
        raise MaintenanceError("Recovery application image differs from the active release")
    desired = {
        "PUBLIC_DATA_ROOT": str(data_root),
        "PUBLIC_MAILER_ENV_FILE": "/etc/project-snow/feedback-mailer.env",
    }
    if all(values.get(key) == value for key, value in desired.items()):
        # A previous replace may have succeeded before its directory fsync
        # failed. Retrying still establishes durability without rewriting bytes.
        sync(path.parent)
        return {"status": "unchanged", "colour": colour}
    original = path.read_bytes()
    # Preserve all unrelated line bytes; the strict environment reader above
    # already rejected duplicate or malformed fields.
    replaced_keys = {key.encode() for key in desired}
    lines = [line for line in original.splitlines(keepends=True) if line.split(b"=", 1)[0] not in replaced_keys]
    payload = b"".join(lines)
    if payload and not payload.endswith(b"\n"):
        payload += b"\n"
    payload += "".join(f"{key}={value}\n" for key, value in desired.items()).encode()
    atomic_write(path, payload)
    return {"status": "updated", "colour": colour}


def verify_anchor(paths: Paths, identifier: str) -> tuple[Path, dict[str, Any]]:
    if not ANCHOR_ID.fullmatch(identifier):
        raise MaintenanceError("Invalid immutable recovery anchor ID")
    root = paths.root / "releases" / "anchors" / identifier
    _controlled_directory(root)
    metadata = json.loads(read_text(root / "anchor.json"))
    if metadata.get("schema_version") != "project-snow-anchor-1" or metadata.get("release_id") != identifier:
        raise MaintenanceError("Recovery anchor identity mismatch")
    files = metadata.get("files")
    if not isinstance(files, dict) or set(files) - {"marker", "manifest.json", "config.json", "compose.env", "public.env", "mailer.env"}:
        raise MaintenanceError("Recovery anchor contains an unexpected file set")
    if not {"marker", "manifest.json", "config.json", "compose.env"} <= set(files):
        raise MaintenanceError("Recovery anchor is incomplete")
    observed = {child.name for child in root.iterdir()}
    if observed != set(files) | {"anchor.json"}:
        raise MaintenanceError("Recovery anchor directory contains unexpected entries")
    for name, expected in files.items():
        payload = read_text(root / name).encode("utf-8")
        if digest(payload) != expected:
            raise MaintenanceError("Recovery anchor file hash mismatch")
    identity = digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
    if identifier != f"{metadata.get('commit_sha')}-{identity}":
        raise MaintenanceError("Recovery anchor content identity mismatch")
    return root, metadata


def _strict_json(path: Path) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MaintenanceError("Legacy baseline contains duplicate JSON fields")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise MaintenanceError("Legacy baseline contains invalid JSON constants")

    try:
        return json.loads(read_text(path), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError("Legacy baseline contains malformed JSON") from exc


def verify_legacy_baseline(paths: Paths) -> set[str]:
    """Read only the named, version-bound 0.9.6 snapshot; never convert it.

    Every captured container image is retained, including historical and foreign
    application images. Unknown anchor formats continue to disable collection.
    """
    root = paths.root / "releases" / "anchors" / LEGACY_BASELINE_ID
    for parent in (root, *root.parents):
        _controlled_directory(parent)
    if {child.name for child in root.iterdir()} != LEGACY_BASELINE_FILES:
        raise MaintenanceError("Legacy baseline contains an unexpected file set")
    for name in LEGACY_BASELINE_FILES:
        require_regular(root / name)
    colour = read_text(root / "active-colour").strip()
    marker = read_text(root / "current").split()
    if (colour != "blue" or len(marker) != 4 or marker[:2] != [colour, LEGACY_BASELINE_SHA]
            or not all(DIGEST.fullmatch(value) for value in marker[2:])):
        raise MaintenanceError("Legacy baseline marker identity mismatch")
    manifest = _strict_json(root / "current-manifest.json")
    binding = _strict_json(root / "current-config.json")
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != "project-snow-release-1"
            or manifest.get("commit_sha") != LEGACY_BASELINE_SHA or manifest.get("app_version") != "0.9.6"):
        raise MaintenanceError("Legacy baseline manifest identity mismatch")
    for field, repository, expected in (
        ("application", "ghcr.io/x1aob/project_snow-public", marker[2]),
        ("embedding", "ghcr.io/x1aob/project_snow-embedding", marker[3]),
    ):
        coordinates = manifest.get(field)
        if (not isinstance(coordinates, dict) or set(coordinates) != {"image", "digest"}
                or coordinates.get("image") != repository
                or f"{coordinates.get('image')}@{coordinates.get('digest')}" != expected):
            raise MaintenanceError("Legacy baseline image coordinates disagree")
    configuration = paths.root / "releases" / "configurations" / LEGACY_BASELINE_SHA
    if (not isinstance(binding, dict)
            or set(binding) != {"schema_version", "colour", "commit_sha", "root", "configuration_sha256"}
            or binding.get("schema_version") != "project-snow-config-snapshot-1"
            or binding.get("colour") != colour or binding.get("commit_sha") != LEGACY_BASELINE_SHA
            or binding.get("root") != str(configuration)):
        raise MaintenanceError("Legacy baseline configuration identity mismatch")
    hashes = binding.get("configuration_sha256")
    if (not isinstance(hashes, dict) or set(hashes) != LEGACY_BASELINE_CONFIGURATION
            or hashes != manifest.get("configuration_sha256")
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in hashes.values())):
        raise MaintenanceError("Legacy baseline configuration hashes disagree")
    for name, expected in hashes.items():
        path = configuration / name
        for parent in (path.parent, *path.parent.parents):
            _controlled_directory(parent)
        require_regular(path)
        if path.stat().st_size > 1024 * 1024 or digest(path.read_bytes()) != expected:
            raise MaintenanceError("Legacy baseline configuration is missing or changed")
    containers = _strict_json(root / "containers.json")
    if not isinstance(containers, list) or len(containers) != 25:
        raise MaintenanceError("Legacy baseline container inventory is incomplete")
    references = set(marker[2:])
    identifiers, names = set(), set()
    for container in containers:
        if (not isinstance(container, dict) or set(container) != {"id", "image", "name", "status"}
                or not isinstance(container["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", container["id"])
                or not isinstance(container["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", container["image"])
                or not isinstance(container["name"], str)
                or not re.fullmatch(r"/?[A-Za-z0-9][A-Za-z0-9_.-]*", container["name"])
                or container["status"] not in ("created", "running", "paused", "restarting", "removing", "exited", "dead")):
            raise MaintenanceError("Legacy baseline container inventory is invalid")
        name = container["name"].removeprefix("/")
        if container["id"] in identifiers or name in names:
            raise MaintenanceError("Legacy baseline container inventory contains duplicates")
        identifiers.add(container["id"])
        names.add(name)
        references.add(container["image"])
    return references


def archive_colour(paths: Paths, colour: str) -> str:
    if colour not in {"blue", "green"}:
        raise MaintenanceError("Invalid recovery colour")
    releases = paths.root / "releases"
    colour_root = releases / "colours"
    sources = {"marker": colour_root / colour, "manifest.json": colour_root / f"{colour}-manifest.json",
               "config.json": colour_root / f"{colour}-config.json",
               "compose.env": paths.root / "runtime" / "colours" / f"{colour}.compose.env"}
    payloads = {name: read_text(source).encode("utf-8") for name, source in sources.items()}
    marker = payloads["marker"].decode().split()
    if len(marker) != 4 or marker[0] != colour or not SHA.fullmatch(marker[1]) or not all(DIGEST.fullmatch(x) for x in marker[2:]):
        raise MaintenanceError("Cannot anchor an inconsistent colour marker")
    manifest = json.loads(payloads["manifest.json"])
    binding = json.loads(payloads["config.json"])
    environment = read_environment(sources["compose.env"])
    if (manifest.get("commit_sha") != marker[1] or binding.get("commit_sha") != marker[1]
            or binding.get("colour") != colour or environment.get("PUBLIC_API_IMAGE") != marker[2]
            or environment.get("EMBEDDING_IMAGE") != marker[3]):
        raise MaintenanceError("Colour recovery metadata disagrees")
    expected_root = paths.root / "releases" / "configurations" / marker[1]
    if binding.get("root") != str(expected_root):
        raise MaintenanceError("Recovery configuration is outside the immutable namespace")
    for name, field in (("public.env", "PUBLIC_ENV_FILE"), ("mailer.env", "PUBLIC_MAILER_ENV_FILE")):
        if environment.get(field):
            source = Path(environment[field])
            allowed = (paths.root / "runtime", Path("/etc/project-snow"), paths.root / "releases" / "anchors")
            if not any(source.is_relative_to(parent) for parent in allowed):
                raise MaintenanceError("Recovery settings are outside trusted roots")
            payloads[name] = read_text(source).encode("utf-8")
    hashes = {name: digest(payload) for name, payload in payloads.items()}
    identifier = f"{marker[1]}-{digest(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode())}"
    anchors = releases / "anchors"
    anchors.mkdir(mode=0o700, exist_ok=True)
    _controlled_directory(anchors)
    target = anchors / identifier
    if target.exists() or target.is_symlink():
        verify_anchor(paths, identifier)
        return identifier
    temporary = Path(tempfile.mkdtemp(prefix=".anchor-", dir=anchors))
    try:
        metadata = {"schema_version": "project-snow-anchor-1", "release_id": identifier,
                    "commit_sha": marker[1], "colour": colour, "application_image": marker[2],
                    "embedding_image": marker[3], "files": hashes}
        payloads["anchor.json"] = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode()
        for name, payload in payloads.items():
            atomic_write(temporary / name, payload, 0o400)
        temporary.chmod(0o500)
        sync(temporary)
        os.rename(temporary, target)
        sync(anchors)
    finally:
        if temporary.exists():
            temporary.chmod(0o700)
            shutil.rmtree(temporary)
    verify_anchor(paths, identifier)
    return identifier


def archive_colours(paths: Paths) -> dict[str, Any]:
    active = active_release(paths)
    result = {}
    for colour in ("blue", "green"):
        marker = paths.root / "releases" / "colours" / colour
        if marker.exists() or marker.is_symlink():
            result[colour] = archive_colour(paths, colour)
        elif colour == active["colour"]:
            raise MaintenanceError("Active colour has no recovery metadata")
    # The index is convenient for humans; GC protects every immutable anchor,
    # including previous indices. Nothing here retires a stable release.
    index = {"schema_version": "project-snow-anchor-index-1", "active": result[active["colour"]], "colours": result}
    atomic_write(paths.root / "releases" / "anchors" / "latest.json", (json.dumps(index, sort_keys=True) + "\n").encode())
    return {"status": "ok", **index}


def restore_colour(paths: Paths, identifier: str, colour: str) -> dict[str, Any]:
    current = active_release(paths)
    if colour not in {"blue", "green"} or colour == current["colour"]:
        raise MaintenanceError("Recovery material may be restored only into the inactive colour")
    anchor, metadata = verify_anchor(paths, identifier)
    source_environment = read_environment(anchor / "compose.env")
    current_environment = read_environment(paths.root / "runtime" / "compose.env")
    # Every shared dependency except the embedding image must match the running
    # runtime. A difference in EMBEDDING_IMAGE alone is permitted: the anchor's
    # digest is the one recorded in this colour's signed manifest and was
    # promoted before, and the subsequent rollback recreates the embedding
    # container to exactly that digest so marker, manifest, current and
    # compose.env agree with what runs.
    differing_shared = [key for key in SHARED_IMAGE_KEYS
                        if source_environment.get(key) != current_environment.get(key)]
    if [key for key in differing_shared if key != "EMBEDDING_IMAGE"]:
        raise MaintenanceError("Restore shared dependencies separately before selecting this recovery anchor")
    binding = json.loads(read_text(anchor / "config.json"))
    configuration_root = Path(binding.get("root", ""))
    expected_root = paths.root / "releases" / "configurations" / metadata["commit_sha"]
    if configuration_root != expected_root:
        raise MaintenanceError("Recovery configuration path is invalid")
    _controlled_directory(configuration_root)
    for name, expected in binding.get("configuration_sha256", {}).items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise MaintenanceError("Unsafe recovery configuration path")
        if digest(read_text(configuration_root / relative).encode()) != expected:
            raise MaintenanceError("Retained configuration is missing or changed")
    # Preserve a previously staged candidate before replacing its colour files.
    if (paths.root / "releases" / "colours" / colour).exists():
        archive_colour(paths, colour)
    for name, field in (("public.env", "PUBLIC_ENV_FILE"), ("mailer.env", "PUBLIC_MAILER_ENV_FILE")):
        if name in metadata["files"]:
            source_environment[field] = str(anchor / name)
    source_environment["SNOW_UPSTREAM"] = f"public-api-{colour}:8000"
    binding["colour"] = colour
    marker = f"{colour} {metadata['commit_sha']} {metadata['application_image']} {metadata['embedding_image']}\n"
    destinations = [(paths.root / "runtime" / "colours" / f"{colour}.compose.env",
                     "".join(f"{key}={value}\n" for key, value in source_environment.items()).encode()),
                    (paths.root / "releases" / "colours" / f"{colour}-manifest.json", (anchor / "manifest.json").read_bytes()),
                    (paths.root / "releases" / "colours" / f"{colour}-config.json", (json.dumps(binding, sort_keys=True) + "\n").encode()),
                    (paths.root / "releases" / "colours" / colour, marker.encode())]
    # Publish the marker last. Interrupted preparations fail the existing
    # runner's exact marker/config checks and can be repeated from the anchor.
    for destination, payload in destinations:
        atomic_write(destination, payload)
    receipt = paths.root / "releases" / "colours" / f"{colour}-stage-receipt"
    if receipt.exists():
        require_regular(receipt)
        receipt.unlink()
        sync(receipt.parent)
    return {"status": "prepared", "colour": colour, "commit_sha": metadata["commit_sha"], "release_id": identifier,
            "next_command": f"project-snow-release rollback {colour} {metadata['commit_sha']}"}


# --- Stable rollback baselines and reversible retirement ---------------------
#
# Retention policy (owner decision): the host keeps only the active colour's
# release, the inactive colour's release (one-step rollback) and the two most
# recent stable baselines. Everything else is RETIRED -- moved out of the
# GC-protected namespace, never deleted here -- and recoverable from restic +
# GHCR digests. Retired items live under paths.root/"retired", a sibling of
# releases/ and runtime/, so protected_image_references()'s releases.rglob and
# runtime.rglob scans no longer see them, while anchor-unretire can move them
# back atomically on the same filesystem.


def _retired_root(paths: Paths, kind: str) -> Path:
    if kind not in {"anchors", "runtime-origin-edge"}:
        raise MaintenanceError("Unknown retired-area kind")
    return paths.root / "retired" / kind


def _anchor_index(paths: Paths) -> dict[str, Any]:
    """Read releases/anchors/latest.json: the active + inactive colour anchors.

    These two anchor ids are the one-step rollback material and are never
    retirable. A missing or malformed index is treated as protecting nothing
    extra beyond the live colours the callers independently verify.
    """
    path = paths.root / "releases" / "anchors" / "latest.json"
    if not path.exists() and not path.is_symlink():
        return {}
    index = json.loads(read_text(path))
    colours = index.get("colours")
    if index.get("schema_version") != "project-snow-anchor-index-1" or not isinstance(colours, dict):
        raise MaintenanceError("Recovery anchor index is malformed")
    return index


def _referenced_colour_anchors(paths: Paths) -> set[str]:
    index = _anchor_index(paths)
    referenced = set()
    for value in (index.get("colours") or {}).values():
        if ANCHOR_ID.fullmatch(str(value)):
            referenced.add(value)
    if ANCHOR_ID.fullmatch(str(index.get("active", ""))):
        referenced.add(index["active"])
    return referenced


def _anchor_index_is_fresh(paths: Paths) -> bool:
    """True when latest.json names an anchor of exactly each colour's current release.

    archive_colours() runs at stage, BEFORE a promotion, so after promoting the
    index still names the slot's previous occupant until the next `anchor`.
    Retirement trusts the index only when it matches the live colour markers.
    """
    index = _anchor_index(paths)
    colours = index.get("colours") or {}
    for colour in ("blue", "green"):
        marker = paths.root / "releases" / "colours" / colour
        if not marker.exists():
            return False
        commit = read_text(marker).split()[1]
        if not str(colours.get(colour, "")).startswith(commit + "-"):
            return False
    return True


def _require_fresh_anchor_index(paths: Paths) -> None:
    if not _anchor_index_is_fresh(paths):
        raise MaintenanceError("The anchor index does not match both colours; run `maintenance.py anchor` first")


def baseline_registry(paths: Paths) -> dict[str, Any]:
    """Return the root-owned baseline registry, or an empty default.

    The registry is a single root-only file. 'active' lists the current stable
    baselines newest-first (at most MAX_ACTIVE_BASELINES); 'retirable' lists
    baselines demoted out of the active window and now eligible for anchor
    retirement. Neither list ever deletes anything.
    """
    path = paths.root / "releases" / "baselines" / "registry.json"
    if not path.exists() and not path.is_symlink():
        return {"schema_version": BASELINE_REGISTRY_SCHEMA, "active": [], "retirable": []}
    require_regular(path, private=True)
    registry = json.loads(read_text(path))
    active = registry.get("active")
    retirable = registry.get("retirable")
    if (registry.get("schema_version") != BASELINE_REGISTRY_SCHEMA
            or not isinstance(active, list) or not isinstance(retirable, list)
            or any(not isinstance(item, dict) for item in active + retirable)):
        raise MaintenanceError("Baseline registry is malformed")
    for item in active + retirable:
        if (set(item) != {"release_id", "app_version", "commit_sha", "marked_at"}
                or not ANCHOR_ID.fullmatch(str(item.get("release_id")))):
            raise MaintenanceError("Baseline registry entry is malformed")
    return registry


def _write_baseline_registry(paths: Paths, registry: dict[str, Any]) -> None:
    directory = paths.root / "releases" / "baselines"
    directory.mkdir(mode=0o700, exist_ok=True)
    _controlled_directory(directory)
    atomic_write(directory / "registry.json", (json.dumps(registry, sort_keys=True, indent=2) + "\n").encode())


def baseline_mark(paths: Paths, identifier: str) -> dict[str, Any]:
    """Mark an anchor as a stable baseline. Idempotent; at most 2 active.

    Verifies the anchor, requires a feature-release (x.Y.0) app_version except
    for the grandfathered 1.0.0 baseline, and refuses symlinks/unsafe perms via
    verify_anchor's root-controlled checks. Marking a third baseline DEMOTES the
    oldest active one to 'retirable' (never deletes it).
    """
    _anchor, metadata = verify_anchor(paths, identifier)
    manifest = json.loads(read_text(paths.root / "releases" / "anchors" / identifier / "manifest.json"))
    app_version = str(manifest.get("app_version", ""))
    # 1.0.0 was reused across many builds, so it is NOT a trustworthy feature
    # marker: accept it ONLY for the one grandfathered baseline id. Every other
    # baseline must carry a genuine x.Y.0 feature version that is not 1.0.0.
    grandfathered = identifier == GRANDFATHERED_BASELINE_ID and app_version == "1.0.0"
    if not grandfathered and (app_version == "1.0.0" or not FEATURE_VERSION.fullmatch(app_version)):
        raise MaintenanceError("A baseline must be a feature release (x.Y.0) or the grandfathered 1.0.0 baseline")
    registry = baseline_registry(paths)
    active = [item for item in registry["active"] if item["release_id"] != identifier]
    retirable = [item for item in registry["retirable"] if item["release_id"] != identifier]
    entry = {"release_id": identifier, "app_version": app_version,
             "commit_sha": metadata["commit_sha"], "marked_at": datetime.now(timezone.utc).isoformat()}
    active.insert(0, entry)
    demoted = []
    while len(active) > MAX_ACTIVE_BASELINES:
        old = active.pop()
        demoted.append(old["release_id"])
        retirable.insert(0, old)
    registry = {"schema_version": BASELINE_REGISTRY_SCHEMA, "active": active, "retirable": retirable}
    _write_baseline_registry(paths, registry)
    return {"status": "ok", "release_id": identifier, "app_version": app_version,
            "active_baselines": [item["release_id"] for item in active], "demoted_to_retirable": demoted}


def _baseline_sets(paths: Paths) -> tuple[set[str], set[str]]:
    registry = baseline_registry(paths)
    active = {item["release_id"] for item in registry["active"]}
    retirable = {item["release_id"] for item in registry["retirable"]}
    return active, retirable


def _protected_anchor_ids(paths: Paths) -> set[str]:
    """Anchor ids that must never be retired: the live colours and current baselines."""
    active_baselines, _ = _baseline_sets(paths)
    return _referenced_colour_anchors(paths) | active_baselines


def retire_plan(paths: Paths) -> dict[str, Any]:
    """Dry-run: compute the anchor dirs eligible for reversible retirement.

    An ANCHOR_ID anchor is retirable when it is neither a live-colour anchor
    (latest.json), nor a current (active) baseline. Active baselines and the
    two colour anchors stay; registry 'retirable' baselines are listed as
    retirable like any other non-protected anchor. The legacy 0.9.6 baseline is
    reported separately (it has permanent restic snapshots and is retired only
    on explicit request).
    """
    anchors = paths.root / "releases" / "anchors"
    protected = _protected_anchor_ids(paths)
    retirable, present_legacy = [], False
    if anchors.exists():
        _controlled_directory(anchors)
        for child in sorted(anchors.iterdir()):
            if ANCHOR_ID.fullmatch(child.name):
                if child.name not in protected:
                    retirable.append(child.name)
            elif child.name == LEGACY_BASELINE_ID:
                present_legacy = True
    return {"schema_version": "project-snow-retire-plan-1", "protected": sorted(protected),
            "anchor_index_fresh": _anchor_index_is_fresh(paths),
            "retirable_anchors": retirable, "legacy_baseline_retirable": present_legacy,
            "origin_edge_snapshots_retirable": _retirable_origin_edge_snapshots(paths),
            "note": "Reversible: anchor-retire / origin-edge-retire move these under retired/; images only they pinned become GC candidates. anchor-unretire / origin-edge-unretire reverse it."}


def _backup_precondition(paths: Paths, release_id: str) -> None:
    """Refuse to retire an anchor without a concrete, completed backup.

    Reversible retirement already keeps the bytes on disk, but the owner policy
    is that an anchor leaves the protected set only once its contents are also
    captured off the protected namespace. The precondition is a successful
    backup (backups/last-success.json) completed after the anchor directory was
    created, OR the legacy baseline's permanent pinned restic snapshots.
    """
    if release_id == LEGACY_BASELINE_ID:
        return  # permanent pinned restic snapshots (see RECOVERY.md)
    anchor = paths.root / "releases" / "anchors" / release_id
    success = paths.root / "backups" / "last-success.json"
    if not success.exists():
        raise MaintenanceError("No successful backup on record; run backup before retiring an anchor")
    record = json.loads(read_text(success))
    completed = record.get("completed_at")
    if record.get("schema_version") != "project-snow-backup-success-1" or not isinstance(completed, str):
        raise MaintenanceError("Backup success record is malformed")
    anchor_created = datetime.fromtimestamp(anchor.stat().st_ctime, timezone.utc)
    try:
        completed_at = datetime.fromisoformat(completed)
    except ValueError as exc:
        raise MaintenanceError("Backup success timestamp is invalid") from exc
    if completed_at <= anchor_created:
        raise MaintenanceError("The latest successful backup predates this anchor; back up before retiring it")


def _atomic_rename_dir(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise MaintenanceError("Retirement destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _controlled_directory(destination.parent)
    os.rename(source, destination)  # same filesystem; atomic
    sync(destination.parent)
    sync(source.parent)


def anchor_retire(paths: Paths, release_id: str) -> dict[str, Any]:
    """Reversibly retire one anchor dir out of releases/anchors.

    Refuses the active colour's anchor, the inactive colour's anchor, any anchor
    referenced by releases/anchors/latest.json, and any CURRENT baseline. The
    legacy 0.9.6 baseline uses the same mechanism (kind 'runtime-origin-edge' is
    for compose snapshots; anchors use kind 'anchors'). A concrete backup
    precondition must hold. restore-anchor and gc-plan no longer see it.
    """
    is_legacy = release_id == LEGACY_BASELINE_ID
    if not is_legacy and not ANCHOR_ID.fullmatch(release_id):
        raise MaintenanceError("Invalid anchor release id")
    anchors = paths.root / "releases" / "anchors"
    _controlled_directory(anchors)
    source = anchors / release_id
    if not source.is_dir() or source.is_symlink():
        raise MaintenanceError("Anchor directory is missing or linked")
    _require_fresh_anchor_index(paths)
    if not is_legacy:
        verify_anchor(paths, release_id)
        protected = _protected_anchor_ids(paths)
        if release_id in protected:
            raise MaintenanceError("Refusing to retire a live-colour anchor or a current baseline")
    if release_id in _referenced_colour_anchors(paths):
        raise MaintenanceError("Refusing to retire an anchor referenced by latest.json")
    _backup_precondition(paths, release_id)
    destination = _retired_root(paths, "anchors") / release_id
    _atomic_rename_dir(source, destination)
    return {"status": "retired", "release_id": release_id, "retired_path": str(destination),
            "reversible": True, "unretire_command": f"anchor-unretire --release-id {release_id}"}


def anchor_unretire(paths: Paths, release_id: str) -> dict[str, Any]:
    """Move a retired anchor dir back into releases/anchors (reverse of retire)."""
    is_legacy = release_id == LEGACY_BASELINE_ID
    if not is_legacy and not ANCHOR_ID.fullmatch(release_id):
        raise MaintenanceError("Invalid anchor release id")
    source = _retired_root(paths, "anchors") / release_id
    if not source.is_dir() or source.is_symlink():
        raise MaintenanceError("Retired anchor is missing or linked")
    anchors = paths.root / "releases" / "anchors"
    anchors.mkdir(mode=0o700, exist_ok=True)
    _controlled_directory(anchors)
    _atomic_rename_dir(source, anchors / release_id)
    if not is_legacy:
        verify_anchor(paths, release_id)
    else:
        verify_legacy_baseline(paths)
    return {"status": "unretired", "release_id": release_id}


def _live_origin_edge_snapshot(paths: Paths) -> Path:
    """The one runtime/origin-edge snapshot the running listener is bound to.

    Mirrors promote.sh's validation of releases/live-origin-edge-config.json:
    schema, a <sha256>.compose.env path directly under runtime/origin-edge, and
    file bytes hashing to that name. Anything else refuses (never guesses).
    """
    binding_path = paths.root / "releases" / "live-origin-edge-config.json"
    if not binding_path.exists() or binding_path.is_symlink():
        raise MaintenanceError("No live origin-edge binding; refusing to retire origin-edge snapshots")
    require_regular(binding_path, private=True)
    binding = json.loads(read_text(binding_path))
    origin = paths.root / "runtime" / "origin-edge"
    env_path = Path(str(binding.get("origin_edge_env_path", "")))
    env_sha = str(binding.get("origin_edge_env_sha256", ""))
    if (binding.get("schema_version") != "project-snow-config-snapshot-1"
            or binding.get("live_origin_edge_schema") != LIVE_ORIGIN_EDGE_SCHEMA
            or env_path.parent != origin or env_path.name != f"{env_sha}.compose.env"
            or not ORIGIN_EDGE_SNAPSHOT.fullmatch(env_path.name)):
        raise MaintenanceError("Live origin-edge binding is malformed")
    if not env_path.is_file() or env_path.is_symlink():
        raise MaintenanceError("Live origin-edge snapshot is missing or linked")
    require_regular(env_path, private=True)
    if digest(env_path.read_bytes()) != env_sha:
        raise MaintenanceError("Live origin-edge snapshot bytes do not match the binding")
    return env_path


def _retirable_origin_edge_snapshots(paths: Paths) -> list[str]:
    origin = paths.root / "runtime" / "origin-edge"
    if not origin.exists() and not origin.is_symlink():
        return []
    _controlled_directory(origin)
    live = _live_origin_edge_snapshot(paths)
    names = []
    for child in sorted(origin.iterdir()):
        if child.is_symlink() or not child.is_file() or not ORIGIN_EDGE_SNAPSHOT.fullmatch(child.name):
            raise MaintenanceError("Unexpected entry in runtime/origin-edge")
        if child != live:
            names.append(child.name)
    return names


def origin_edge_snapshot_retire(paths: Paths) -> dict[str, Any]:
    """Reversibly retire every runtime/origin-edge snapshot except the live binding.

    Only releases/live-origin-edge-config.json's snapshot is read by promote.sh
    (retain / overlay / restore of the running listener); older snapshots are
    history whose only effect is to pin old images in protected_image_references.
    """
    origin = paths.root / "runtime" / "origin-edge"
    names = _retirable_origin_edge_snapshots(paths)
    destination_root = _retired_root(paths, "runtime-origin-edge")
    retired = []
    for name in names:
        destination = destination_root / name
        if destination.exists() or destination.is_symlink():
            raise MaintenanceError("Origin-edge retirement destination already exists")
        destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _controlled_directory(destination_root)
        os.rename(origin / name, destination)
        retired.append(name)
    if retired:
        sync(destination_root)
        sync(origin)
    return {"status": "ok", "retired": retired, "kept_live": _live_origin_edge_snapshot(paths).name,
            "retired_root": str(destination_root),
            "unretire_command": "origin-edge-unretire --name <sha256>.compose.env"}


def origin_edge_snapshot_unretire(paths: Paths, name: str) -> dict[str, Any]:
    """Move one retired origin-edge snapshot back under runtime/origin-edge."""
    if not ORIGIN_EDGE_SNAPSHOT.fullmatch(name):
        raise MaintenanceError("Invalid origin-edge snapshot name")
    source = _retired_root(paths, "runtime-origin-edge") / name
    if not source.is_file() or source.is_symlink():
        raise MaintenanceError("Retired origin-edge snapshot is missing or linked")
    if digest(source.read_bytes()) != name.split(".", 1)[0]:
        raise MaintenanceError("Retired origin-edge snapshot bytes do not match its name")
    origin = paths.root / "runtime" / "origin-edge"
    _controlled_directory(origin)
    destination = origin / name
    if destination.exists() or destination.is_symlink():
        raise MaintenanceError("Origin-edge snapshot already present")
    os.rename(source, destination)
    sync(origin)
    sync(source.parent)
    return {"status": "unretired", "name": name}


def protected_image_references(paths: Paths, *, strict_unknown: bool = True) -> set[str]:
    references: set[str] = set()
    releases = paths.root / "releases"
    anchors = releases / "anchors"
    if anchors.exists():
        _controlled_directory(anchors)
        for child in anchors.iterdir():
            if ANCHOR_ID.fullmatch(child.name):
                root, _ = verify_anchor(paths, child.name)
                references.update(value for value in read_environment(root / "compose.env").values() if DIGEST.fullmatch(value))
            elif child.name == LEGACY_BASELINE_ID:
                references.update(verify_legacy_baseline(paths))
            elif child.name != "latest.json" and strict_unknown:
                # A separately captured emergency/baseline anchor may use an
                # older schema. Do not silently ignore its protected resources.
                raise MaintenanceError("Unknown recovery anchor format; image collection is disabled until its references are reconciled")
    # Protect every retained environment/configuration, not just the current
    # colour. Other applications' files and containers are never GC targets.
    runtime = paths.root / "runtime"
    for environment in runtime.rglob("*.env"):
        if environment.is_symlink():
            raise MaintenanceError("Unexpected symlink in retained runtime metadata")
        if environment.name == "compose.env" or environment.name.endswith(".compose.env"):
            values = read_environment(environment)
            if environment.parent == runtime / "origin-edge" and ORIGIN_EDGE_SNAPSHOT.fullmatch(environment.name):
                # A listener snapshot is only ever used to (re)create origin-edge,
                # which runs CADDY_IMAGE alone; its other keys are incidental
                # copies of the slot it was taken from and pin nothing.
                values = {key: values.get(key, "") for key in ORIGIN_EDGE_PROTECTED_KEYS}
            references.update(value for value in values.values() if DIGEST.fullmatch(value))
    for manifest in releases.rglob("*-manifest.json"):
        document = json.loads(read_text(manifest))
        for field in ("application", "embedding"):
            coordinates = document.get(field) or {}
            reference = f"{coordinates.get('image', '')}@{coordinates.get('digest', '')}"
            if DIGEST.fullmatch(reference):
                references.add(reference)
    return references


def pending_candidate(paths: Paths) -> dict[str, Any] | None:
    path = paths.root / "releases" / "pending-candidate.json"
    if not path.exists() and not path.is_symlink():
        return None
    pending = json.loads(read_text(path))
    if (pending.get("schema_version") != "project-snow-pending-candidate-1" or pending.get("colour") not in {"blue", "green"}
            or not SHA.fullmatch(str(pending.get("commit_sha", "")))
            or not SHA.fullmatch(str(pending.get("base_commit_sha", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(pending.get("manifest_sha256", "")))):
        raise MaintenanceError("Pending candidate metadata is invalid")
    return pending


def record_candidate(paths: Paths, colour: str, manifest_path: Path) -> dict[str, Any]:
    active = active_release(paths)
    manifest_bytes = read_text(manifest_path).encode()
    manifest = json.loads(manifest_bytes)
    commit = str(manifest.get("commit_sha", ""))
    if colour not in {"blue", "green"} or colour == active["colour"] or not SHA.fullmatch(commit):
        raise MaintenanceError("Candidate must use a valid inactive colour and SHA")
    previous = pending_candidate(paths)
    if previous and previous["commit_sha"] not in {commit, active["commit_sha"]}:
        raise MaintenanceError("A different candidate is awaiting review; explicitly discard it before staging another release")
    if previous and previous["commit_sha"] == commit and previous["manifest_sha256"] != digest(manifest_bytes):
        raise MaintenanceError("This candidate SHA has different manifest bytes; explicitly discard the previous candidate before replacing it")
    candidate = {"schema_version": "project-snow-pending-candidate-1", "colour": colour, "commit_sha": commit,
                 "base_commit_sha": active["commit_sha"], "manifest_sha256": digest(manifest_bytes),
                 "recorded_at": datetime.now(timezone.utc).isoformat()}
    atomic_write(paths.root / "releases" / "pending-candidate.json", (json.dumps(candidate, sort_keys=True) + "\n").encode())
    return {"status": "candidate-reserved", **candidate}


def discard_candidate(paths: Paths, commit: str, *, promoted: bool = False) -> dict[str, Any]:
    if not SHA.fullmatch(commit):
        raise MaintenanceError("A complete candidate SHA is required")
    pending = pending_candidate(paths)
    if pending is None:
        return {"status": "absent"}
    if pending["commit_sha"] != commit:
        raise MaintenanceError("The requested SHA is not the pending candidate")
    if promoted and active_release(paths)["commit_sha"] != commit:
        raise MaintenanceError("Only an exactly promoted candidate can be completed")
    path = paths.root / "releases" / "pending-candidate.json"
    path.unlink()
    sync(path.parent)
    return {"status": "completed" if promoted else "discarded", "commit_sha": commit}


def image_gc_plan(paths: Paths, execute: Callable[[list[str]], str] = run) -> dict[str, Any]:
    references = protected_image_references(paths)
    protected_ids: set[str] = set()
    container_ids = execute(["docker", "ps", "--all", "--quiet", "--no-trunc"]).split()
    if any(not OBJECT_ID.fullmatch(value) for value in container_ids):
        raise MaintenanceError("Unexpected container identity in global inventory")
    if container_ids:
        containers = docker_json(["inspect", *container_ids], execute)
        protected_ids.update(str(container["Image"]) for container in containers)
    # With containerd, distinct OCI index IDs can share the runnable manifest.
    # Container inspect.Image alone does not account for that relationship.
    for row in execute(["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{.ID}} {{.Containers}}"]).splitlines():
        columns = row.split()
        if len(columns) != 2 or not OBJECT_ID.fullmatch(columns[0]) or not columns[1].isdigit():
            raise MaintenanceError("Cannot establish Docker image container-reference counts")
        if int(columns[1]) > 0:
            protected_ids.add(columns[0])
    for reference in sorted(references):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
            # A baseline may record the runnable image identity while Docker's
            # inspect resolves it to an OCI index identity. Retain both.
            protected_ids.add(reference)
        images = docker_json(["image", "inspect", reference], execute)
        if not isinstance(images, list) or len(images) != 1:
            raise MaintenanceError("A retained release image is missing; restore it before garbage collection")
        protected_ids.add(images[0]["Id"])
    identifiers = sorted(set(execute(["docker", "image", "ls", "--all", "--quiet", "--no-trunc"]).split()))
    if any(not OBJECT_ID.fullmatch(value) for value in identifiers):
        raise MaintenanceError("Unexpected image identity in inventory")
    candidates = []
    images = docker_json(["image", "inspect", *identifiers], execute) if identifiers else []
    for item in images:
        image_id = item["Id"]
        coordinates = list(item.get("RepoTags") or []) + list(item.get("RepoDigests") or [])
        repositories = {ref.split("@")[0] if "@" in ref else ref.rsplit(":", 1)[0] for ref in coordinates}
        # Untagged/unknown/foreign/shared images are deliberately excluded.
        if image_id in protected_ids or not repositories or not repositories <= SNOW_REPOSITORIES:
            continue
        candidates.append({"image_id": image_id, "references": sorted(coordinates), "size_bytes": int(item.get("Size", 0))})
    return {"schema_version": "project-snow-image-gc-plan-1", "protected_image_ids": sorted(protected_ids),
            "protected_references": sorted(references), "candidates": candidates,
            "note": "Reported sizes share layers; do not sum them as guaranteed reclaimed space. No automatic prune is performed."}


def delete_planned_images(paths: Paths, requested: list[str], execute: Callable[[list[str]], str] = run) -> dict[str, Any]:
    if not requested or len(set(requested)) != len(requested) or any(not OBJECT_ID.fullmatch(value) for value in requested):
        raise MaintenanceError("Explicit unique full image IDs are required")
    removed = []
    for image_id in requested:
        # Rebuild the global protection inventory before EACH deletion. Docker's
        # non-forced removal adds a final guard for newly created containers.
        eligible = {item["image_id"] for item in image_gc_plan(paths, execute)["candidates"]}
        if image_id not in eligible:
            raise MaintenanceError("Requested image is no longer an unreferenced Snow-only candidate")
        execute(["docker", "image", "rm", image_id])
        removed.append(image_id)
    return {"status": "ok", "removed_image_ids": removed}
