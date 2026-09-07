"""Scan only the actual, manifest-bound running Project Snow images.

Linux release-host tool. Requires an already installed, operator-approved Trivy
image by full digest; never pulls tags, mounts Docker's socket into a scanner,
or changes another container. Output deliberately excludes Docker/Trivy Env.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

try:
    from scripts.report_trivy_findings import collect_findings
except ModuleNotFoundError:
    from report_trivy_findings import collect_findings

PROJECT = "project-snow-public"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SCANNER = re.compile(r"ghcr\.io/aquasecurity/trivy@sha256:[0-9a-f]{64}")
SERVICE_KEYS = {
    "public-api-blue": "PUBLIC_API_IMAGE",
    "public-api-green": "PUBLIC_API_IMAGE",
    "admin": "PUBLIC_API_IMAGE",
    "feedback-mailer": "PUBLIC_API_IMAGE",
    "embedding": "EMBEDDING_IMAGE",
    "caddy": "CADDY_IMAGE",
    "origin-edge": "CADDY_IMAGE",
    "cloudflared": "CLOUDFLARED_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "qdrant": "QDRANT_IMAGE",
    "neo4j": "NEO4J_IMAGE",
    "egress-proxy": "EGRESS_PROXY_IMAGE",
}
GIB = 1024**3
HOST_DISK_RESERVE = 10 * GIB
DATABASE_SCRATCH_ESTIMATE = 4 * GIB
MAX_REPORT = 32 * 1024**2
CONTAINER_FORMAT = (
    '{"id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)
IMAGE_FORMAT = (
    '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}},'
    '"size":{{json .Size}},"os":{{json .Os}},"architecture":{{json .Architecture}}}'
)


class ScanError(RuntimeError):
    pass


def canonical_reference(value: str) -> str:
    repository, separator, digest = str(value).partition("@")
    if (
        not separator
        or not IMAGE_ID.fullmatch(digest)
        or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*", repository)
    ):
        raise ScanError("An image binding is not an immutable digest")
    # RepoDigests omit a tag that may be present in the deployment pin.
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    first = repository.split("/", 1)[0]
    if "/" not in repository:
        repository = "docker.io/library/" + repository
    elif "." not in first and ":" not in first and first != "localhost":
        repository = "docker.io/" + repository
    if repository.startswith("index.docker.io/"):
        repository = "docker.io/" + repository.removeprefix("index.docker.io/")
    return repository + "@" + digest


def controlled_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ScanError("Controlled directories must use absolute paths")
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
            raise ScanError("A metadata, work or output directory is not root-controlled")


def controlled_bytes(path: Path) -> bytes:
    """Read only root-controlled release metadata; never follow a symlink."""
    controlled_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid
            or info.st_gid
            or info.st_mode & 0o022
            or info.st_size > 1024**2
        ):
            raise ScanError("Release metadata is not a bounded root-controlled regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(1024**2 + 1)
    finally:
        os.close(descriptor)


def image_environment(payload: bytes) -> dict[str, str]:
    result = {}
    for line in payload.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in set(SERVICE_KEYS.values()):
            if key in result:
                raise ScanError("Duplicate image binding in deployment metadata")
            result[key] = canonical_reference(value.strip())
    return result


def load_bindings(
    root: Path, services: set[str], read: Callable = controlled_bytes, retained_admin_image: str | None = None
) -> tuple[dict, dict]:
    """Current/colour metadata, never a manifest from a source checkout or main."""
    colour = read(root / "releases" / "active-colour").decode().strip()
    if colour not in {"blue", "green"} or f"public-api-{colour}" not in services:
        raise ScanError("The active public API is not running")

    def release(marker_path, manifest_path, environment_path, expected_colour):
        marker = read(marker_path).decode().split()
        manifest_bytes = read(manifest_path)
        manifest = json.loads(manifest_bytes)
        environment_bytes = read(environment_path)
        environment = image_environment(environment_bytes)
        if (
            len(marker) != 4
            or marker[0] != expected_colour
            or not COMMIT.fullmatch(marker[1])
            or manifest.get("schema_version") != "project-snow-release-1"
            or manifest.get("commit_sha") != marker[1]
        ):
            raise ScanError("Running release marker and manifest identity disagree")
        for index, field, key in (
            (2, "application", "PUBLIC_API_IMAGE"),
            (3, "embedding", "EMBEDDING_IMAGE"),
        ):
            entry = manifest.get(field) or {}
            reference = canonical_reference(f"{entry.get('image', '')}@{entry.get('digest', '')}")
            if reference != canonical_reference(marker[index]) or reference != environment.get(key):
                raise ScanError("Running manifest and deployment image pins disagree")
        return environment, {
            "commit_sha": marker[1],
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "image_pins_sha256": hashlib.sha256(environment_bytes).hexdigest(),
        }

    current, identity = release(
        root / "releases" / "current",
        root / "releases" / "current-manifest.json",
        root / "runtime" / "compose.env",
        colour,
    )
    if retained_admin_image:
        if "admin" not in services:
            raise ScanError("The explicitly acknowledged retained admin is not running")
        retained_admin_image = canonical_reference(retained_admin_image)
        if not retained_admin_image.startswith("ghcr.io/x1aob/project_snow-public@sha256:"):
            raise ScanError("A retained admin scan target must use the Project Snow public repository")
    bindings = {}
    for service in services:
        if service not in SERVICE_KEYS:
            raise ScanError("An unknown Project Snow service is running; update the reviewed allowlist")
        environment, source = current, identity
        if service in {"public-api-blue", "public-api-green"} and service != f"public-api-{colour}":
            other = service.removeprefix("public-api-")
            environment, source = release(
                root / "releases" / "colours" / other,
                root / "releases" / "colours" / f"{other}-manifest.json",
                root / "runtime" / "colours" / f"{other}.compose.env",
                other,
            )
        if service == "origin-edge" and (root / "releases" / "live-origin-edge-config.json").exists():
            binding = json.loads(read(root / "releases" / "live-origin-edge-config.json"))
            path = Path(binding.get("origin_edge_env_path", ""))
            if path.parent != root / "runtime" / "origin-edge":
                raise ScanError("Retained origin edge image binding is outside its release namespace")
            payload = read(path)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != binding.get("origin_edge_env_sha256"):
                raise ScanError("Retained origin edge image pin checksum differs")
            environment = image_environment(payload)
            source = {"commit_sha": binding.get("commit_sha"), "image_pins_sha256": digest}
        key = SERVICE_KEYS[service]
        if key not in environment:
            raise ScanError(f"Missing deployment image pin for {service}")
        bindings[service] = {"reference": environment[key], **source}
        if service == "admin" and retained_admin_image and retained_admin_image != environment[key]:
            bindings[service] = {
                "reference": retained_admin_image,
                "expected_reference": environment[key],
                "identity_status": "retained_unbound",
                "operator_acknowledged_scan_only": True,
            }
    return bindings, {"active_colour": colour, **identity}


class Commands:
    def __init__(
        self, deadline_seconds: int, execute: Callable = subprocess.run, clock: Callable = time.monotonic
    ):
        self.execute, self.clock = execute, clock
        self.deadline = clock() + deadline_seconds

    def remaining(self) -> float:
        value = self.deadline - self.clock()
        if value <= 0:
            raise ScanError("The total discovery/download/scan deadline expired")
        return value

    def run(self, command: list[str], *, cleanup: bool = False, allow_missing: bool = False) -> str:
        timeout = 10 if cleanup else min(self.remaining(), 1800)
        if command[0] == "docker":
            # A saved remote Docker context must not redirect a host scan.
            command = ["docker", "--host", "unix:///var/run/docker.sock", *command[1:]]
        try:
            result = self.execute(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScanError("A scan command exceeded its bounded deadline") from exc
        except OSError as exc:
            raise ScanError("A required local executable is unavailable") from exc
        if result.returncode and not (allow_missing and result.returncode == 1):
            raise ScanError(
                f"A scan command failed (exit {result.returncode}); no successful scan is recorded"
            )
        if len(result.stdout or "") > 1024**2:
            raise ScanError("A scan command returned oversized metadata")
        return result.stdout if not result.returncode else ""


def running_containers(commands: Commands) -> list[dict]:
    ids = commands.run(
        ["docker", "ps", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}"]
    ).split()
    if not ids or len(ids) > 32 or any(not CONTAINER_ID.fullmatch(item) for item in ids):
        raise ScanError("Expected a bounded set of running Project Snow containers")
    lines = commands.run(["docker", "container", "inspect", "--format", CONTAINER_FORMAT, *ids]).splitlines()
    rows, seen = [], set()
    for line in lines:
        row = json.loads(line)
        if row.get("project") != PROJECT:
            continue
        service = row.get("service")
        if (
            service not in SERVICE_KEYS
            or service in seen
            or row.get("running") is not True
            or row.get("id") not in ids
            or not IMAGE_ID.fullmatch(str(row.get("image_id", "")))
        ):
            raise ScanError("Running container identity, service allowlist or uniqueness check failed")
        seen.add(service)
        rows.append({key: row[key] for key in ("id", "image_id", "running", "project", "service")})
    return sorted(rows, key=lambda row: row["service"])


def inspect_image(commands: Commands, image_id: str) -> dict:
    rows = commands.run(["docker", "image", "inspect", "--format", IMAGE_FORMAT, image_id]).splitlines()
    if len(rows) != 1:
        raise ScanError("Expected exactly one installed image")
    row = json.loads(rows[0])
    if not IMAGE_ID.fullmatch(str(row.get("id", ""))):
        raise ScanError("Docker returned an invalid installed image identity")
    return row


def select_images(containers: list[dict], bindings: dict, commands: Commands) -> list[dict]:
    selected = {}
    for container in containers:
        service, image_id = container["service"], container["image_id"]
        image = selected.get(image_id)
        if image is None:
            metadata = inspect_image(commands, image_id)
            if metadata["id"] != image_id:
                raise ScanError("Running image changed during discovery")
            image = selected[image_id] = {
                "image_id": image_id,
                "services": [],
                "bindings": [],
                "repo_digests": sorted(
                    canonical_reference(value) for value in metadata.get("repo_digests") or []
                ),
                "size_bytes": metadata.get("size"),
                "os": metadata.get("os"),
                "architecture": metadata.get("architecture"),
            }
        binding = bindings[service]
        if binding["reference"] not in image["repo_digests"]:
            raise ScanError(f"Actual running digest does not match deployment metadata for {service}")
        image["services"].append(service)
        image["bindings"].append(binding)
    return sorted(selected.values(), key=lambda image: image["image_id"])


def archive_config_ids(path: Path, image: dict) -> set[str]:
    """Docker's containerd Image ID may be an OCI index, not a config digest."""
    with tarfile.open(path, "r:") as archive:

        def member_bytes(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 2 * 1024**2:
                raise ScanError("Image archive contains invalid manifest metadata")
            with archive.extractfile(member) as handle:
                return handle.read()

        def config(name, digest=None):
            payload = member_bytes(name)
            actual = "sha256:" + hashlib.sha256(payload).hexdigest()
            if digest is not None and actual != digest:
                raise ScanError("Image archive config checksum differs")
            document = json.loads(payload)
            if document.get("os") == image["os"] and document.get("architecture") == image["architecture"]:
                return {actual}
            return set()

        try:
            manifest = json.loads(member_bytes("manifest.json"))
        except KeyError:
            manifest = None
        if manifest is not None:
            if not isinstance(manifest, list) or len(manifest) > 16:
                raise ScanError("Image archive has an unexpected Docker manifest")
            result = set()
            for item in manifest:
                result.update(config(item["Config"]))
        else:
            visited = set()

            def walk(document):
                if "config" in document:
                    digest = document["config"]["digest"]
                    if not IMAGE_ID.fullmatch(digest):
                        raise ScanError("Image archive config identity is invalid")
                    return config("blobs/sha256/" + digest.split(":")[1], digest)
                result = set()
                for descriptor in document.get("manifests", []):
                    digest = descriptor.get("digest", "")
                    if not IMAGE_ID.fullmatch(digest) or len(visited) >= 32:
                        raise ScanError("Image archive index exceeds its metadata bounds")
                    if digest in visited:
                        continue
                    visited.add(digest)
                    platform = descriptor.get("platform") or {}
                    if platform and (platform.get("os"), platform.get("architecture")) != (
                        image["os"],
                        image["architecture"],
                    ):
                        continue
                    payload = member_bytes("blobs/sha256/" + digest.split(":")[1])
                    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
                        raise ScanError("Image archive manifest checksum differs")
                    result.update(walk(json.loads(payload)))
                return result

            result = walk(json.loads(member_bytes("index.json")))
    if len(result) != 1:
        raise ScanError("Image archive does not identify exactly one running platform config")
    return result


def findings_from_report(path: Path, image: dict, configs: set[str]) -> list[dict]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_REPORT:
        raise ScanError("Trivy did not produce a bounded regular report")
    report = json.loads(path.read_bytes())
    if (
        report.get("SchemaVersion") != 2
        or report.get("ArtifactType") != "container_image"
        or not isinstance(report.get("Results"), list)
        or (report.get("Metadata") or {}).get("ImageID") not in {image["image_id"], *configs}
    ):
        raise ScanError("Trivy report is not bound to the exported running image")
    for result in report["Results"]:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("Vulnerabilities") or [], list)
            or any(not isinstance(item, dict) for item in result.get("Vulnerabilities") or [])
        ):
            raise ScanError("Trivy returned malformed vulnerability results")
    # Exactly the existing CI policy. Never persist raw Metadata/ImageConfig,
    # package inventories, secret findings, environment, or diagnostic output.
    return [
        finding
        for finding in collect_findings(report)
        if finding["severity"] in {"HIGH", "CRITICAL"} and finding["fixed"] != "not-published"
    ]


def run_scanner(
    commands: Commands, scanner: str, work: Path, arguments: list[str], image_path: Path | None = None
) -> None:
    token = uuid4().hex
    cid = work / f"{token}.cid"
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        f"snow-running-scan-{token}",
        "--cidfile",
        str(cid),
        "--label",
        f"io.project-snow.running-scan={token}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--memory-swap",
        "2g",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,noexec,size=512m",
        "--env",
        "TMPDIR=/cache/tmp",
        "--workdir",
        "/",
        "--mount",
        f"type=bind,source={work / 'cache'},target=/cache",
        "--mount",
        f"type=bind,source={work / 'policy'},target=/policy,readonly",
        "--mount",
        f"type=bind,source={work / 'reports'},target=/reports",
        "--network",
        "none" if image_path else "bridge",
    ]
    if image_path:
        command += ["--mount", f"type=bind,source={image_path},target=/input/image.tar,readonly"]
    seconds = max(1, math.floor(commands.remaining()))
    command += [
        "--entrypoint",
        "trivy",
        scanner,
        "--cache-dir",
        "/cache",
        "--config",
        "/policy/trivy.yaml",
        "--quiet",
        "--timeout",
        f"{seconds}s",
        "image",
        "--no-progress",
        "--skip-version-check",
        "--disable-telemetry",
        *arguments,
    ]
    try:
        commands.run(command)
    finally:
        # Only our exact ID plus random ownership label is ever eligible for
        # removal. Docker may keep a run alive after its CLI times out.
        identifier = cid.read_text().strip() if cid.exists() else None
        if identifier and not CONTAINER_ID.fullmatch(identifier):
            raise ScanError("Scanner cleanup has an invalid owned container identity")
        # Timeout may precede cidfile publication. Only our unpredictable name
        # plus the same ownership label can resolve a fallback cleanup ID.
        cleanup_target = identifier or f"snow-running-scan-{token}"
        payload = commands.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"owner":{{json (index .Config.Labels "io.project-snow.running-scan")}}}',
                cleanup_target,
            ],
            cleanup=True,
            allow_missing=True,
        ).strip()
        if payload:
            owned = json.loads(payload)
            if not CONTAINER_ID.fullmatch(str(owned.get("id", ""))):
                raise ScanError("Scanner cleanup has an invalid owned container identity")
            if owned.get("owner") != token or (identifier and owned["id"] != identifier):
                raise ScanError("Scanner cleanup ownership changed; no container was removed")
            commands.run(["docker", "container", "rm", "--force", owned["id"]], cleanup=True)


def require_scratch_space(root: Path, work: Path, extra_bytes: int = 0) -> None:
    """Keep the host reserve in addition to the next operation's scratch estimate."""
    if shutil.disk_usage(root).free < HOST_DISK_RESERVE:
        raise ScanError("At least 10 GiB free host disk reserve is required for scanning")
    if shutil.disk_usage(work).free < HOST_DISK_RESERVE + extra_bytes:
        raise ScanError("Insufficient scratch space beyond the required 10 GiB disk reserve")


def scan(
    root: Path,
    scanner: str,
    commands: Commands,
    *,
    work_root: Path | None = None,
    read: Callable = controlled_bytes,
    max_image_bytes: int = 4 * GIB,
    retained_admin_image: str | None = None,
) -> dict:
    if not SCANNER.fullmatch(scanner):
        raise ScanError(
            "Trivy must be an approved ghcr.io/aquasecurity/trivy image with a full sha256 digest"
        )
    scanner_metadata = inspect_image(commands, scanner)
    if canonical_reference(scanner) not in {
        canonical_reference(value) for value in scanner_metadata.get("repo_digests") or []
    }:
        raise ScanError("The approved Trivy digest is not installed locally; no image was pulled")
    containers = running_containers(commands)
    bindings, release = load_bindings(
        root, {row["service"] for row in containers}, read, retained_admin_image
    )
    images = select_images(containers, bindings, commands)
    result = {
        "schema_version": "project-snow-running-image-scan-1",
        "status": "scanning",
        "project": PROJECT,
        "started_at": datetime.now(UTC).isoformat(),
        "release": release,
        "scanner": scanner,
        "scanner_image_id": scanner_metadata["id"],
        "containers": containers,
        "images": [],
        "identity_status": "retained_unbound"
        if any(row.get("identity_status") == "retained_unbound" for row in bindings.values())
        else "consistent",
        "policy": (
            "HIGH/CRITICAL with a published fix; actual exported running images; offline package analysis"
        ),
    }
    with tempfile.TemporaryDirectory(prefix="snow-running-scan-", dir=work_root) as directory:
        work = Path(directory)
        for name in ("cache", "policy", "reports"):
            (work / name).mkdir(mode=0o700)
        # Java DB downloads/decompression can exceed /tmp's bounded tmpfs.
        # Use private disk scratch without increasing the scanner's RAM cap.
        (work / "cache" / "tmp").mkdir(mode=0o700)
        (work / "policy" / "trivy.yaml").write_text("{}\n")
        (work / "policy" / "ignore").write_text("")
        require_scratch_space(root, work, DATABASE_SCRATCH_ESTIMATE)
        run_scanner(commands, scanner, work, ["--download-db-only"])
        require_scratch_space(root, work, DATABASE_SCRATCH_ESTIMATE)
        run_scanner(commands, scanner, work, ["--download-java-db-only"])
        require_scratch_space(root, work)
        metadata_path = work / "cache" / "db" / "metadata.json"
        if not metadata_path.is_file() or metadata_path.stat().st_size > 16384:
            raise ScanError("Trivy database download did not produce bounded metadata")
        database = json.loads(metadata_path.read_bytes())
        if database.get("Version") != 2 or datetime.fromisoformat(
            str(database.get("NextUpdate", "")).replace("Z", "+00:00")
        ) <= datetime.now(UTC):
            raise ScanError(
                "Trivy vulnerability database is missing, incompatible or past its update deadline"
            )
        result["database"] = {
            key: database.get(key) for key in ("Version", "UpdatedAt", "NextUpdate", "DownloadedAt")
        }
        for index, image in enumerate(images):
            size = image["size_bytes"]
            if type(size) is not int or not 0 < size <= max_image_bytes:
                raise ScanError("An installed running image exceeds the reviewed export size bound")
            require_scratch_space(root, work, max(2 * GIB, 2 * size + GIB))
            archive = work / "image.tar"
            commands.run(["docker", "image", "save", "--output", str(archive), image["image_id"]])
            require_scratch_space(root, work)
            if not archive.is_file() or archive.stat().st_size > max_image_bytes + GIB:
                raise ScanError("The exported image archive exceeds its size bound")
            configs = archive_config_ids(archive, image)
            with archive.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            name = f"image-{index}.json"
            run_scanner(
                commands,
                scanner,
                work,
                [
                    "--input",
                    "/input/image.tar",
                    "--format",
                    "json",
                    "--output",
                    f"/reports/{name}",
                    "--scanners",
                    "vuln",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--ignore-unfixed",
                    "--ignorefile",
                    "/policy/ignore",
                    "--exit-code",
                    "0",
                    "--list-all-pkgs=false",
                    "--parallel",
                    "1",
                    "--offline-scan",
                    "--skip-db-update",
                    "--skip-java-db-update",
                ],
                archive,
            )
            require_scratch_space(root, work)
            findings = findings_from_report(work / "reports" / name, image, configs)
            result["images"].append(
                {
                    **image,
                    "archive_sha256": digest,
                    "platform_config_ids": sorted(configs),
                    "findings": findings,
                    "status": "vulnerable" if findings else "passed",
                }
            )
            archive.unlink()
        if containers != running_containers(commands):
            raise ScanError("The running deployment changed during scanning; retry the new snapshot")
        final_bindings, final_release = load_bindings(
            root, {row["service"] for row in containers}, read, retained_admin_image
        )
        if final_bindings != bindings or final_release != release:
            raise ScanError("Deployment metadata changed during scanning; the snapshot cannot pass")
    commands.remaining()
    result["finding_count"] = sum(len(image["findings"]) for image in result["images"])
    # Keep the redacted report consumable by report_trivy_findings.py; its
    # collect_findings gate sees exactly the same actionable vulnerabilities.
    result["Results"] = [
        {
            "Target": ",".join(image["services"]) + " " + image["image_id"],
            "Vulnerabilities": [
                {
                    "VulnerabilityID": finding["id"],
                    "PkgName": finding["package"],
                    "InstalledVersion": finding["installed"],
                    "FixedVersion": finding["fixed"],
                    "Severity": finding["severity"],
                }
                for finding in image["findings"]
            ],
        }
        for image in result["images"]
    ]
    result["status"] = (
        "vulnerable"
        if result["finding_count"]
        else "identity_drift"
        if result["identity_status"] != "consistent"
        else "passed"
    )
    result["finished_at"] = datetime.now(UTC).isoformat()
    return result


@contextmanager
def release_lock():
    import fcntl

    descriptor = os.open("/run/lock/project-snow-release.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ScanError("The release lock is not root-controlled")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScanError("A release or maintenance operation is already running") from exc
        yield
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/project-snow"))
    parser.add_argument("--trivy-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, help="Private temporary work parent with enough disk space")
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    parser.add_argument(
        "--retained-admin-image", help="Acknowledge one exact unbound admin digest for scanning only"
    )
    args = parser.parse_args(argv)
    output_ready = False
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise ScanError("Run on the Linux release host as root")
        if not 60 <= args.deadline_seconds <= 3600:
            raise ScanError("Total deadline must be between 60 and 3600 seconds")
        if args.output.exists() or args.output.is_symlink():
            raise ScanError("The report path already exists; use a new path")
        controlled_directory(args.output.parent)
        output_ready = True
        if args.work_root is not None:
            controlled_directory(args.work_root)
        with release_lock():
            report = scan(
                args.root,
                args.trivy_image,
                Commands(args.deadline_seconds),
                work_root=args.work_root,
                retained_admin_image=args.retained_admin_image,
            )
        exit_code = 0 if report["status"] == "passed" else 1
    except (ScanError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        report = {
            "schema_version": "project-snow-running-image-scan-1",
            "status": "error",
            "error": str(exc) if isinstance(exc, ScanError) else type(exc).__name__,
        }
        exit_code = 2
    if not output_ready:
        print(json.dumps(report))
        return 2
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        print(json.dumps({"status": "error", "error": "Could not create an exclusive private report"}))
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "finding_count": report.get("finding_count"),
                "scanned_images": len(report.get("images", [])),
                "report": str(args.output),
            }
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
