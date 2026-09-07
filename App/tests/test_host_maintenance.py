from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import maintenance


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "snow"
    (root / "releases").mkdir(parents=True)
    (root / "runtime").mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    commit = "c" * 40
    (root / "releases" / "active-colour").write_text("blue\n")
    (root / "releases" / "current").write_text(f"blue {commit} {image} {embedding}\n")
    (root / "releases" / "current-manifest.json").write_text(json.dumps({"commit_sha": commit, "application": {"image": image.split("@")[0], "digest": image.split("@")[1]}}))
    (root / "runtime" / "compose.env").write_text(f"PUBLIC_API_IMAGE={image}\n")
    (secrets / "public_database_url").write_text("never-read-host-secret")
    return maintenance.Paths(root=root, secrets=secrets, docker_storage=tmp_path / "docker"), image


def fake_docker(calls, *, wrong_image=False, internal=True):
    def execute(command):
        calls.append(command)
        if command[1:3] == ["network", "inspect"]:
            return json.dumps([{"Id": "d" * 64, "Name": "project-snow-public_data", "Driver": "bridge", "Internal": internal,
                                "Labels": {"com.docker.compose.project": "project-snow-public", "com.docker.compose.network": "data"}}])
        if command[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": "sha256:" + "e" * 64}])
        if command[1] == "ps":
            return "f" * 64
        if command[1] == "inspect":
            return json.dumps([{"Image": "sha256:" + ("a" if wrong_image else "e") * 64}])
        if command[1] == "run":
            return '{"deleted":{"public_feedback":2}}'
        raise AssertionError(command)
    return execute


def test_cleanup_uses_active_digest_existing_network_and_only_database_secret(release):
    paths, image = release
    calls = []
    with patch.object(maintenance, "require_regular"):
        result = maintenance.cleanup(paths, fake_docker(calls))
    command = calls[-1]
    assert result["deleted"] == {"public_feedback": 2}
    assert command[1] == "run" and "--pull=never" in command
    assert command[command.index("--network") + 1] == "d" * 64
    assert image in command and "--entrypoint" in command
    assert command.count("--mount") == 1
    assert "public_database_url" in command[command.index("--mount") + 1]
    assert all("compose" not in command and "create" not in command for command in calls)
    assert "never-read-host-secret" not in repr(calls)
    assert "public_admin_token" not in repr(calls) and "management" not in repr(calls)


@pytest.mark.parametrize("fault", ["image", "network", "manifest"])
def test_cleanup_fails_before_container_start_on_runtime_drift(release, fault):
    paths, _ = release
    calls = []
    if fault == "manifest":
        (paths.root / "runtime" / "compose.env").write_text("PUBLIC_API_IMAGE=drifted\n")
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError):
        maintenance.cleanup(paths, fake_docker(calls, wrong_image=fault == "image", internal=fault != "network"))
    assert not any(command[1] == "run" for command in calls)


@pytest.mark.parametrize("growth,free,passes", [(0, 10, True), (0, 9, False), (6, 11, False), (6, 12, True)])
def test_capacity_reserves_larger_of_ten_gib_or_twice_growth(tmp_path, growth, free, passes):
    paths = maintenance.Paths(root=tmp_path, docker_storage=tmp_path)
    usage = lambda _: shutil._ntuple_diskusage(50 * maintenance.GIB, 0, free * maintenance.GIB)
    if passes:
        assert maintenance.capacity(paths, growth * maintenance.GIB, usage)["status"] == "ok"
    else:
        with pytest.raises(maintenance.MaintenanceError):
            maintenance.capacity(paths, growth * maintenance.GIB, usage)


def test_capacity_checks_docker_mount_separately(tmp_path):
    paths = maintenance.Paths(root=tmp_path / "app", docker_storage=tmp_path / "docker")
    usage = lambda path: shutil._ntuple_diskusage(50, 0, (20 if path == paths.root else 2) * maintenance.GIB)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.capacity(paths, 0, usage)


def test_environment_rejects_duplicate_image_pin(tmp_path):
    environment = tmp_path / "compose.env"
    environment.write_text("PUBLIC_API_IMAGE=a\nPUBLIC_API_IMAGE=b\n")
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError):
        maintenance.read_environment(environment)


def test_lease_recovery_requires_stopped_source_and_only_uses_its_verified_digest(release):
    paths, image = release
    calls = []
    base = fake_docker(calls)
    def execute(command):
        if command[1] == "ps":
            calls.append(command)
            return ""
        if command[1] == "run":
            calls.append(command)
            return '{"recovered":2,"remaining_leases":0}'
        return base(command)
    with patch.object(maintenance, "require_regular"):
        result = maintenance.recover_requests(paths, execute)
    assert result["recovered"] == 2
    job = calls[-1]
    assert image in job and job.count("--mount") == 1
    assert "recover_expired_requests" in job[-1] and "time.monotonic() + 46" in job[-1]
    assert not any("compose" in command or "prune" in command for command in calls)
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError, match="Stop and drain"):
        maintenance.recover_requests(paths, fake_docker([]))


@pytest.mark.parametrize("changed", [None, "EMBEDDING_IMAGE", "POSTGRES_IMAGE", "infra/egress-squid.conf"])
def test_stage_rejects_shared_dependency_changes(release, changed):
    paths, image = release
    environment = {key: image for key in maintenance.SHARED_IMAGE_KEYS}
    (paths.root / "runtime" / "compose.env").write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    if changed in environment:
        environment[changed] = image.replace("a" * 64, "b" * 64)
    candidate = paths.root / "runtime" / "candidate.env"
    candidate.write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    hashes = {key: "a" * 64 for key in maintenance.SHARED_CONFIG_PATHS}
    (paths.root / "releases" / "current-config.json").write_text(json.dumps({"configuration_sha256": hashes}))
    if changed in hashes:
        hashes[changed] = "b" * 64
    manifest = paths.root / "candidate.json"
    manifest.write_text(json.dumps({"configuration_sha256": hashes}))
    with patch.object(maintenance, "require_regular"):
        if changed:
            with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
                maintenance.shared_dependency_gate(paths, candidate, manifest)
        else:
            assert maintenance.shared_dependency_gate(paths, candidate, manifest)["status"] == "ok"


def test_stage_never_reconciles_shared_services():
    source = (OPS / "deploy.sh").read_text()
    assert 'compose run --rm --no-deps "$service" alembic upgrade head' in source
    assert 'compose up -d --no-deps "$service"' in source
    assert 'compose up -d postgres qdrant neo4j embedding egress-proxy' not in source
    assert source.index("shared-dependency-gate") < source.index('compose pull "$service"')
