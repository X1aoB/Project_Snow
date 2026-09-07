"""Persistent Caddy routing and verified retention of unchanged edge containers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Callable

from maintenance import MaintenanceError, OBJECT_ID, Paths, active_release, read_text, require_inherited_release_lock, run
from release_state import atomic_write, digest

IMPORT = "import /etc/project-snow-routing/upstream.caddy"
SERVICES = {"caddy", "cloudflared", "egress-proxy"}


def routing_root(paths: Paths) -> Path:
    for parent in (paths.root, paths.root / "runtime"):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
            raise MaintenanceError("Routing parent directory must be root-controlled")
    root = paths.root / "runtime" / "routing"
    root.mkdir(mode=0o755, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
        raise MaintenanceError("Routing directory must be root-controlled")
    return root


def select_legacy(paths: Paths, configuration: Path, colour: str) -> dict:
    if IMPORT in read_text(configuration / "infra" / "Caddyfile"):
        raise MaintenanceError("Persistent targets must use the validated reload path")
    directory = paths.root / "runtime" / "routing"
    if not directory.exists() and not directory.is_symlink():
        return {"status": "legacy", "colour": colour}
    root = routing_root(paths)
    # A failed new-config transition may already have replaced the file. Keep
    # its startup source consistent even when rollback recreates legacy Caddy.
    atomic_write(root / "upstream.caddy", route_payload(colour), 0o644)
    return {"status": "legacy-route-restored", "colour": colour}


def route_payload(colour: str) -> bytes:
    if colour not in {"blue", "green"}:
        raise MaintenanceError("Invalid persistent upstream colour")
    return f"to public-api-{colour}:8000\n".encode()


def fingerprint(container: dict) -> str:
    # Ignore process times and endpoint IPs, which legitimately change on a
    # restart. Preserve configuration, mounts and network identities so manual
    # changes invalidate retention and force the existing reconciliation path.
    document = {"Id": container["Id"], "Image": container["Image"], "Config": container["Config"],
                "HostConfig": container["HostConfig"], "Mounts": container.get("Mounts", []),
                "Networks": {name: value.get("NetworkID") for name, value in
                             container.get("NetworkSettings", {}).get("Networks", {}).items()}}
    return digest(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())


def live_container(service: str, execute: Callable = run) -> dict | None:
    identifiers = execute(["docker", "ps", "--quiet", "--no-trunc", "--filter", "label=com.docker.compose.project=project-snow-public",
                           "--filter", f"label=com.docker.compose.service={service}"]).split()
    if not identifiers:
        return None
    if len(identifiers) != 1 or not OBJECT_ID.fullmatch(identifiers[0]):
        raise MaintenanceError("Edge service does not have exactly one known running container")
    containers = json.loads(execute(["docker", "inspect", identifiers[0]]))
    if len(containers) != 1 or containers[0].get("Id") != identifiers[0]:
        raise MaintenanceError("Edge container identity changed during inspection")
    return containers[0]


def service_models(paths: Paths, environment: Path, configuration: Path, colour: str, execute: Callable = run) -> dict:
    read_text(environment)
    if not configuration.is_relative_to(paths.root / "releases" / "configurations"):
        raise MaintenanceError("Routing configuration is outside the immutable release namespace")
    read_text(configuration / "compose.prod.yml")
    document = json.loads(execute(["docker", "compose", "--env-file", str(environment), "-f", str(configuration / "compose.prod.yml"),
                                   "--profile", colour, "config", "--format", "json"]))
    models = {}
    for name in SERVICES & set(document.get("services", {})):
        service = document["services"][name]
        for mount in service.get("volumes", []):
            if mount.get("type") == "bind":
                source = Path(mount["source"])
                if source.is_relative_to(configuration):
                    # Compose embeds a release-specific path even if immutable
                    # config bytes are unchanged. Compare its content identity.
                    mount["source"] = "configuration-sha256:" + digest(read_text(source).encode())
        networks = {network: document.get("networks", {}).get(network) for network in service.get("networks", {})}
        models[name] = digest(json.dumps({"service": service, "networks": networks}, sort_keys=True, separators=(",", ":")).encode())
    return models


def saved_bindings(root: Path) -> dict:
    source = root / "live-services.json"
    if not source.exists() and not source.is_symlink():
        return {}
    document = json.loads(read_text(source))
    if document.get("schema_version") != "project-snow-edge-binding-1" or not isinstance(document.get("services"), dict):
        raise MaintenanceError("Persistent edge binding is invalid")
    return document["services"]


def prepare(paths: Paths, environment: Path, configuration: Path, colour: str, services: list[str], execute: Callable = run) -> dict:
    if not services or set(services) - SERVICES:
        raise MaintenanceError("Unknown edge service requested")
    if IMPORT not in read_text(configuration / "infra" / "Caddyfile"):
        raise MaintenanceError("Target Caddy configuration does not support persistent routing")
    root = routing_root(paths)
    upstream = root / "upstream.caddy"
    previous = read_text(upstream).encode() if upstream.exists() or upstream.is_symlink() else route_payload(active_release(paths)["colour"])
    if previous not in {route_payload("blue"), route_payload("green")}:
        raise MaintenanceError("Unexpected persistent upstream configuration")
    models = service_models(paths, environment, configuration, colour, execute)
    saved = saved_bindings(root)
    retain, recreate = [], []
    containers = {}
    for service in services:
        container = live_container(service, execute)
        containers[service] = container
        binding = saved.get(service) or {}
        if (container and models.get(service) and binding.get("model_sha256") == models[service]
                and binding.get("container_sha256") == fingerprint(container)):
            retain.append(service)
        else:
            recreate.append(service)
    atomic_write(upstream, route_payload(colour), 0o644)
    if "caddy" in retain:
        identifier = containers["caddy"]["Id"]
        try:
            execute(["docker", "exec", identifier, "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
        except Exception:
            # A failed or uncertain reload must leave the on-disk startup
            # source pointing at the previous colour before shell rollback.
            atomic_write(upstream, previous, 0o644)
            try:
                execute(["docker", "exec", identifier, "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
            except Exception:
                pass
            raise MaintenanceError("Persistent Caddy reload failed; previous startup route restored") from None
    return {"status": "prepared", "colour": colour, "retained": retain, "recreate": recreate}


def record(paths: Paths, environment: Path, configuration: Path, colour: str, services: list[str], execute: Callable = run) -> dict:
    if not services or set(services) - SERVICES:
        raise MaintenanceError("Unknown edge service requested")
    root = routing_root(paths)
    if read_text(root / "upstream.caddy").encode() != route_payload(colour):
        raise MaintenanceError("Persistent route differs from the selected target")
    models = service_models(paths, environment, configuration, colour, execute)
    saved = saved_bindings(root)
    for service in services:
        container = live_container(service, execute)
        if container is None or service not in models:
            raise MaintenanceError("Cannot bind a missing edge service")
        if service == "caddy":
            mounts = container.get("Mounts") or []
            route_mounts = [mount for mount in mounts if mount.get("Destination") == "/etc/project-snow-routing"]
            if (len(route_mounts) != 1 or route_mounts[0].get("Source") != str(root)
                    or route_mounts[0].get("RW") is not False):
                raise MaintenanceError("Caddy does not mount the persistent routing directory read-only")
        saved[service] = {"container_id": container["Id"], "container_sha256": fingerprint(container), "model_sha256": models[service]}
    atomic_write(root / "live-services.json", (json.dumps({"schema_version": "project-snow-edge-binding-1", "services": saved}, sort_keys=True) + "\n").encode())
    return {"status": "recorded", "services": services}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "record", "legacy"))
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--colour", choices=("blue", "green"), required=True)
    parser.add_argument("--services", nargs="+", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise MaintenanceError("Persistent edge routing requires root")
        paths = Paths()
        require_inherited_release_lock(paths.lock)
        if args.operation == "legacy":
            result = select_legacy(paths, args.configuration, args.colour)
        else:
            function = prepare if args.operation == "prepare" else record
            result = function(paths, args.environment, args.configuration, args.colour, args.services)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MaintenanceError, ValueError, OSError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, MaintenanceError) else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
