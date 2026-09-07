#!/usr/bin/env python3
"""Restore the pinned baseline into disposable, offline Docker resources.

No production environment file is loaded. Only generated credentials are
injected. This is a measured component restore drill, not a whole-host RTO.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import time
import uuid
import zipfile

from maintenance import DIGEST, MaintenanceError, Paths, capacity, release_lock
from release_state import atomic_write

BASELINE_SHA = "502ec99412bef843c37e4b31a53df8fa9faeb33c"
PIN = "project-snow-pinned-" + BASELINE_SHA
BASE = "srv/project-snow/backups/overhaul-baseline-20260907"
CODE_ZIP = "baseline-502ec99-code.zip"
CODE_SHA256 = "439f3846ffa050ddaa7790d39b572fedd420d39d258499762cf0deea690e6189"
ANCHOR = "srv/project-snow/releases/anchors/baseline-502ec99-0.9.6-20260907/current-manifest.json"
LIMITS = {"postgres": (512, .5), "api": (1024, .75), "embedding": (768, .5), "qdrant": (512, .5), "neo4j": (1024, .75)}
LABEL = "io.project-snow.restore-drill"
HEALTH_PROBE = """import json, sys, urllib.error, urllib.request
endpoint = sys.argv[1]
if endpoint not in {'ready', 'full'}:
    raise SystemExit(2)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    response = opener.open('http://127.0.0.1:8000/public/v1/health/' + endpoint, timeout=15)
except urllib.error.HTTPError as error:
    response = error
with response:
    payload = response.read(131073)
if len(payload) > 131072:
    raise SystemExit(2)
document = json.loads(payload)
result = {key: document.get(key) for key in ('status', 'database', 'data', 'media', 'stickers') if isinstance(document.get(key), str)}
if endpoint == 'full':
    result['dependencies'] = {key: document.get('dependencies', {}).get(key) for key in ('embedding', 'qdrant', 'neo4j')}
print(json.dumps(result, sort_keys=True))
"""
REBUILD = """import os, runpy, sys, time
import httpx
from neo4j import GraphDatabase
deadline = time.monotonic() + 180
while True:
    try:
        with httpx.Client(timeout=8, trust_env=False) as client:
            client.get(os.environ['EMBEDDING_URL'] + '/health').raise_for_status()
            client.get(os.environ['QDRANT_URL'] + '/collections', headers={'api-key': os.environ['QDRANT_API_KEY']}).raise_for_status()
        with GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD']), connection_timeout=8) as driver:
            driver.verify_connectivity()
        break
    except Exception:
        if time.monotonic() >= deadline:
            raise SystemExit('Isolated dependency warm-up failed') from None
        time.sleep(2)
sys.argv = ['data_loader', '--release-root', '/restore/data', '--activate']
runpy.run_module('backend.snow_app.data_loader', run_name='__main__')
"""


def command_identity(arguments: list[str]) -> str:
    program = Path(arguments[0]).name if arguments else "unknown"
    if program not in {"docker", "restic"}:
        return "unknown"
    verbs = {"docker": {"exec", "start", "create", "inspect", "image", "network", "volume", "container", "ps", "rm"},
             "restic": {"restore", "snapshots"}}
    verb = arguments[1] if len(arguments) > 1 and arguments[1] in verbs[program] else "unknown"
    result = program + "/" + verb
    if program == "docker" and verb == "exec":
        for child in ("pg_isready", "pg_restore", "psql", "python"):
            if child in arguments:
                result += "/" + child
                if child == "pg_restore":
                    result += "/list" if "--list" in arguments else "/restore"
                break
    elif program == "docker" and verb in {"image", "network", "volume", "container"}:
        if len(arguments) > 2 and arguments[2] in {"create", "inspect", "ls", "rm"}:
            result += "/" + arguments[2]
    return result


class DrillCommandError(MaintenanceError):
    def __init__(self, arguments: list[str], outcome: str, stderr: bytes | str | None):
        super().__init__("Isolated drill command failed (" + command_identity(arguments) + ", " + outcome + ")")
        self.stderr = (stderr.encode() if isinstance(stderr, str) else stderr or b"")[:1024 * 1024]


def command(arguments: list[str], *, stdin: Path | None = None, timeout: int = 900) -> str:
    try:
        with stdin.open("rb") if stdin else open(os.devnull, "rb") as source:
            result = subprocess.run(arguments, stdin=source, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise DrillCommandError(arguments, "timeout", error.stderr) from None
    if result.returncode:
        # Database errors, archive paths and container logs can contain private
        # material. The receipt records the failed phase, never raw stderr.
        raise DrillCommandError(arguments, f"exit {result.returncode}", result.stderr)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise MaintenanceError("Drill command exceeded its bounded metadata output")
    return result.stdout.decode("utf-8").strip()


def diagnostic_executor(execute, receipt: dict, directory: Path):
    def tracked(arguments, **kwargs):
        identity = command_identity(arguments)
        receipt["last_command"] = identity
        try:
            return execute(arguments, **kwargs)
        except DrillCommandError as error:
            directory.mkdir(mode=0o700, exist_ok=True)
            phase = receipt["phase"]
            if not re.fullmatch("[a-z-]+", phase):
                raise MaintenanceError("Invalid diagnostic phase") from None
            destination = directory / (phase + "-" + identity.replace("/", "-") + ".stderr")
            atomic_write(destination, error.stderr or b"[command returned no stderr]\n")
            # Keep only the latest bounded stderr for each fixed operation;
            # failed warm-up probes cannot create an unbounded set of files.
            entries = receipt.setdefault("diagnostics", [])
            if str(destination) not in entries:
                entries.append(str(destination))
            raise
    return tracked


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in relative:
        raise MaintenanceError("Unsafe restored artifact path")
    path = root.joinpath(*name.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise MaintenanceError("Restored artifact escapes its private root")
    return path


def regular_bytes(path: Path, maximum: int = 4 * 1024 * 1024) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise MaintenanceError("Unsafe or oversized restored metadata")
    return path.read_bytes()


def expected_hash(document: object, filename: str) -> str:
    matches = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).replace("\\", "/").rsplit("/", 1)[-1] == filename and isinstance(item, str) and re.fullmatch("[0-9a-fA-F]{64}", item):
                    matches.add(item.lower())
                visit(item)
            identity = value.get("path", value.get("file", value.get("name", "")))
            if str(identity).replace("\\", "/").rsplit("/", 1)[-1] == filename:
                checksum = value.get("sha256", value.get("SHA256", ""))
                if isinstance(checksum, str) and re.fullmatch("[0-9a-fA-F]{64}", checksum):
                    matches.add(checksum.lower())
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(document)
    if len(matches) != 1:
        raise MaintenanceError("The baseline has no unique recorded artifact checksum")
    return matches.pop()


def tar_metadata(archive: Path, member_path: str) -> bytes:
    result = None
    with tarfile.open(archive, "r:*") as package:
        for count, member in enumerate(package, 1):
            if count > 20000:
                raise MaintenanceError("Configuration archive exceeds its member limit")
            normalized = member.name.removeprefix("./").lstrip("/")
            if ".." in PurePosixPath(normalized).parts or normalized != member_path:
                continue
            if result is not None or not member.isfile() or member.size > 1024 * 1024:
                raise MaintenanceError("Configuration archive contains unsafe or duplicate selected metadata")
            with package.extractfile(member) as source:
                result = source.read(1024 * 1024 + 1)
    if result is None:
        raise MaintenanceError("Required baseline metadata is absent from its configuration archive")
    return result


def verify_package(root: Path, kind: str, binding: dict) -> dict:
    manifest_bytes = regular_bytes(root / "manifest.json")
    if hashlib.sha256(manifest_bytes).hexdigest() != binding["manifest_sha256"]:
        raise MaintenanceError("Restored package manifest differs from its release binding")
    manifest = json.loads(manifest_bytes)
    entries = {}
    if kind == "data":
        for entry in manifest.get("files", []):
            name = str(entry.get("path", ""))
            if not name or name in entries:
                raise MaintenanceError("Restored data package has duplicate or empty file identities")
            entries[name] = (entry.get("sha256"), entry.get("bytes"))
    else:
        checksums = regular_bytes(root / "SHA256SUMS")
        if hashlib.sha256(checksums).hexdigest() != binding["checksums_sha256"]:
            raise MaintenanceError("Restored media checksum list differs from its release binding")
        for line in checksums.decode("utf-8").splitlines():
            checksum, separator, name = line.partition("  ")
            if not separator:
                checksum, separator, name = line.partition(" *")
            if not separator or name in entries:
                raise MaintenanceError("Restored media checksum list is malformed")
            entries[name] = (checksum, None)
    if not entries:
        raise MaintenanceError("Restored package has no verifiable assets")
    size = 0
    for name, (checksum, expected_size) in entries.items():
        if not re.fullmatch("[0-9a-f]{64}", str(checksum)):
            raise MaintenanceError("Invalid restored artifact digest")
        path = safe_child(root, name)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (expected_size is not None and info.st_size != expected_size) or file_hash(path) != checksum:
            raise MaintenanceError("Restored artifact checksum or size mismatch")
        size += info.st_size
    return {"status": "verified", "files": len(entries), "bytes": size}


def inspect_material(restored: Path) -> tuple[dict, dict, dict, Path, dict]:
    baseline = restored / BASE
    hashes = json.loads(regular_bytes(baseline / "sha256.json"))
    dump, archive, code = [baseline / name for name in ("postgres.dump", "host-configuration.tar", CODE_ZIP)]
    artifact_receipt = {}
    for path in (dump, archive, code):
        expected = CODE_SHA256 if path.name == CODE_ZIP else expected_hash(hashes, path.name)
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise MaintenanceError("Baseline recovery artifact does not match its recorded hash")
        artifact_receipt[path.name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
    with zipfile.ZipFile(code) as package:
        members = package.infolist()
        if len(members) > 20000 or sum(member.file_size for member in members) > 256 * 1024 * 1024:
            raise MaintenanceError("Baseline source archive exceeds its recovery bound")
        for member in members:
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts or stat.S_ISLNK(member.external_attr >> 16):
                raise MaintenanceError("Unsafe baseline source archive member")
        if package.testzip() is not None:
            raise MaintenanceError("Baseline source archive CRC verification failed")
    direct = restored / ANCHOR
    manifest = json.loads(regular_bytes(direct) if direct.exists() else tar_metadata(archive, ANCHOR))
    # Read only selected image coordinates from the authenticated backup; never
    # source public.env or extract /etc/project-snow production secrets.
    environment = tar_metadata(archive, "srv/project-snow/runtime/compose.env").decode("utf-8")
    images = {}
    for line in environment.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"POSTGRES_IMAGE", "QDRANT_IMAGE", "NEO4J_IMAGE", "EMBEDDING_IMAGE", "PUBLIC_API_IMAGE"}:
            if key in images or not DIGEST.fullmatch(value):
                raise MaintenanceError("Baseline contains an invalid or duplicate image pin")
            images[key] = value
    expected_api = manifest["application"]["image"] + "@" + manifest["application"]["digest"]
    expected_embedding = manifest["embedding"]["image"] + "@" + manifest["embedding"]["digest"]
    if images.get("PUBLIC_API_IMAGE") != expected_api or images.get("EMBEDDING_IMAGE") != expected_embedding or "POSTGRES_IMAGE" not in images:
        raise MaintenanceError("Baseline runtime and release manifest image identities disagree")
    roots, packages = {}, {}
    for kind, field, parent in (("data", "data_version", "data/releases"), ("avatar", "media_version", "media/releases"),
                               ("sticker", "sticker_version", "media/stickers/releases")):
        version = manifest[field]
        if not re.fullmatch("[A-Za-z0-9._-]+", version):
            raise MaintenanceError("Invalid restored package version")
        root = safe_child(restored, "srv/project-snow/" + parent + "/" + version)
        roots[kind] = root
        packages[kind] = verify_package(root, kind, manifest["release_artifacts"][kind])
    return manifest, images, roots, dump, {"artifacts": artifact_receipt, "packages": packages}


def generated_environments(work: Path, manifest: dict) -> dict[str, Path]:
    password = secrets.token_hex(24)
    neo_password = secrets.token_hex(24)
    qdrant_key = secrets.token_hex(24)
    key = lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    api = {"PUBLIC_DATABASE_URL": f"postgresql+psycopg://project_snow:{password}@postgres:5432/project_snow",
           "PUBLIC_ALLOW_INSECURE_DEV": "false", "PUBLIC_AUTO_CREATE_SCHEMA": "false", "PUBLIC_ENABLED_PROVIDERS": "",
           "PUBLIC_APP_VERSION": manifest["app_version"], "PUBLIC_DATA_VERSION": manifest["data_version"],
           "DATA_ROOT": "/restore/data", "APP_RUNTIME": "/restore/data", "PUBLIC_DATA_ROOT": "/restore/data",
           "PUBLIC_MEDIA_ROOT": "/restore/avatar", "PUBLIC_MEDIA_VERSION": manifest["media_version"],
           "PUBLIC_STICKER_ROOT": "/restore/sticker", "PUBLIC_STICKER_VERSION": manifest["sticker_version"],
           "PUBLIC_ORIGIN": "https://snow.xiaob.dev", "TURNSTILE_SITE_KEY": "isolated-drill-only", "TURNSTILE_SECRET": secrets.token_hex(24),
           "PUBLIC_CREDENTIAL_KEY": key(), "PUBLIC_STATE_HMAC_KEY": key(), "PUBLIC_IP_HMAC_KEY": key(), "PUBLIC_QQ_KEY": key(),
           "QDRANT_URL": "http://qdrant:6333", "QDRANT_COLLECTION": "project_snow_documents", "QDRANT_API_KEY": qdrant_key,
           "EMBEDDING_URL": "http://embedding:8000", "NEO4J_URI": "bolt://neo4j:7687", "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": neo_password,
           "PUBLIC_FEEDBACK_SMTP_HOST": "", "PUBLIC_FEEDBACK_SMTP_PASSWORD": "", "CHAT_ENABLED": "false", "MVP_CHAT_ENABLED": "false",
           "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*", "PYTHONDONTWRITEBYTECODE": "1"}
    environments = {"api": api, "postgres": {"POSTGRES_USER": "project_snow", "POSTGRES_DB": "project_snow", "POSTGRES_PASSWORD": password,
                                               "PGPASSWORD": password},
                    "qdrant": {"QDRANT__SERVICE__API_KEY": qdrant_key},
                    "embedding": {"EMBEDDING_MODEL": "/models/bge-small-zh-v1.5", "EMBEDDING_DIMENSION": "512", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
                    "neo4j": {"NEO4J_AUTH": "neo4j/" + neo_password, "NEO4J_server_memory_heap_initial__size": "256M",
                              "NEO4J_server_memory_heap_max__size": "256M", "NEO4J_server_memory_pagecache_size": "128M",
                              "NEO4J_db_tx__log_preallocate": "false", "NEO4J_db_tx__log_rotation_size": "16M"}}
    result = {}
    for role, environment in environments.items():
        path = work / (role + ".env")
        atomic_write(path, "".join(name + "=" + value + "\n" for name, value in environment.items()).encode())
        result[role] = path
    return result


class Sandbox:
    def __init__(self, work: Path, execute=command):
        self.work, self.execute = work, execute
        self.identity = "snow-restore-drill-" + uuid.uuid4().hex
        self.network = None
        self.network_attempted = False
        self.containers = []
        self.container_names = set()
        self.roles = {}
        self.volumes = []
        self.planned_volumes = set()

    def open(self):
        self.network_attempted = True
        identifier = self.execute(["docker", "network", "create", "--internal", "--label", LABEL + "=" + self.identity, self.identity])
        if not re.fullmatch("[0-9a-f]{64}", identifier):
            raise MaintenanceError("Docker returned an invalid private network identity")
        self.network = identifier
        network = json.loads(self.execute(["docker", "network", "inspect", self.network]))[0]
        if network.get("Internal") is not True or network.get("Name") != self.identity:
            raise MaintenanceError("Drill network is not the intended isolated network")

    def create(self, role: str, image: str, environment: Path, *, mounts: list[str] = (), arguments: list[str] = (),
               entrypoint: str | None = None) -> str:
        if role not in LIMITS or not DIGEST.fullmatch(image) or not self.network:
            raise MaintenanceError("Invalid isolated container specification")
        if environment.is_symlink() or not environment.resolve().is_relative_to(self.work.resolve()):
            raise MaintenanceError("Only generated private drill environments may be injected")
        # Images must already exist under the exact verified digest. This path
        # neither pulls images nor loads an unreviewed archive into the daemon.
        self.execute(["docker", "image", "inspect", image])
        memory, cpu = LIMITS[role]
        name = self.identity + "-" + role + "-" + uuid.uuid4().hex[:8]
        args = ["docker", "create", "--pull=never", "--network", self.network, "--network-alias", role,
                "--name", name,
                "--label", LABEL + "=" + self.identity, "--memory", f"{memory}m", "--memory-swap", f"{memory}m", "--cpus", str(cpu),
                "--pids-limit", "128", "--security-opt", "no-new-privileges:true", "--log-opt", "max-size=5m", "--log-opt", "max-file=1",
                "--env-file", str(environment)]
        if role == "api":
            args += ["--read-only", "--user", "0:0", "--cap-drop=ALL", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
        if role in {"postgres", "qdrant", "neo4j"}:
            volume = self.identity + "-" + role
            self.planned_volumes.add(volume)
            self.execute(["docker", "volume", "create", "--label", LABEL + "=" + self.identity, volume])
            self.volumes.append(volume)
            destination = {"postgres": "/var/lib/postgresql/data", "qdrant": "/qdrant/storage", "neo4j": "/data"}[role]
            args += ["--mount", f"type=volume,source={volume},target={destination}"]
        for mount in mounts:
            fields = dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
            source = Path(fields.get("source", ""))
            if (fields.get("type") != "bind" or not source.resolve().is_relative_to(self.work.resolve())
                    or source.is_symlink() or "readonly" not in mount.split(",")
                    or fields.get("target") not in {"/restore/data", "/restore/avatar", "/restore/sticker"}):
                raise MaintenanceError("Drill mounts must be read-only restored artifacts inside the private work directory")
            args += ["--mount", mount]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        # Record the random name before asking Docker to create anything. A
        # timed-out CLI can leave a daemon-side resource without returning ID.
        self.container_names.add(name)
        identifier = self.execute([*args, image, *arguments])
        if not re.fullmatch("[0-9a-f]{64}", identifier):
            raise MaintenanceError("Docker returned an invalid isolated container identity")
        self.containers.append(identifier)
        self.roles[identifier] = role
        return identifier

    def start(self, identifier: str, *, attached=False):
        return self.execute(["docker", "start", *( ["--attach"] if attached else []), identifier])

    def close(self) -> list[str]:
        failures = []
        label = LABEL + "=" + self.identity
        try:
            if self.container_names:
                found = self.execute(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=" + label], timeout=15).split()
                if len(found) > 16 or any(not re.fullmatch("[0-9a-f]{64}", item) for item in found):
                    raise MaintenanceError("Unexpected owned drill container discovery")
                for identifier in found:
                    if identifier in self.containers:
                        continue
                    template = '{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}}}'
                    info = json.loads(self.execute(["docker", "container", "inspect", "--format", template, identifier], timeout=15))
                    if (info.get("id") != identifier or info.get("name", "").lstrip("/") not in self.container_names
                            or info.get("labels", {}).get(LABEL) != self.identity):
                        raise MaintenanceError("Unacknowledged drill container ownership differs")
                    self.containers.append(identifier)
            if self.network_attempted and not self.network:
                found = self.execute(["docker", "network", "ls", "--quiet", "--no-trunc", "--filter", "label=" + label], timeout=15).split()
                if len(found) > 1 or any(not re.fullmatch("[0-9a-f]{64}", item) for item in found):
                    raise MaintenanceError("Unexpected owned drill network discovery")
                for identifier in found:
                    info = json.loads(self.execute(["docker", "network", "inspect", identifier], timeout=15))[0]
                    if info.get("Name") != self.identity or info.get("Labels", {}).get(LABEL) != self.identity:
                        raise MaintenanceError("Unacknowledged drill network ownership differs")
                    self.network = identifier
            if self.planned_volumes - set(self.volumes):
                found = self.execute(["docker", "volume", "ls", "--quiet", "--filter", "label=" + label], timeout=15).split()
                if len(found) > 3:
                    raise MaintenanceError("Unexpected owned drill volume discovery")
                for name in found:
                    if name in self.volumes:
                        continue
                    info = json.loads(self.execute(["docker", "volume", "inspect", name], timeout=15))[0]
                    if name not in self.planned_volumes or info.get("Name") != name or info.get("Labels", {}).get(LABEL) != self.identity:
                        raise MaintenanceError("Unacknowledged drill volume ownership differs")
                    self.volumes.append(name)
        except Exception:
            failures.append("discovery:" + self.identity)
        for kind, items in (("container", reversed(self.containers)), ("network", [self.network] if self.network else []), ("volume", reversed(self.volumes))):
            for identity in items:
                try:
                    if kind == "container":
                        self.execute(["docker", "rm", "--force", "--volumes", identity], timeout=15)
                    else:
                        self.execute(["docker", kind, "rm", identity], timeout=15)
                except Exception:
                    failures.append(kind + ":" + identity)
        return failures


def restore_database(sandbox: Sandbox, postgres: str, dump: Path) -> dict:
    if postgres not in sandbox.containers or sandbox.roles.get(postgres) != "postgres":
        raise MaintenanceError("Database restore requires a PostgreSQL container created by this isolated drill")
    execute = sandbox.execute
    deadline = time.monotonic() + 120
    while True:
        try:
            execute(["docker", "exec", "--user", "postgres", postgres, "pg_isready", "-h", "127.0.0.1", "-U", "project_snow", "-d", "project_snow"])
            break
        except MaintenanceError:
            if time.monotonic() >= deadline:
                raise MaintenanceError("Isolated PostgreSQL did not become ready")
            time.sleep(1)
    prefix = ["docker", "exec", "-i", "--user", "postgres", postgres]
    toc = execute([*prefix, "pg_restore", "--list"], stdin=dump)
    expected_tables = sorted(re.findall(r"^\d+; \d+ \d+ TABLE public (\S+) \S+$", toc, re.MULTILINE))
    if not expected_tables:
        raise MaintenanceError("Baseline dump contains no public table definitions")
    execute([*prefix, "pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--clean", "--if-exists",
             "-h", "127.0.0.1", "-U", "project_snow", "-d", "project_snow"], stdin=dump)
    sql = """CREATE TEMP TABLE drill_counts(name text, row_count bigint);
DO $$DECLARE entry record; BEGIN FOR entry IN SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename LOOP
EXECUTE format('INSERT INTO drill_counts SELECT %L, count(*) FROM public.%I',entry.tablename,entry.tablename); END LOOP; END$$;
SELECT json_build_object('counts',(SELECT json_object_agg(name,row_count) FROM drill_counts),
'unvalidated_constraints',(SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated),
'migration_heads',(SELECT json_agg(version_num ORDER BY version_num) FROM alembic_version),
'schema',(SELECT json_agg(row_to_json(c) ORDER BY c.table_name,c.ordinal_position) FROM
(SELECT table_name,column_name,data_type,is_nullable,ordinal_position FROM information_schema.columns WHERE table_schema='public') c));
"""
    payload = sandbox.work / "count-receipt.sql"
    atomic_write(payload, sql.encode())
    response = execute([*prefix, "psql", "-h", "127.0.0.1", "-U", "project_snow", "-d", "project_snow", "-qAt", "-v", "ON_ERROR_STOP=1"], stdin=payload)
    document = json.loads(response)
    if sorted(document["counts"]) != expected_tables or document["unvalidated_constraints"] != 0:
        raise MaintenanceError("Restored schema or constraints differ from dump definitions")
    schema_hash = hashlib.sha256(json.dumps(document.pop("schema"), sort_keys=True).encode()).hexdigest()
    return {"status": "restored", **document, "schema_sha256": schema_hash, "dump_tables": expected_tables,
            "historical_count_comparison": "unavailable: no row-count receipt was captured at baseline"}


def health(sandbox: Sandbox, api: str, *, full: bool) -> dict:
    if api not in sandbox.containers or sandbox.roles.get(api) != "api":
        raise MaintenanceError("Health probes require an API container created by this isolated drill")
    # Docker internal networks need not expose published ports on the host.
    # Exec a bounded stdlib probe in this exact container instead: no host port,
    # no external route, no production credentials, only two fixed GET paths.
    prefix = ["docker", "exec", api, "python", "-c", HEALTH_PROBE]
    deadline = time.monotonic() + 180
    while True:
        try:
            ready = json.loads(sandbox.execute([*prefix, "ready"], timeout=20))
            if ready.get("status") == "ok":
                break
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise MaintenanceError("Restored API did not pass DB/data/media readiness")
        time.sleep(2)
    result = {"readiness": "ok", "database": ready.get("database"), "data": ready.get("data"), "media": ready.get("media"), "stickers": ready.get("stickers")}
    if full:
        report = json.loads(sandbox.execute([*prefix, "full"], timeout=20))
        if report.get("status") != "ok" or any(report.get("dependencies", {}).get(name) != "ok" for name in ("embedding", "qdrant", "neo4j")):
            raise MaintenanceError("Restored retrieval dependencies did not pass full health")
        result["retrieval"] = "ok"
    else:
        result["retrieval"] = "not exercised; use --full for isolated index rebuild"
    return result


def drill(paths: Paths, *, snapshot: str, metadata_snapshot: str, full: bool = False, execute=command) -> dict:
    started = time.monotonic()
    capacity(paths, 0)
    for identifier in (snapshot, metadata_snapshot):
        if not re.fullmatch("[0-9a-f]{8,64}", identifier):
            raise MaintenanceError("Use an explicit baseline snapshot ID")
        snapshots = json.loads(execute(["restic", "snapshots", "--json", identifier]))
        if len(snapshots) != 1 or PIN not in snapshots[0].get("tags", []):
            raise MaintenanceError("Selected snapshot is not the pinned baseline")
    parent = paths.root / "backups" / "restore-drills"
    parent.mkdir(mode=0o700, exist_ok=True)
    for directory in (paths.root, paths.root / "backups", parent):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
            raise MaintenanceError("Drill directory is not root-controlled")
    if parent.stat().st_mode & 0o077:
        raise MaintenanceError("Drill working directories must be private")
    work = parent / ("run-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    restored = work / "restored"
    restored.mkdir(mode=0o700)
    receipt = {"schema_version": "project-snow-restore-drill-1", "started_at": datetime.now(timezone.utc).isoformat(),
               "snapshot": snapshot, "metadata_snapshot": metadata_snapshot, "baseline_sha": BASELINE_SHA,
               "scope": "database, assets, API" + (", isolated retrieval rebuild" if full else ""), "phase": "restore-material"}
    diagnostics = parent / (work.name + ".diagnostics")
    execute = diagnostic_executor(execute, receipt, diagnostics)
    sandbox = Sandbox(work, execute)
    try:
        execute(["restic", "restore", snapshot, "--target", str(restored), "--verify"], timeout=1800)
        execute(["restic", "restore", metadata_snapshot, "--target", str(restored), "--verify", "--include", "/" + BASE + "/**"], timeout=900)
        manifest, images, roots, dump, materials = inspect_material(restored)
        receipt.update(materials)
        capacity(paths, 0)
        environments = generated_environments(work, manifest)
        receipt["phase"] = "restore-database"
        sandbox.open()
        postgres = sandbox.create("postgres", images["POSTGRES_IMAGE"], environments["postgres"])
        sandbox.start(postgres)
        receipt["database"] = restore_database(sandbox, postgres, dump)
        if sorted(receipt["database"]["migration_heads"]) != sorted(manifest["migration_heads"]):
            raise MaintenanceError("Restored Alembic head differs from the baseline manifest")
        mounts = [f"type=bind,source={path},target=/restore/{name},readonly" for name, path in roots.items()]
        if full:
            receipt["phase"] = "rebuild-retrieval"
            for role, field in (("embedding", "EMBEDDING_IMAGE"), ("qdrant", "QDRANT_IMAGE"), ("neo4j", "NEO4J_IMAGE")):
                identifier = sandbox.create(role, images[field], environments[role])
                sandbox.start(identifier)
            loader = sandbox.create("api", images["PUBLIC_API_IMAGE"], environments["api"], mounts=mounts, entrypoint="python",
                                    arguments=["-c", REBUILD])
            # Read-only readiness probes precede a single load. A failed load is
            # not retried against partially written indexes, and this API slot
            # has exited before the actual API starts.
            sandbox.start(loader, attached=True)
            status = json.loads(execute(["docker", "inspect", loader]))[0]["State"]
            if status.get("Running") or status.get("ExitCode") != 0:
                raise MaintenanceError("Isolated retrieval rebuild did not succeed")
        receipt["phase"] = "api-health"
        api = sandbox.create("api", images["PUBLIC_API_IMAGE"], environments["api"], mounts=mounts, entrypoint="python",
                             arguments=["-m", "uvicorn", "backend.snow_app.public_main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"])
        sandbox.start(api)
        receipt["api"] = health(sandbox, api, full=full)
        receipt["status"] = "passed"
        receipt["phase"] = "complete"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error_type"] = type(error).__name__
        receipt["error_reason"] = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
        receipt["failed_command"] = receipt.get("last_command")
    finally:
        failures = sandbox.close()
        receipt["cleanup_failures"] = failures
        receipt.pop("last_command", None)
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 2)
        receipt["not_exercised"] = ["production traffic promotion", "mail or provider calls", "whole-host recovery", "historical row-count equality"]
        if failures:
            receipt["status"] = "failed"
            receipt["retained_private_directory"] = str(work)
        else:
            if work.parent != parent or work.is_symlink() or not work.resolve().is_relative_to(parent.resolve()):
                raise MaintenanceError("Refusing cleanup outside the owned drill directory")
            shutil.rmtree(work)
        if receipt["status"] == "passed" and diagnostics.exists():
            if diagnostics.is_symlink() or diagnostics.parent != parent:
                raise MaintenanceError("Refusing cleanup outside the owned diagnostic directory")
            shutil.rmtree(diagnostics)
            receipt.pop("diagnostics", None)
        destination = parent / (work.name + ".json")
        atomic_write(destination, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
    return {"status": receipt["status"], "receipt": str(destination), "phase": receipt["phase"], "elapsed_seconds": receipt["elapsed_seconds"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="828b0a9f")
    parser.add_argument("--metadata-snapshot", default="fd81b13a")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("The isolated restore drill requires root-owned temporary resources")
    def interrupted(_signal, _frame):
        raise MaintenanceError("Restore drill was interrupted; cleanup was requested")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        paths = Paths()
        with release_lock(paths.lock.with_name("project-snow-restore-drill.lock")):
            result = drill(paths, snapshot=args.snapshot, metadata_snapshot=args.metadata_snapshot, full=args.full)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
