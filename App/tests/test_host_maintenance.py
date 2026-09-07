from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "ops" / "maintenance.py"
spec = importlib.util.spec_from_file_location("snow_host_maintenance", SOURCE)
maintenance = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = maintenance
spec.loader.exec_module(maintenance)


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
