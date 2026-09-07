from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.report_trivy_findings import collect_findings
from scripts.scan_running_images import (
    GIB,
    Commands,
    ScanError,
    archive_config_ids,
    canonical_reference,
    controlled_directory,
    scan,
)

APP_ID = "sha256:" + "1" * 64
EDGE_ID = "sha256:" + "2" * 64
SCANNER_ID = "sha256:" + "3" * 64
APP_REF = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
EMBED_REF = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
EDGE_REF = "caddy:2-alpine@sha256:" + "c" * 64
TRIVY = "ghcr.io/aquasecurity/trivy@sha256:" + "d" * 64
OLD_COMMIT = "e" * 40
SECRET = "do-not-output-this-environment-value"
CONFIG = json.dumps({"os": "linux", "architecture": "amd64", "config": {"Env": [SECRET]}}).encode()
CONFIG_ID = "sha256:" + hashlib.sha256(CONFIG).hexdigest()


@pytest.fixture(autouse=True)
def synthetic_disk_space(monkeypatch):
    # Fake Docker writes only tiny fixtures. Contract outcomes must not depend
    # on the CI runner's actual remaining disk space.
    monkeypatch.setattr(
        "scripts.scan_running_images.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100 * GIB, used=40 * GIB, free=60 * GIB),
    )


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def binding_files(root):
    write(root / "releases" / "active-colour", "blue\n")
    write(root / "releases" / "current", f"blue {OLD_COMMIT} {APP_REF} {EMBED_REF}\n")
    write(
        root / "releases" / "current-manifest.json",
        {
            "schema_version": "project-snow-release-1",
            "commit_sha": OLD_COMMIT,
            "application": {"image": APP_REF.split("@")[0], "digest": APP_REF.split("@")[1]},
            "embedding": {"image": EMBED_REF.split("@")[0], "digest": EMBED_REF.split("@")[1]},
        },
    )
    write(
        root / "runtime" / "compose.env",
        f"PUBLIC_API_IMAGE={APP_REF}\nEMBEDDING_IMAGE={EMBED_REF}\nCADDY_IMAGE={EDGE_REF}\nOTHER={SECRET}\n",
    )


def docker_archive(path):
    with tarfile.open(path, "w") as archive:
        members = {
            "config.json": CONFIG,
            "manifest.json": json.dumps(
                [
                    {"Config": "config.json", "RepoTags": None, "Layers": []},
                ]
            ).encode(),
        }
        for name, payload in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def vulnerability(severity="HIGH", fixed="2.0"):
    return {
        "VulnerabilityID": "CVE-2026-1000",
        "PkgName": "example",
        "InstalledVersion": "1.0",
        "FixedVersion": fixed,
        "Severity": severity,
    }


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = [
            {
                "id": "a" * 64,
                "image_id": APP_ID,
                "running": True,
                "project": "project-snow-public",
                "service": "public-api-blue",
                "Env": [SECRET],
            },
            {
                "id": "b" * 64,
                "image_id": EDGE_ID,
                "running": True,
                "project": "project-snow-public",
                "service": "caddy",
            },
            {
                "id": "c" * 64,
                "image_id": "sha256:" + "f" * 64,
                "running": True,
                "project": "dify",
                "service": "api",
            },
        ]
        self.image_refs = {APP_ID: [APP_REF], EDGE_ID: [canonical_reference(EDGE_REF)], SCANNER_ID: [TRIVY]}
        self.image_sizes = {}
        self.findings = []
        self.report_id = CONFIG_ID
        self.failure = None
        self.timeout_stage = None
        self.owned = {}
        self.scans = 0
        self.changed = False
        self.db_expired = False
        self.timeout_before_cid = False
        self.owned_names = {}
        self.scratch_paths = []

    def __call__(self, command, **kwargs):
        assert command[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        command = ["docker", *command[3:]]
        self.calls.append((command, kwargs))
        assert kwargs["timeout"] > 0
        assert kwargs["stderr"] is subprocess.DEVNULL
        result = ""
        if command[:3] == ["docker", "ps", "--quiet"]:
            assert "label=com.docker.compose.project=project-snow-public" in command
            result = "\n".join(row["id"] for row in self.containers)
        elif command[:3] == ["docker", "image", "inspect"]:
            target = command[-1]
            identifier = SCANNER_ID if target == TRIVY else target
            result = json.dumps(
                {
                    "id": identifier,
                    "repo_digests": self.image_refs[identifier],
                    "size": self.image_sizes.get(identifier, 100_000),
                    "os": "linux",
                    "architecture": "amd64",
                    "Env": [SECRET],
                }
            )
        elif command[:3] == ["docker", "container", "inspect"]:
            if "io.project-snow.running-scan" in command[4]:
                identifier = self.owned_names.get(command[-1], command[-1])
                owner = self.owned.get(identifier)
                result = json.dumps({"id": identifier, "owner": owner}) if owner else ""
            else:
                rows = [dict(row) for row in self.containers]
                if self.changed and self.scans:
                    rows[0]["id"] = "9" * 64
                result = "\n".join(json.dumps(row) for row in rows)
        elif command[:3] == ["docker", "image", "save"]:
            assert command[-1] in set(self.image_refs) - {SCANNER_ID}
            docker_archive(Path(command[command.index("--output") + 1]))
        elif command[:2] == ["docker", "run"]:
            assert "--pull=never" in command
            assert TRIVY in command
            assert command[command.index("--memory") + 1] == "2g"
            assert command[command.index("--cpus") + 1] == "1"
            assert command[command.index("--tmpfs") + 1] == "/tmp:rw,nosuid,noexec,size=512m"
            assert command[command.index("--env") + 1] == "TMPDIR=/cache/tmp"
            assert not any("docker.sock" in argument or ":main" in argument for argument in command)
            mounts = {}
            for index, argument in enumerate(command):
                if argument == "--mount":
                    parts = dict(item.split("=", 1) for item in command[index + 1].split(",") if "=" in item)
                    mounts[parts["target"]] = Path(parts["source"])
            scratch = mounts["/cache"] / "tmp"
            assert scratch.is_dir() and not scratch.is_symlink()
            assert scratch.parent.parent.name.startswith("snow-running-scan-")
            assert scratch.parent.parent == mounts["/reports"].parent
            self.scratch_paths.append(scratch)
            cid = Path(command[command.index("--cidfile") + 1])
            if not self.timeout_before_cid:
                cid.write_text("8" * 64)
            token = command[command.index("--label") + 1].split("=", 1)[1]
            stage = "scan" if "--input" in command else "download"
            if self.timeout_stage == stage:
                self.owned["8" * 64] = token
                self.owned_names[command[command.index("--name") + 1]] = "8" * 64
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if self.failure == stage:
                return subprocess.CompletedProcess(command, 1, "", SECRET)
            if "--download-db-only" in command:
                next_update = datetime.now(UTC) + timedelta(hours=-1 if self.db_expired else 6)
                write(
                    mounts["/cache"] / "db" / "metadata.json",
                    {
                        "Version": 2,
                        "NextUpdate": next_update.isoformat(),
                        "UpdatedAt": datetime.now(UTC).isoformat(),
                    },
                )
            elif "--input" in command:
                self.scans += 1
                assert command[command.index("--network") + 1] == "none"
                assert "--offline-scan" in command
                assert "--skip-db-update" in command and "--ignore-unfixed" in command
                name = Path(command[command.index("--output") + 1]).name
                write(
                    mounts["/reports"] / name,
                    {
                        "SchemaVersion": 2,
                        "ArtifactType": "container_image",
                        "Metadata": {"ImageID": self.report_id, "ImageConfig": {"Env": [SECRET]}},
                        "Results": [{"Target": "python", "Vulnerabilities": self.findings}],
                    },
                )
        elif command[:3] == ["docker", "container", "rm"]:
            assert command[-1] == "8" * 64
            del self.owned[command[-1]]
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, result, "")


def run_scan(tmp_path, fake, **kwargs):
    root = tmp_path / "host"
    binding_files(root)
    return scan(
        root,
        TRIVY,
        Commands(1800, execute=fake),
        work_root=tmp_path,
        read=lambda path: path.read_bytes(),
        **kwargs,
    )


def test_scans_actual_old_deployed_ids_and_excludes_dify_and_all_env(tmp_path):
    fake = FakeDocker()
    result = run_scan(tmp_path, fake)
    assert result["status"] == "passed"
    assert result["release"]["commit_sha"] == OLD_COMMIT
    assert {image["image_id"] for image in result["images"]} == {APP_ID, EDGE_ID}
    assert all(image["platform_config_ids"] == [CONFIG_ID] for image in result["images"])
    assert APP_ID != CONFIG_ID  # containerd index IDs need not be platform config IDs.
    assert SECRET not in json.dumps(result) and "Env" not in json.dumps(result)
    saves = [command[-1] for command, _ in fake.calls if command[:3] == ["docker", "image", "save"]]
    assert set(saves) == {APP_ID, EDGE_ID}
    assert not any("main" in " ".join(command) for command, _ in fake.calls)


def test_running_digest_manifest_mismatch_refuses_before_download_or_scan(tmp_path):
    fake = FakeDocker()
    fake.image_refs[APP_ID] = [APP_REF.replace("a" * 64, "f" * 64)]
    with pytest.raises(ScanError, match="does not match"):
        run_scan(tmp_path, fake)
    assert not any(command[:2] == ["docker", "run"] for command, _ in fake.calls)
    assert not any(command[:3] == ["docker", "image", "save"] for command, _ in fake.calls)


def test_database_and_offline_analysis_use_private_disk_tmpdir_without_expanding_ram(tmp_path):
    fake = FakeDocker()
    assert run_scan(tmp_path, fake)["status"] == "passed"
    assert len(fake.scratch_paths) == 4  # two downloads, then two offline image scans
    assert len(set(fake.scratch_paths)) == 1
    assert fake.scratch_paths[0].is_relative_to(tmp_path)
    assert not fake.scratch_paths[0].exists()  # private mutable tool data is removed after this run


def test_database_download_requires_four_gib_beyond_host_reserve(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(
        "scripts.scan_running_images.shutil.disk_usage", lambda _path: SimpleNamespace(free=14 * GIB - 1),
    )
    with pytest.raises(ScanError, match="scratch.*10 GiB"):
        run_scan(tmp_path, fake)
    assert not any(command[:2] == ["docker", "run"] for command, _ in fake.calls)


def test_separate_scratch_disk_cannot_hide_low_host_reserve(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(
        "scripts.scan_running_images.shutil.disk_usage",
        lambda path: SimpleNamespace(free=10 * GIB - 1 if path == tmp_path / "host" else 60 * GIB),
    )
    with pytest.raises(ScanError, match="host disk reserve"):
        run_scan(tmp_path, fake)
    assert not any(command[:2] == ["docker", "run"] for command, _ in fake.calls)


@pytest.mark.parametrize("image_size,free_after_download", [(100_000, 11 * GIB), (3 * GIB, 16 * GIB)])
def test_each_export_reserves_image_scratch_in_addition_to_ten_gib(
    tmp_path, monkeypatch, image_size, free_after_download,
):
    fake = FakeDocker()
    fake.image_sizes[APP_ID] = image_size

    def space(_path):
        downloaded = any("--download-java-db-only" in command for command, _ in fake.calls)
        return SimpleNamespace(free=free_after_download if downloaded else 20 * GIB)

    monkeypatch.setattr("scripts.scan_running_images.shutil.disk_usage", space)
    with pytest.raises(ScanError, match="scratch.*10 GiB"):
        run_scan(tmp_path, fake)
    assert any("--download-java-db-only" in command for command, _ in fake.calls)
    assert not any(command[:3] == ["docker", "image", "save"] for command, _ in fake.calls)


def test_download_that_consumes_host_reserve_cannot_report_success(tmp_path, monkeypatch):
    fake = FakeDocker()

    def space(_path):
        downloaded = any("--download-java-db-only" in command for command, _ in fake.calls)
        return SimpleNamespace(free=9 * GIB if downloaded else 20 * GIB)

    monkeypatch.setattr("scripts.scan_running_images.shutil.disk_usage", space)
    with pytest.raises(ScanError, match="host disk reserve"):
        run_scan(tmp_path, fake)
    assert not any(command[:3] == ["docker", "image", "save"] for command, _ in fake.calls)


def test_cannot_use_an_unpinned_scanner_or_pull_a_tool(tmp_path):
    fake = FakeDocker()
    with pytest.raises(ScanError, match="full sha256"):
        scan(tmp_path, "ghcr.io/aquasecurity/trivy:latest", Commands(1800, execute=fake))
    assert fake.calls == []


def test_explicit_unbound_admin_is_scanned_but_remains_an_identity_failure(tmp_path):
    fake = FakeDocker()
    admin_id = "sha256:" + "6" * 64
    admin_ref = "ghcr.io/x1aob/project_snow-public@sha256:" + "7" * 64
    fake.image_refs[admin_id] = [admin_ref]
    fake.containers.append(
        {
            "id": "6" * 64,
            "image_id": admin_id,
            "running": True,
            "project": "project-snow-public",
            "service": "admin",
        }
    )
    with pytest.raises(ScanError, match="does not match"):
        run_scan(tmp_path, fake)
    result = run_scan(tmp_path, fake, retained_admin_image=admin_ref)
    assert result["status"] == "identity_drift"
    assert result["identity_status"] == "retained_unbound"
    admin = next(image for image in result["images"] if "admin" in image["services"])
    assert admin["image_id"] == admin_id
    assert admin["bindings"][0]["identity_status"] == "retained_unbound"
    assert admin["bindings"][0]["expected_reference"] == APP_REF
    assert "manifest_sha256" not in admin["bindings"][0]
    assert admin["bindings"][0]["operator_acknowledged_scan_only"] is True


@pytest.mark.parametrize("stage", ["download", "scan"])
def test_scan_failure_never_returns_success_or_prints_diagnostics(tmp_path, stage):
    fake = FakeDocker()
    fake.failure = stage
    with pytest.raises(ScanError, match="failed") as caught:
        run_scan(tmp_path, fake)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("stage", ["download", "scan"])
def test_timeout_cleans_up_only_the_owned_scanner_container(tmp_path, stage):
    fake = FakeDocker()
    fake.timeout_stage = stage
    with pytest.raises(ScanError, match="deadline"):
        run_scan(tmp_path, fake)
    removed = [command[-1] for command, _ in fake.calls if command[:3] == ["docker", "container", "rm"]]
    assert removed == ["8" * 64]
    assert not fake.owned
    assert all(row["id"] != "8" * 64 for row in fake.containers)


def test_timeout_before_cidfile_resolves_only_the_owned_random_name(tmp_path):
    fake = FakeDocker()
    fake.timeout_stage = "download"
    fake.timeout_before_cid = True
    with pytest.raises(ScanError, match="deadline"):
        run_scan(tmp_path, fake)
    assert not fake.owned
    cleanup_targets = [
        command[-1]
        for command, _ in fake.calls
        if command[:3] == ["docker", "container", "inspect"] and "io.project-snow.running-scan" in command[4]
    ]
    assert len(cleanup_targets) == 1 and cleanup_targets[0].startswith("snow-running-scan-")


@pytest.mark.parametrize(
    "mode,uid", [(stat.S_IFDIR | 0o777, 0), (stat.S_IFDIR | 0o700, 123), (stat.S_IFLNK | 0o777, 0)]
)
def test_custom_work_and_output_directories_reject_uncontrolled_or_linked_parents(
    tmp_path, monkeypatch, mode, uid
):
    def metadata(path):
        return SimpleNamespace(
            st_mode=mode if path == tmp_path else stat.S_IFDIR | 0o755,
            st_uid=uid if path == tmp_path else 0,
            st_gid=0,
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ScanError, match="root-controlled"):
        controlled_directory(tmp_path)


def test_high_critical_fixable_findings_follow_existing_ci_gate(tmp_path):
    fake = FakeDocker()
    fake.findings = [
        vulnerability(),
        vulnerability("CRITICAL"),
        vulnerability("LOW"),
        vulnerability("HIGH", ""),
    ]
    result = run_scan(tmp_path, fake)
    assert result["status"] == "vulnerable"
    assert result["finding_count"] == 4  # two actionable findings per distinct running image
    assert len(collect_findings(result)) == result["finding_count"]
    assert {row["severity"] for image in result["images"] for row in image["findings"]} == {
        "HIGH",
        "CRITICAL",
    }


def test_wrong_trivy_image_identity_cannot_pass(tmp_path):
    fake = FakeDocker()
    fake.report_id = "sha256:" + "f" * 64
    with pytest.raises(ScanError, match="not bound"):
        run_scan(tmp_path, fake)


def test_expired_database_cannot_pass(tmp_path):
    fake = FakeDocker()
    fake.db_expired = True
    with pytest.raises(ScanError, match="database"):
        run_scan(tmp_path, fake)


def test_deployment_change_during_scan_invalidates_result(tmp_path):
    fake = FakeDocker()
    fake.changed = True
    with pytest.raises(ScanError, match="identity|changed"):
        run_scan(tmp_path, fake)


def test_total_deadline_is_shared_across_discovery_download_and_scans(tmp_path):
    fake = FakeDocker()
    elapsed = [0.0]

    def execute(command, **kwargs):
        elapsed[0] += 10
        return fake(command, **kwargs)

    root = tmp_path / "host"
    binding_files(root)
    with pytest.raises(ScanError, match="total.*deadline"):
        scan(
            root,
            TRIVY,
            Commands(50, execute=execute, clock=lambda: elapsed[0]),
            work_root=tmp_path,
            read=lambda path: path.read_bytes(),
        )


def test_oci_archive_resolves_platform_config_without_assuming_index_equals_config(tmp_path):
    config_digest = hashlib.sha256(CONFIG).hexdigest()
    manifest = json.dumps({"config": {"digest": "sha256:" + config_digest}}).encode()
    manifest_digest = hashlib.sha256(manifest).hexdigest()
    index = json.dumps(
        {
            "manifests": [
                {"digest": "sha256:" + manifest_digest, "platform": {"os": "linux", "architecture": "amd64"}}
            ]
        }
    ).encode()
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        for name, payload in {
            "index.json": index,
            "blobs/sha256/" + config_digest: CONFIG,
            "blobs/sha256/" + manifest_digest: manifest,
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    assert archive_config_ids(path, {"image_id": APP_ID, "os": "linux", "architecture": "amd64"}) == {
        CONFIG_ID
    }


def test_docker_hub_tag_pins_and_repo_digests_have_same_identity():
    assert canonical_reference(EDGE_REF) == "docker.io/library/caddy@sha256:" + "c" * 64
