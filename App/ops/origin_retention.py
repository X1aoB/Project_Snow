"""Read-only equivalence gate for an already validated live origin listener.

This does not manage origin-edge as an application routing service. The caller
must first validate its durable binding, TLS material and live network policy.
Exit 10 means a real model difference and permits the existing replacement
workflow; an inspection/validation failure must preserve the listener and fail.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Callable

from maintenance import DIGEST, MaintenanceError, Paths, read_environment, require_inherited_release_lock

SERVICE_KEYS = {
    "cap_add", "cap_drop", "command", "cpus", "depends_on", "dns", "entrypoint", "image", "logging",
    "mem_limit", "networks", "pids_limit", "ports", "read_only", "restart", "security_opt",
    "tmpfs", "volumes",
}
TLS_NAMES = {"origin-cert.pem", "origin-key.pem", "aop-ca.pem"}
TLS_SCHEMA = "project-snow-origin-tls-1"


def require(condition: object, message: str) -> None:
    if not condition:
        raise MaintenanceError(message)


def controlled_directory(path: Path) -> None:
    require(path.is_absolute() and ".." not in path.parts, "Origin retention requires absolute controlled paths")
    for parent in (path, *path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0 and not info.st_mode & 0o022,
                "Origin retention directory is not root-controlled")


def read_payload(path: Path, *, mode: int | None = None) -> bytes:
    controlled_directory(path.parent)
    # Open special files without waiting for a writer/device before fstat can
    # reject them. CLOEXEC also prevents inspection descriptors leaking to tools.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and 0 < info.st_size <= 1024 * 1024
                and (mode is None or stat.S_IMODE(info.st_mode) == mode), "Origin retention file metadata is unsafe")
        payload = source.read(1024 * 1024 + 1)
        after = os.fstat(source.fileno())
        current = path.lstat()
        require(len(payload) == info.st_size
                and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
                "Origin retention file changed during inspection")
    return payload


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class DeadlineCommands:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + 60

    def __call__(self, command: list[str]) -> str:
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Origin retention inspection exceeded its deadline")
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=min(15, remaining))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MaintenanceError("Origin retention inspection failed or timed out") from error
        require(result.returncode == 0 and len(result.stdout) <= 4 * 1024 * 1024,
                "Origin retention inspection failed or returned oversized metadata")
        return result.stdout


def tls_identity(root: Path, configuration: Path) -> dict:
    # Keep the directory identity; equal certificate bytes in a different TLS
    # release must still go through the explicit replacement workflow.
    require(re.fullmatch(r"/etc/project-snow/origin-edge/releases/[0-9a-f]{64}", root.as_posix()),
            "Origin retention requires a versioned TLS directory")
    controlled_directory(root)
    require(stat.S_IMODE(root.stat().st_mode) == 0o700, "Origin TLS directory permissions changed")
    metadata = json.loads(read_payload(root / "metadata.json", mode=0o400))
    hashes = {name: sha256(read_payload(root / name, mode=0o400)) for name in sorted(TLS_NAMES)}
    expected = {"schema_version": "project-snow-origin-tls-install-1", "hostname": "snow.xiaob.dev",
                "bundle_sha256": root.name, "origin_certificate_sha256": hashes["origin-cert.pem"],
                "aop_ca_sha256": hashes["aop-ca.pem"], "origin_private_key_sha256": hashes["origin-key.pem"]}
    identity = f"{TLS_SCHEMA}\nsnow.xiaob.dev\n{hashes['origin-cert.pem']}\n{hashes['aop-ca.pem']}\n".encode("ascii")
    require(metadata == expected and root.name == sha256(identity), "Installed origin TLS content identity changed")
    for name in ("origin-cert.pem", "aop-ca.pem"):
        require(sha256(read_payload(configuration / "config/origin-edge" / name)) == hashes[name],
                "Origin TLS differs from the immutable configuration")
    # Hashes stay in process memory. Neither the key nor its digest is logged.
    return {"root": str(root), "files": hashes}


def normalized_model(document: dict, configuration: Path, file_hash: Callable[[Path], str]) -> dict:
    require(document.get("name") == "project-snow-public", "Origin Compose project identity changed")
    service = deepcopy(document.get("services", {}).get("origin-edge"))
    require(isinstance(service, dict), "Origin service is missing from the rendered configuration")
    # Compose renders an omitted entrypoint as null on the production version.
    # Both inherit the image default; [] explicitly clears it and is distinct.
    if service.get("entrypoint") is None:
        service["entrypoint"] = None
    mounts = service.get("volumes", [])
    require(isinstance(mounts, list), "Origin mount model is invalid")
    config_mounts = 0
    for mount in mounts:
        if mount.get("source") == str(configuration / "infra/OriginEdge.Caddyfile"):
            require(mount.get("type") == "bind" and mount.get("read_only") is True
                    and mount.get("target") == "/etc/caddy/OriginEdge.Caddyfile", "Origin configuration mount changed")
            mount["source"] = "configuration-sha256:" + file_hash(configuration / "infra/OriginEdge.Caddyfile")
            config_mounts += 1
    require(config_mounts == 1, "Origin must bind exactly one immutable Caddy configuration")
    # Preserve every option, duplicate and future field. Only an exact verified
    # Caddyfile source is normalized; TLS and other mount paths are untouched.
    network_names = service.get("networks", {})
    require(isinstance(network_names, dict) and network_names, "Origin service networks are invalid")
    networks = {name: document.get("networks", {}).get(name) for name in network_names}
    require(all(isinstance(value, dict) for value in networks.values()), "Origin network definition is missing")
    return {"service": service, "networks": networks}


def runtime_matches(model: dict, container: dict, image: dict, networks: dict) -> None:
    """Supplement the shell's exact port/mount/TLS/network hardening checks."""
    service = model["service"]
    # New Compose features cannot silently bypass the applied-runtime checks.
    require(not set(service) - SERVICE_KEYS, "Origin service has unsupported runtime fields; maintenance review required")
    config, host = container.get("Config", {}), container.get("HostConfig", {})
    require(container.get("State", {}).get("Running") is True
            and container.get("Image") == image.get("Id") and re.fullmatch(r"sha256:[0-9a-f]{64}", str(image.get("Id")))
            and config.get("Image") == service["image"] and DIGEST.fullmatch(service["image"]),
            "Origin runtime image does not match its immutable reference")
    labels = config.get("Labels") or {}
    require(labels.get("com.docker.compose.project") == "project-snow-public"
            and labels.get("com.docker.compose.service") == "origin-edge", "Origin runtime service identity changed")
    image_config = image.get("Config") or {}
    require(service.get("entrypoint") is None, "Origin entrypoint override requires maintenance review")
    require(config.get("Cmd") == service.get("command")
            and config.get("Entrypoint") == image_config.get("Entrypoint")
            and (config.get("Env") or []) == (image_config.get("Env") or [])
            and (config.get("User") or "") == (image_config.get("User") or "")
            and (config.get("WorkingDir") or "") == (image_config.get("WorkingDir") or ""),
            "Origin runtime process configuration changed")
    memory = int(service["mem_limit"])
    require(host.get("NanoCpus") == int(Decimal(str(service["cpus"])) * 1000000000)
            and host.get("Memory") == memory and host.get("PidsLimit") == service["pids_limit"]
            and host.get("MemorySwap", 0) in (0, memory * 2)
            and all(host.get(key, 0) == 0 for key in ("CpuShares", "CpuPeriod", "CpuQuota", "MemoryReservation"))
            and not host.get("CpusetCpus") and not host.get("CpusetMems") and not host.get("OomKillDisable"),
            "Origin runtime resource limits changed")
    logging = service["logging"]
    require(host.get("LogConfig") == {"Type": logging["driver"], "Config": logging.get("options") or {}},
            "Origin runtime log bounds changed")
    expected_tmpfs = dict(item.split(":", 1) for item in service["tmpfs"])
    require(host.get("Tmpfs") == expected_tmpfs and host.get("RestartPolicy") ==
            {"Name": service["restart"], "MaximumRetryCount": 0}, "Origin temporary storage or restart policy changed")
    require(not host.get("Privileged") and not host.get("PublishAllPorts") and not host.get("Devices")
            and not host.get("DeviceRequests") and not host.get("PidMode") and not host.get("UTSMode")
            and host.get("ReadonlyRootfs") is service["read_only"] and host.get("Dns") == service["dns"],
            "Origin runtime isolation changed")
    def capabilities(values: list[str]) -> list[str]:
        return sorted(value.removeprefix("CAP_") for value in values)
    require(capabilities(host.get("CapAdd") or []) == capabilities(service["cap_add"])
            and capabilities(host.get("CapDrop") or []) == capabilities(service["cap_drop"])
            and sorted(host.get("SecurityOpt") or []) == sorted(service["security_opt"]),
            "Origin runtime hardening changed")
    attached = container.get("NetworkSettings", {}).get("Networks", {})
    expected_names = {value["name"] for value in model["networks"].values()}
    require(set(attached) == expected_names == set(networks), "Origin runtime network set changed")
    for value in model["networks"].values():
        name = value["name"]
        observed = networks[name]
        require(attached[name].get("NetworkID") == observed.get("Id")
                and re.fullmatch(r"[0-9a-f]{64}", str(observed.get("Id")))
                and observed.get("Name") == name and observed.get("Driver") == value.get("driver", "bridge")
                and observed.get("Internal") is value.get("internal", False)
                and (observed.get("Options") or {}) == (value.get("driver_opts") or {}),
                "Origin runtime network identity or policy changed")


def equivalent(paths: Paths, target_environment: Path, target_configuration: Path, target_colour: str,
               retained_environment: Path, retained_configuration: Path, retained_colour: str,
               execute: Callable) -> bool:
    snapshots = []
    for environment, configuration, colour in (
        (retained_environment, retained_configuration, retained_colour),
        (target_environment, target_configuration, target_colour),
    ):
        require(colour in {"blue", "green"} and configuration.parent == paths.root / "releases/configurations"
                and re.fullmatch(r"[0-9a-f]{40}", configuration.name), "Origin configuration namespace is invalid")
        controlled_directory(environment.parent)
        values = read_environment(environment)
        reference = values.get("CADDY_IMAGE", "")
        require(DIGEST.fullmatch(reference), "Origin image must use an immutable reference")
        read_payload(configuration / "compose.prod.yml")
        document = json.loads(execute(["docker", "compose", "--env-file", str(environment), "-f",
                                       str(configuration / "compose.prod.yml"), "--profile", colour,
                                       "config", "--format", "json"]))
        model = normalized_model(document, configuration, lambda path: sha256(read_payload(path)))
        require(model["service"].get("image") == reference, "Rendered origin image differs from its environment")
        snapshots.append((model, values.get("ORIGIN_TLS_ROOT", ""), configuration))
    old, new = snapshots
    if old[0] != new[0] or old[1] != new[1]:
        return False
    old_tls = tls_identity(Path(old[1]), old[2])
    new_tls = tls_identity(Path(new[1]), new[2])
    require(old_tls == new_tls, "Origin TLS changed during equivalence inspection")
    identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter",
                           "label=com.docker.compose.project=project-snow-public", "--filter",
                           "label=com.docker.compose.service=origin-edge"]).split()
    require(len(identifiers) == 1 and re.fullmatch(r"[0-9a-f]{64}", identifiers[0]), "Origin runtime is not unique")
    inspected = json.loads(execute(["docker", "inspect", identifiers[0]]))
    images = json.loads(execute(["docker", "image", "inspect", old[0]["service"]["image"]]))
    require(len(inspected) == len(images) == 1 and inspected[0].get("Id") == identifiers[0],
            "Origin runtime changed during inspection")
    networks = {}
    for value in old[0]["networks"].values():
        observed = json.loads(execute(["docker", "network", "inspect", value["name"]]))
        require(len(observed) == 1, "Origin runtime network is not unique")
        networks[value["name"]] = observed[0]
    runtime_matches(old[0], inspected[0], images[0], networks)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for prefix in ("target", "retained"):
        parser.add_argument(f"--{prefix}-environment", type=Path, required=True)
        parser.add_argument(f"--{prefix}-configuration", type=Path, required=True)
        parser.add_argument(f"--{prefix}-colour", choices=("blue", "green"), required=True)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, "Origin equivalence inspection requires root")
        paths = Paths()
        require_inherited_release_lock(paths.lock)
        same = equivalent(paths, args.target_environment, args.target_configuration, args.target_colour,
                          args.retained_environment, args.retained_configuration, args.retained_colour, DeadlineCommands())
        return 0 if same else 10
    except (MaintenanceError, ValueError, OSError, KeyError, TypeError) as error:
        # Never print rendered Compose, container Env, TLS bytes or key hashes.
        print(str(error) if isinstance(error, MaintenanceError) else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
