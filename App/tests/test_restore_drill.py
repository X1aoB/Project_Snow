from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

from .test_release_state import portable_metadata_checks
import maintenance
import restore_drill


def hashed(payload):
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def material(tmp_path, monkeypatch):
    root = tmp_path / "restored"
    baseline = root / restore_drill.BASE
    baseline.mkdir(parents=True)
    image = "ghcr.io/example/snow@sha256:" + "a" * 64
    manifest = {"app_version": "0.9.6", "commit_sha": "b" * 40, "migration_heads": ["20260819_0004"],
                "application": {"image": image.split("@")[0], "digest": image.split("@")[1]},
                "embedding": {"image": image.split("@")[0], "digest": image.split("@")[1]}, "release_artifacts": {}}
    for kind, field, parent in (("data", "data_version", "data/releases"), ("avatar", "media_version", "media/releases"),
                               ("sticker", "sticker_version", "media/stickers/releases")):
        version = "synthetic-" + kind
        manifest[field] = version
        directory = root / "srv/project-snow" / parent / version
        directory.mkdir(parents=True)
        payload = b"synthetic public artifact"
        (directory / "asset.json").write_bytes(payload)
        document = {"files": [{"path": "asset.json", "bytes": len(payload), "sha256": hashed(payload)}]} if kind == "data" else {}
        manifest_bytes = json.dumps(document).encode()
        (directory / "manifest.json").write_bytes(manifest_bytes)
        binding = {"version": version, "manifest_sha256": hashed(manifest_bytes)}
        if kind != "data":
            checksums = (hashed(payload) + "  asset.json\n" + hashed(manifest_bytes) + "  manifest.json\n").encode()
            (directory / "SHA256SUMS").write_bytes(checksums)
            binding["checksums_sha256"] = hashed(checksums)
        manifest["release_artifacts"][kind] = binding
    archive = baseline / "host-configuration.tar"
    environment = "".join(name + "=" + image + "\n" for name in
                          ("PUBLIC_API_IMAGE", "EMBEDDING_IMAGE", "POSTGRES_IMAGE", "QDRANT_IMAGE", "NEO4J_IMAGE"))
    with tarfile.open(archive, "w") as package:
        for name, payload in ((restore_drill.ANCHOR, json.dumps(manifest).encode()),
                              ("srv/project-snow/runtime/compose.env", environment.encode()),
                              ("etc/project-snow/public.env", b"DO_NOT_LOAD_THIS_PRODUCTION_SECRET=secret-trap")):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            package.addfile(member, io.BytesIO(payload))
    (baseline / "postgres.dump").write_bytes(b"PGDMP-synthetic")
    code = baseline / restore_drill.CODE_ZIP
    with zipfile.ZipFile(code, "w") as package:
        package.writestr("App/README.md", "baseline code")
    monkeypatch.setattr(restore_drill, "CODE_SHA256", restore_drill.file_hash(code))
    # The actual baseline hash record predates the code zip. Its independently
    # reviewed hash must not be manufactured from the restored file itself.
    hashes = {name: restore_drill.file_hash(baseline / name) for name in ("postgres.dump", "host-configuration.tar")}
    (baseline / "sha256.json").write_text(json.dumps(hashes))
    return root, baseline, manifest


def test_baseline_material_verifies_code_dump_assets_without_extracting_secrets(material):
    root, baseline, expected = material
    manifest, images, roots, dump, receipt = restore_drill.inspect_material(root)
    assert manifest == expected and dump == baseline / "postgres.dump"
    assert set(receipt["packages"]) == {"data", "avatar", "sticker"}
    assert receipt["packages"]["data"]["files"] == 1
    assert not (root / "etc").exists()
    assert "secret-trap" not in repr((manifest, images, roots, receipt))


def test_tampered_asset_or_code_archive_fails_before_container_creation(material):
    root, baseline, manifest = material
    asset = root / "srv/project-snow/data/releases" / manifest["data_version"] / "asset.json"
    asset.write_bytes(b"changed")
    with pytest.raises(maintenance.MaintenanceError, match="checksum or size"):
        restore_drill.inspect_material(root)
    (baseline / restore_drill.CODE_ZIP).write_bytes(b"untrusted replacement")
    with pytest.raises(maintenance.MaintenanceError, match="recorded hash"):
        restore_drill.inspect_material(root)


def test_archive_path_traversal_cannot_substitute_trusted_metadata(tmp_path):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as package:
        member = tarfile.TarInfo("../../srv/project-snow/runtime/compose.env")
        member.size = 4
        package.addfile(member, io.BytesIO(b"evil"))
    with pytest.raises(maintenance.MaintenanceError, match="absent"):
        restore_drill.tar_metadata(archive, "srv/project-snow/runtime/compose.env")


def test_generated_credentials_are_new_and_provider_mail_paths_are_disabled(tmp_path, material):
    environment = restore_drill.generated_environments(tmp_path, material[2])
    api = dict(line.split("=", 1) for line in environment["api"].read_text().splitlines())
    assert "@postgres:5432/project_snow" in api["PUBLIC_DATABASE_URL"]
    assert api["PUBLIC_ENABLED_PROVIDERS"] == "" and api["PUBLIC_FEEDBACK_SMTP_HOST"] == ""
    assert all(api[field] == "" for field in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
    assert "secret-trap" not in environment["api"].read_text()
    postgres = dict(line.split("=", 1) for line in environment["postgres"].read_text().splitlines())
    assert postgres["PGPASSWORD"] == postgres["POSTGRES_PASSWORD"]
    assert postgres["PGPASSWORD"] in api["PUBLIC_DATABASE_URL"]


def test_sandbox_caps_resources_uses_internal_network_without_published_ports_and_only_deletes_owned_ids(tmp_path):
    calls = []
    sandbox = None
    counter = 0
    def execute(args, **kwargs):
        nonlocal counter
        calls.append(args)
        if args[1:3] == ["network", "create"]:
            return "f" * 64
        if args[1:3] == ["network", "inspect"]:
            return json.dumps([{"Internal": True, "Name": sandbox.identity}])
        if args[1:3] == ["image", "inspect"]:
            return "[]"
        if args[1:3] == ["volume", "create"]:
            return args[-1]
        if args[1] == "create":
            counter += 1
            return str(counter) * 64
        return ""
    sandbox = restore_drill.Sandbox(tmp_path, execute)
    environment = tmp_path / "generated.env"
    environment.write_text("SYNTHETIC=1\n")
    sandbox.open()
    image = "example/test@sha256:" + "a" * 64
    for role in restore_drill.LIMITS:
        sandbox.create(role, image, environment)
    assert sum(memory for memory, cpu in restore_drill.LIMITS.values()) <= 4096
    assert sum(cpu for memory, cpu in restore_drill.LIMITS.values()) <= 3
    for call in (call for call in calls if call[1] == "create"):
        assert call[call.index("--network") + 1] == "f" * 64
        assert "--memory" in call and "--memory-swap" in call and "--cpus" in call
        assert "--pull=never" in call and "--env-file" in call
        assert "--publish" not in call and "-p" not in call
    assert "--internal" in calls[0]
    assert sandbox.close() == []
    removed = [call[-1] for call in calls if call[1] == "rm"]
    assert set(removed) == set(sandbox.containers)
    assert not any("prune" in call or "compose" in call or "project-snow-public" in call for call in calls)


def test_sandbox_rejects_mounting_production_or_foreign_paths(tmp_path):
    sandbox = restore_drill.Sandbox(tmp_path, lambda *args, **kwargs: "[]")
    sandbox.network = "f" * 64
    environment = tmp_path / "generated.env"
    environment.write_text("SYNTHETIC=1\n")
    with pytest.raises(maintenance.MaintenanceError, match="private work directory"):
        sandbox.create("api", "example/test@sha256:" + "a" * 64, environment,
                       mounts=["type=bind,source=/etc/project-snow,target=/restore/data,readonly"])


def test_database_restore_targets_only_created_id_and_reports_real_counts_not_historical_equality(tmp_path):
    identifier = "a" * 64
    dump = tmp_path / "postgres.dump"
    dump.write_bytes(b"PGDMP-synthetic")
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        assert args[0:2] == ["docker", "exec"] and identifier in args
        if "--list" in args:
            return "199; 1259 16390 TABLE public public_feedback project_snow\n200; 1259 16391 TABLE public alembic_version project_snow"
        if "psql" in args:
            return json.dumps({"counts": {"public_feedback": 7, "alembic_version": 1}, "unvalidated_constraints": 0,
                               "migration_heads": ["20260819_0004"], "schema": [{"table_name": "public_feedback", "column_name": "id"}]})
        return ""
    sandbox = restore_drill.Sandbox(tmp_path, execute)
    sandbox.containers.append(identifier)
    sandbox.roles[identifier] = "postgres"
    receipt = restore_drill.restore_database(sandbox, identifier, dump)
    assert receipt["counts"]["public_feedback"] == 7
    assert receipt["historical_count_comparison"].startswith("unavailable")
    restores = [args for args in calls if "pg_restore" in args and "--list" not in args]
    assert len(restores) == 1 and "--single-transaction" in restores[0] and "--no-privileges" in restores[0]
    # Docker's temporary initialization server accepts Unix sockets before the
    # final database exists. Every real connection waits for final TCP startup.
    for args in calls:
        if any(child in args for child in ("pg_isready", "psql")) or args in restores:
            assert args[args.index("-h") + 1] == "127.0.0.1"


def test_database_restore_rejects_a_production_or_unregistered_container_before_any_command(tmp_path):
    sandbox = restore_drill.Sandbox(tmp_path, lambda *args, **kwargs: pytest.fail("No command may target an unregistered database"))
    with pytest.raises(maintenance.MaintenanceError, match="created by this isolated drill"):
        restore_drill.restore_database(sandbox, "project-snow-public-postgres-1", tmp_path / "postgres.dump")


@pytest.mark.parametrize("resource", ["container", "network", "volume"])
def test_create_timeout_recovers_only_owned_random_names_and_labels(tmp_path, resource):
    calls = []
    identifier = "c" * 64
    sandbox = None
    name = ""
    def execute(args, **kwargs):
        nonlocal name
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return "[]"
        if args[1:3] == ["network", "create"] or args[1] == "create" or args[1:3] == ["volume", "create"]:
            kind = "container" if args[1] == "create" else args[1]
            name = args[args.index("--name") + 1] if kind == "container" else args[-1]
            if kind == resource:
                raise subprocess.TimeoutExpired(args, 1)
            return name
        if args[1] == "ps" or args[1:3] == ["network", "ls"]:
            return identifier
        if args[1:3] == ["volume", "ls"]:
            return name
        if args[1:3] == ["container", "inspect"]:
            return json.dumps({"id": identifier, "name": "/" + name, "labels": {restore_drill.LABEL: sandbox.identity}})
        if args[1:3] in (["network", "inspect"], ["volume", "inspect"]):
            return json.dumps([{"Name": name, "Labels": {restore_drill.LABEL: sandbox.identity}}])
        return ""
    sandbox = restore_drill.Sandbox(tmp_path, execute)
    environment = tmp_path / "generated.env"
    environment.write_text("SYNTHETIC=1\n")
    with pytest.raises(subprocess.TimeoutExpired):
        if resource == "network":
            sandbox.open()
        else:
            sandbox.network = "f" * 64
            sandbox.create("api" if resource == "container" else "postgres", "example/test@sha256:" + "a" * 64, environment)
    assert sandbox.close() == []
    if resource == "container":
        assert ["docker", "rm", "--force", "--volumes", identifier] in calls
    else:
        assert ["docker", resource, "rm", identifier if resource == "network" else name] in calls
    assert not any("prune" in call for call in calls)


def test_timeout_discovery_refuses_foreign_ownership_without_deleting_it(tmp_path):
    calls = []
    identifier = "b" * 64
    def execute(args, **kwargs):
        calls.append(args)
        if args[1] == "ps":
            return identifier
        if args[1:3] == ["container", "inspect"]:
            return json.dumps({"id": identifier, "name": "/planned", "labels": {restore_drill.LABEL: "another-run"}})
        pytest.fail("Foreign ownership must never be removed")
    sandbox = restore_drill.Sandbox(tmp_path, execute)
    sandbox.container_names.add("planned")
    assert sandbox.close() == ["discovery:" + sandbox.identity]
    assert not any("rm" in call for call in calls)


def test_health_with_null_internal_network_ports_uses_only_owned_container_loopback(tmp_path):
    identifier = "d" * 64
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            # Actual Docker internal network state observed during the first
            # drill; health must not depend on a host port being assigned.
            return json.dumps([{"NetworkSettings": {"Ports": {"8000/tcp": None}}}])
        assert args[:4] == ["docker", "exec", identifier, "python"]
        assert kwargs["timeout"] == 20 and args[-1] in {"ready", "full"}
        return json.dumps({"status": "ok", "database": "ok", "data": "ok", "media": "ok", "stickers": "ok",
                           "dependencies": {"embedding": "ok", "qdrant": "ok", "neo4j": "ok"}})
    sandbox = restore_drill.Sandbox(tmp_path, execute)
    sandbox.containers.append(identifier)
    sandbox.roles[identifier] = "api"
    assert restore_drill.health(sandbox, identifier, full=True)["retrieval"] == "ok"
    assert [args[-1] for args in calls] == ["ready", "full"]
    assert "127.0.0.1:8000/public/v1/health/" in restore_drill.HEALTH_PROBE
    assert "ProxyHandler({})" in restore_drill.HEALTH_PROBE


def test_health_rejects_production_container_before_exec(tmp_path):
    sandbox = restore_drill.Sandbox(tmp_path, lambda *args, **kwargs: pytest.fail("No production exec is allowed"))
    with pytest.raises(maintenance.MaintenanceError, match="created by this isolated drill"):
        restore_drill.health(sandbox, "project-snow-public-api-blue-1", full=False)


def test_command_failure_identity_excludes_arguments_and_preserves_bounded_private_stderr(tmp_path, monkeypatch):
    args = ["docker", "exec", "-i", "a" * 64, "pg_restore", "--clean", "-U", "sensitive-user-fixture"]
    raw_stderr = b"sensitive-stderr-fixture" + b"x" * (2 * 1024 * 1024)
    monkeypatch.setattr(restore_drill.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 1, stdout=b"", stderr=raw_stderr))
    receipt = {"phase": "restore-database"}
    private = tmp_path / "diagnostics"
    execute = restore_drill.diagnostic_executor(restore_drill.command, receipt, private)
    for _ in range(3):
        with pytest.raises(restore_drill.DrillCommandError) as caught:
            execute(args)
        assert str(caught.value) == "Isolated drill command failed (docker/exec/pg_restore/restore, exit 1)"
    assert receipt["last_command"] == "docker/exec/pg_restore/restore"
    assert len(receipt["diagnostics"]) == 1
    artifact = Path(receipt["diagnostics"][0])
    assert artifact.parent == private and len(artifact.read_bytes()) == 1024 * 1024
    assert artifact.read_bytes().startswith(b"sensitive-stderr-fixture")
    assert "sensitive" not in json.dumps(receipt)


def test_command_timeout_is_redacted_and_keeps_stderr_for_private_diagnostics(monkeypatch):
    args = ["docker", "exec", "container", "python", "-c", "sensitive-script-fixture"]
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(args, 2, stderr=b"sensitive-timeout-fixture")
    monkeypatch.setattr(restore_drill.subprocess, "run", timeout)
    with pytest.raises(restore_drill.DrillCommandError) as caught:
        restore_drill.command(args)
    assert str(caught.value) == "Isolated drill command failed (docker/exec/python, timeout)"
    assert caught.value.stderr == b"sensitive-timeout-fixture"
