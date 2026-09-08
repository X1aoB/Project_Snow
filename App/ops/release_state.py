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
    if any(source_environment.get(key) != current_environment.get(key) for key in SHARED_IMAGE_KEYS):
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
            references.update(value for value in read_environment(environment).values() if DIGEST.fullmatch(value))
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
