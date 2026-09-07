from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from .test_release_state import state, portable_metadata_checks, write
import maintenance
import release_state
import recovery_backup
import auto_stage


def test_candidate_reservation_prevents_overwrite_and_requires_exact_completion(state):
    manifest = state.root / "releases" / "colours" / "green-manifest.json"
    release_state.record_candidate(state, "green", manifest)
    document = json.loads(manifest.read_text())
    document["commit_sha"] = "e" * 40
    write(manifest, document)
    with pytest.raises(maintenance.MaintenanceError, match="different candidate"):
        release_state.record_candidate(state, "green", manifest)
    with pytest.raises(maintenance.MaintenanceError, match="exactly promoted"):
        release_state.discard_candidate(state, "d" * 40, promoted=True)
    with pytest.raises(maintenance.MaintenanceError, match="not the pending"):
        release_state.discard_candidate(state, "e" * 40)
    assert release_state.discard_candidate(state, "d" * 40)["status"] == "discarded"


def test_same_sha_cannot_replace_an_already_reviewable_manifest(state):
    manifest = state.root / "releases" / "colours" / "green-manifest.json"
    release_state.record_candidate(state, "green", manifest)
    document = json.loads(manifest.read_text())
    document["generated_at"] = "a different CI attempt"
    write(manifest, document)
    with pytest.raises(maintenance.MaintenanceError, match="different manifest bytes"):
        release_state.record_candidate(state, "green", manifest)


def test_unknown_baseline_anchor_disables_gc_but_is_preserved_for_backup(state):
    write(state.root / "releases" / "anchors" / "baseline-0.9.6" / "snapshot.json", {"protected": True})
    with pytest.raises(maintenance.MaintenanceError, match="Unknown recovery anchor"):
        release_state.protected_image_references(state)
    assert release_state.protected_image_references(state, strict_unknown=False)


@pytest.fixture
def backup_environment(state, monkeypatch):
    monkeypatch.setattr(recovery_backup, "postgres_container", lambda *args: "a" * 64)
    monkeypatch.setattr(recovery_backup, "require_private_staging", lambda *args: None)
    monkeypatch.setattr(recovery_backup.shutil, "disk_usage", lambda *args: SimpleNamespace(free=20 * maintenance.GIB))
    (state.root / "repo").mkdir()
    state.secrets.parent.mkdir(exist_ok=True)
    return state


def test_pinned_backup_contains_full_recovery_sources_and_checks_before_retention(backup_environment):
    state = backup_environment
    calls, consumed, captured = [], [], {}
    def execute(command):
        calls.append(command)
        if command[0] == "docker":
            return "100"
        if command[1] == "backup":
            stage = next(Path(value) for value in command if "recovery-" in value)
            captured.update(json.loads((stage / "recovery.json").read_text()))
            return json.dumps({"message_type": "summary", "snapshot_id": "f" * 64})
        return ""
    result = recovery_backup.backup(state, execute, lambda command, path: path.write_bytes(b"PGDMP-example"),
                                    lambda command, path: consumed.append(command), pin=True)
    assert result["retention"] == "pinned until explicitly retired"
    backup_call = next(command for command in calls if command[0:2] == ["restic", "backup"])
    assert "project-snow-pinned-" + "c" * 40 in backup_call
    assert str(state.root / "releases") in backup_call
    assert str(state.root / "runtime") in backup_call
    assert str(state.root / "repo") in backup_call
    assert str(state.secrets.parent) in backup_call
    assert captured["postgres_dump_sha256"] == result["dump_sha256"]
    assert "does not contain image tar" in captured["image_recovery"]
    assert calls[-2] == ["restic", "check"]
    assert calls[-1][0:4] == ["restic", "forget", "--tag", "project-snow-production"]
    assert all("--list" in command for command in consumed)
    assert not list((state.root / "backups" / "staging").iterdir())


def test_failed_integrity_check_never_retires_previous_snapshots(backup_environment):
    calls = []
    def execute(command):
        calls.append(command)
        if command[0] == "docker":
            return "100"
        if command[1] == "backup":
            return json.dumps({"message_type": "summary", "snapshot_id": "e" * 64})
        raise maintenance.MaintenanceError("injected repository check failure")
    with pytest.raises(maintenance.MaintenanceError, match="repository check"):
        recovery_backup.backup(backup_environment, execute, lambda command, path: path.write_bytes(b"PGDMP"), lambda *args: None)
    assert not any(command[0:2] == ["restic", "forget"] for command in calls)


def test_restore_refuses_wrong_checksum_and_running_writers_before_mutation(state, monkeypatch):
    dump = state.root / "restore.dump"
    dump.write_bytes(b"PGDMP")
    monkeypatch.setattr(recovery_backup, "require_regular", lambda *args, **kwargs: None)
    consumers = []
    with pytest.raises(maintenance.MaintenanceError, match="checksum"):
        recovery_backup.restore_postgres(state, dump, "f" * 64, lambda command: pytest.fail(str(command)),
                                         lambda *args: consumers.append(args))
    def execute(command):
        if command[1] == "ps":
            return "a" * 64
        return json.dumps([{"Config": {"Labels": {"com.docker.compose.service": "public-api-blue"}}}])
    with pytest.raises(maintenance.MaintenanceError, match="Stop both API"):
        recovery_backup.restore_postgres(state, dump, recovery_backup.hash_file(dump), execute,
                                         lambda *args: consumers.append(args))
    assert not consumers


def test_restore_is_atomic_rechecks_writers_and_leaves_them_stopped(state, monkeypatch):
    dump = state.root / "restore.dump"
    dump.write_bytes(b"PGDMP")
    monkeypatch.setattr(recovery_backup, "require_regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery_backup, "postgres_container", lambda *args: "a" * 64)
    writer_checks, consumers = [], []
    monkeypatch.setattr(recovery_backup, "require_stopped_writers", lambda *args: writer_checks.append(True))
    monkeypatch.setattr(recovery_backup, "cleanup", lambda *args, **kwargs: {"checked": not kwargs["require_running_api"]})
    monkeypatch.setattr(recovery_backup, "recover_requests", lambda *args, **kwargs: {"recovered": 1})
    result = recovery_backup.restore_postgres(state, dump, recovery_backup.hash_file(dump), lambda command: "",
                                              lambda command, path: consumers.append(command))
    assert len(writer_checks) == 2
    assert "--list" in consumers[0]
    assert "--single-transaction" in consumers[1] and "--exit-on-error" in consumers[1]
    assert result["retention"] == {"checked": True}
    assert result["writers"].startswith("remain stopped")


@pytest.fixture
def auto_environment(state, monkeypatch, tmp_path):
    monkeypatch.setattr(auto_stage, "release_lock", lambda *args: nullcontext())
    real_temporary = auto_stage.tempfile.TemporaryDirectory
    monkeypatch.setattr(auto_stage.tempfile, "TemporaryDirectory", lambda **kwargs: real_temporary(dir=tmp_path))
    return state


def candidate_fetch(url, *, optional=False):
    if url == auto_stage.MAIN_URL:
        return json.dumps({"ref": "refs/heads/main", "object": {"sha": "d" * 40}}).encode()
    return json.dumps({"commit_sha": "d" * 40}).encode()


def test_auto_stage_verifies_installed_verifier_before_inbox_and_never_promotes(auto_environment):
    calls = []
    def execute(command):
        calls.append(("execute", command))
        return ""
    def publish(paths, name, payload):
        calls.append(("publish", name))
    result = auto_stage.auto_stage(auto_environment, fetch=candidate_fetch, execute=execute, publish=publish)
    assert result["status"] == "staged"
    assert calls[0][1][1] == auto_stage.VERIFIER
    assert [kind for kind, command in calls] == ["execute", "publish", "publish", "execute"]
    assert calls[-1][1] == [auto_stage.RUNNER, "stage", "green", "d" * 40]
    assert all("promote" not in command for kind, command in calls)


def test_auto_stage_waits_for_manual_review_and_does_not_overwrite(auto_environment):
    state = auto_environment
    release_state.record_candidate(state, "green", state.root / "releases" / "colours" / "green-manifest.json")
    def fetch(url, **kwargs):
        assert url == auto_stage.MAIN_URL
        return json.dumps({"ref": "refs/heads/main", "object": {"sha": "e" * 40}}).encode()
    result = auto_stage.auto_stage(state, fetch=fetch, execute=lambda command: pytest.fail(str(command)))
    assert result["status"] == "awaiting-review"
    assert result["commit_sha"] == "d" * 40


def test_auto_stage_missing_signed_metadata_is_quiet_and_does_not_invoke_runner(auto_environment):
    def fetch(url, **kwargs):
        return candidate_fetch(url) if url == auto_stage.MAIN_URL else None
    result = auto_stage.auto_stage(auto_environment, fetch=fetch, execute=lambda command: pytest.fail(str(command)))
    assert result["status"] == "awaiting-signed-candidate"


def test_auto_stage_failed_materials_need_explicit_retry(auto_environment):
    def execute(command):
        if command[0] == auto_stage.RUNNER:
            raise maintenance.MaintenanceError("missing exact data version")
        return ""
    with pytest.raises(maintenance.MaintenanceError, match="explicitly retry"):
        auto_stage.auto_stage(auto_environment, fetch=candidate_fetch, execute=execute, publish=lambda *args: None)
    result = auto_stage.auto_stage(auto_environment, fetch=candidate_fetch, execute=lambda command: pytest.fail(str(command)))
    assert result["status"] == "retry-required"
    assert (auto_environment.root / "releases" / "current").read_text().split()[1] == "c" * 40


@pytest.mark.parametrize("url", ["http://github.com/release", "https://evil.example/release", "https://github.com@evil.example/a",
                                 "https://github.com:444/a", "file:///etc/shadow"])
def test_candidate_download_rejects_untrusted_redirects(url):
    with pytest.raises(maintenance.MaintenanceError):
        auto_stage.validate_url(url)
