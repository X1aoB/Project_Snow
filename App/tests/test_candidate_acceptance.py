from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import candidate_acceptance as acceptance  # noqa: E402
import maintenance  # noqa: E402
import release_state  # noqa: E402

CURRENT = "a" * 40
CANDIDATE = "b" * 40
NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)
PUBLIC = "ghcr.io/x1aob/project_snow-public"
EMBEDDING = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "c" * 64


@pytest.fixture
def staged(tmp_path, monkeypatch):
    root = tmp_path / "snow"
    (root / "releases/colours").mkdir(parents=True)
    (root / "runtime/colours").mkdir(parents=True)

    def write(name, value):
        path = root / name
        path.write_text(value, encoding="utf-8", newline="")
        path.chmod(0o600)

    old_image = PUBLIC + "@sha256:" + "d" * 64
    new_image = PUBLIC + "@sha256:" + "e" * 64
    manifest = {
        "commit_sha": CANDIDATE,
        "app_version": "0.10.0-rc.1",
        "application": {"image": PUBLIC, "digest": "sha256:" + "e" * 64},
        "embedding": {"image": EMBEDDING.split("@")[0], "digest": EMBEDDING.split("@")[1]},
    }
    manifest_payload = acceptance.encoded(manifest).decode()
    write("releases/active-colour", "blue\n")
    write("releases/current", f"blue {CURRENT} {old_image} {EMBEDDING}\n")
    write(
        "releases/current-manifest.json",
        json.dumps(
            {"commit_sha": CURRENT, "application": {"image": PUBLIC, "digest": old_image.split("@")[1]}}
        ),
    )
    write("releases/current-config.json", '{"configuration_sha256":{"sample":"old"}}')
    write("runtime/compose.env", f"PUBLIC_API_IMAGE={old_image}\n")
    write("releases/colours/green", f"green {CANDIDATE} {new_image} {EMBEDDING}\n")
    write("releases/colours/green-manifest.json", manifest_payload)
    write("releases/colours/green-config.json", '{"configuration_sha256":{"sample":"new"}}')
    write("runtime/colours/green.compose.env", f"PUBLIC_API_IMAGE={new_image}\n")
    write(
        "releases/colours/green-stage-receipt",
        f"project-snow-stage-receipt-1 {'f' * 64} green {CANDIDATE} {new_image} {EMBEDDING}\n",
    )
    write(
        "releases/pending-candidate.json",
        json.dumps(
            {
                "schema_version": "project-snow-pending-candidate-1",
                "colour": "green",
                "commit_sha": CANDIDATE,
                "base_commit_sha": CURRENT,
                "manifest_sha256": acceptance.sha256(manifest_payload.encode()),
                "recorded_at": NOW.isoformat(),
            }
        ),
    )

    # Ownership and ancestor policies run separately below. These portable
    # behavior cases exercise actual receipt bytes and distinct Docker states.
    monkeypatch.setattr(maintenance, "require_regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        acceptance,
        "directory",
        lambda path, create=False: path.mkdir(parents=True, exist_ok=True) if create else None,
    )
    if os.name == "nt":
        monkeypatch.setattr(acceptance, "sync", lambda path: None)
        monkeypatch.setattr(release_state, "sync", lambda path: None)
        monkeypatch.setattr(release_state.os, "fchmod", lambda *args: None, raising=False)

    runtime = {
        "blue": {"id": "1" * 64, "image_id": "sha256:" + "2" * 64, "image": old_image, "running": True},
        "green": {"id": "3" * 64, "image_id": "sha256:" + "4" * 64, "image": new_image, "running": True},
    }
    expected_images = {old_image: runtime["blue"]["image_id"], new_image: runtime["green"]["image_id"]}
    build = {"revision": CANDIDATE, "app_version": "0.10.0-rc.1"}
    calls = []

    def execute(command):
        calls.append(command)
        if command[1] == "ps":
            colour = command[-1].rsplit("-", 1)[1]
            return runtime[colour]["id"]
        if command[1] == "inspect":
            return json.dumps(next(row for row in runtime.values() if row["id"] == command[-1]))
        if command[1:3] == ["image", "inspect"]:
            return expected_images[command[-1]]
        if command[1] == "exec":
            return json.dumps(build)
        raise AssertionError(command)

    evidence = {
        "schema_version": "project-snow-candidate-evidence-1",
        "commit_sha": CANDIDATE,
        "expected_current": CURRENT,
        "checks": dict.fromkeys(acceptance.CHECKS, True),
    }
    return maintenance.Paths(root=root), execute, runtime, build, evidence, calls, write


def ready(staged, **options):
    paths, execute, _, _, evidence, _, _ = staged
    return acceptance.prepare(
        paths, "green", CANDIDATE, CURRENT, evidence, now=NOW, execute=execute, **options
    )


def approved(staged):
    receipt = ready(staged)
    acceptance.approve(staged[0], receipt["receipt_id"], CURRENT, now=NOW, execute=staged[1])
    return receipt


def test_preparation_does_not_authorize_promotion_and_explicit_exact_approval_does(staged):
    receipt = ready(staged)
    assert receipt["status"] == "ready_for_review"
    with pytest.raises(FileNotFoundError):
        acceptance.verify(staged[0], "green", CANDIDATE, now=NOW, execute=staged[1])
    acceptance.approve(staged[0], receipt["receipt_id"], CURRENT, now=NOW, execute=staged[1])
    result = acceptance.verify(staged[0], "green", CANDIDATE, now=NOW, execute=staged[1])
    assert result["receipt_id"] == receipt["receipt_id"] and result["expected_current"] == CURRENT
    assert all(".Config.Env" not in str(command) for command in staged[5])


@pytest.mark.parametrize("check", acceptance.CHECKS)
def test_every_incomplete_acceptance_check_blocks_receipt(staged, check):
    staged[4]["checks"][check] = False
    with pytest.raises(maintenance.MaintenanceError, match="every required check"):
        ready(staged)
    assert not (staged[0].root / "releases/acceptance").exists()


@pytest.mark.parametrize("seconds", [59, 86401])
def test_unbounded_or_too_short_receipt_lifetimes_are_rejected(staged, seconds):
    with pytest.raises(maintenance.MaintenanceError, match="lifetime"):
        ready(staged, ttl_seconds=seconds)


def test_expired_or_wrong_base_approval_cannot_promote(staged):
    receipt = approved(staged)
    with pytest.raises(maintenance.MaintenanceError, match="expired"):
        acceptance.verify(staged[0], "green", CANDIDATE, now=NOW + timedelta(days=1), execute=staged[1])
    with pytest.raises(maintenance.MaintenanceError, match="different current"):
        acceptance.approve(staged[0], receipt["receipt_id"], "0" * 40, now=NOW, execute=staged[1])


@pytest.mark.parametrize(
    "changed", ["current", "candidate-config", "stage-nonce", "pending-reservation", "current-config"]
)
def test_receipt_is_invalidated_by_exact_release_or_restaging_changes(staged, changed):
    approved(staged)
    paths, _, _, _, _, _, write = staged
    names = {
        "candidate-config": "releases/colours/green-config.json",
        "current-config": "releases/current-config.json",
        "stage-nonce": "releases/colours/green-stage-receipt",
        "pending-reservation": "releases/pending-candidate.json",
    }
    if changed == "current":
        marker = paths.root / "releases/current"
        write("releases/current", marker.read_text().replace(CURRENT, "0" * 40))
    elif changed == "stage-nonce":
        write(names[changed], (paths.root / names[changed]).read_text().replace("f" * 64, "0" * 64))
    else:
        write(names[changed], (paths.root / names[changed]).read_text() + " \n")
    with pytest.raises(maintenance.MaintenanceError):
        acceptance.verify(paths, "green", CANDIDATE, now=NOW, execute=staged[1])


@pytest.mark.parametrize(
    "fault", ["candidate-image", "candidate-restart", "current-restart", "revision", "version"]
)
def test_runtime_image_identity_and_build_are_rechecked_at_promotion(staged, fault):
    approved(staged)
    if fault == "candidate-image":
        staged[2]["green"]["image_id"] = "sha256:" + "9" * 64
    elif fault in {"candidate-restart", "current-restart"}:
        staged[2]["green" if fault.startswith("candidate") else "blue"]["id"] = "9" * 64
    else:
        staged[3]["revision" if fault == "revision" else "app_version"] = "wrong"
    with pytest.raises(maintenance.MaintenanceError):
        acceptance.verify(staged[0], "green", CANDIDATE, now=NOW, execute=staged[1])


def test_modified_immutable_receipt_and_path_traversal_are_rejected(staged):
    receipt = approved(staged)
    path = staged[0].root / "releases/acceptance" / (receipt["receipt_id"] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(maintenance.MaintenanceError, match="modified"):
        acceptance.verify(staged[0], "green", CANDIDATE, now=NOW, execute=staged[1])
    with pytest.raises(maintenance.MaintenanceError, match="exact acceptance"):
        acceptance.load_receipt(staged[0], "../outside")


def test_same_commit_manifest_tampering_is_not_accepted(staged):
    path = staged[0].root / "releases/colours/green-manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(maintenance.MaintenanceError, match="disagree"):
        ready(staged)


def test_newline_only_receipt_tampering_cannot_hide_behind_text_normalization(staged):
    receipt = approved(staged)
    path = staged[0].root / "releases/acceptance" / (receipt["receipt_id"] + ".json")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(maintenance.MaintenanceError, match="modified"):
        acceptance.verify(staged[0], "green", CANDIDATE, now=NOW, execute=staged[1])


def test_expiry_during_runtime_probes_blocks_promotion(staged, monkeypatch):
    approved(staged)
    ticks = iter([0.0, 31.0])
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: next(ticks))
    with pytest.raises(maintenance.MaintenanceError, match="expired while checking"):
        acceptance.verify(
            staged[0], "green", CANDIDATE, now=NOW + timedelta(days=1, seconds=-30), execute=staged[1]
        )


def test_forward_promotion_checks_receipt_before_any_compose_start():
    script = (OPS / "promote.sh").read_text()
    gate = script.index('candidate_acceptance.py" verify --lock-held')
    assert script.index('if [ "$rollback_mode" = 0 ]; then', script.index("read -r marker_colour")) < gate
    assert gate < script.index("compose up -d")


def test_runtime_probe_timeout_fails_closed_without_exposing_docker_diagnostics(monkeypatch):
    def fail(*args, **kwargs):
        assert 0 < kwargs["timeout"] <= 20
        raise subprocess.TimeoutExpired("docker private-diagnostic", kwargs["timeout"])

    monkeypatch.setattr(acceptance.subprocess, "run", fail)
    with pytest.raises(maintenance.MaintenanceError, match="exceeded its deadline") as error:
        acceptance.DeadlineCommands()(["docker", "ps"])
    assert "private-diagnostic" not in str(error.value)


def test_total_runtime_deadline_prevents_another_docker_call(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: clock[0])
    execute = acceptance.DeadlineCommands()
    clock[0] += 91
    with pytest.raises(maintenance.MaintenanceError, match="total runtime"):
        execute(["docker", "ps"])


@pytest.mark.skipif(os.name != "posix", reason="Linux root ownership and symlink policy")
def test_acceptance_directory_rejects_symlink_and_writable_parent(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("root metadata policy requires an isolated root test")
    # /tmp itself is writable, so even a private child must fail. Production
    # receipts use /srv/project-snow, never system temporary storage.
    with pytest.raises(maintenance.MaintenanceError, match="root-controlled"):
        acceptance.directory(tmp_path)
