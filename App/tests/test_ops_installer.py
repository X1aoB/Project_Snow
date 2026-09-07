from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ops"))
import install_maintenance


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source = tmp_path / "App" / "ops"
    source.mkdir(parents=True)
    scripts = source.parent / "scripts"
    scripts.mkdir()
    actual = Path(__file__).parents[1] / "ops"
    for name in ("maintenance.py", "release_state.py", "recovery_backup.py", "auto_stage.py", "routing.py", "monitor.py"):
        shutil.copyfile(actual / name, source / name)
    for kind in ("cleanup", "backup", "auto-stage", "monitor"):
        for suffix in ("service", "timer"):
            shutil.copyfile(actual / f"project-snow-{kind}.{suffix}", source / f"project-snow-{kind}.{suffix}")
    (scripts / "verify_release_proof.py").write_bytes(b"VERIFIER_ID = 'reviewed'\n")
    destination = tmp_path / "installed"
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr(install_maintenance, "controlled", lambda *args, **kwargs: None)
    monkeypatch.setattr(install_maintenance, "sync", lambda *args: None)
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(source, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("Symlink creation requires Windows developer mode; Linux CI exercises this contract")
    return source, destination, units


def test_installer_generations_keep_verifier_independent_of_candidate_checkout(installation):
    source, destination, units = installation
    scripts = source.parent / "scripts"
    first = install_maintenance.install(source, destination, units)
    first_generation = (destination / "current").resolve()
    assert (destination / "verify_release_proof.py").read_bytes() == b"VERIFIER_ID = 'reviewed'\n"
    # A candidate changes only the checkout. The installed trust root is stable
    # until an explicit root maintenance installation selects a generation.
    (scripts / "verify_release_proof.py").write_bytes(b"VERIFIER_ID = 'new-reviewed-generation'\n")
    assert (destination / "verify_release_proof.py").read_bytes() == b"VERIFIER_ID = 'reviewed'\n"
    second = install_maintenance.install(source, destination, units)
    assert first["installed_version"] != second["installed_version"]
    assert second["previous_helper"] == "versions/" + first["installed_version"]
    assert (first_generation / "verify_release_proof.py").read_bytes() == b"VERIFIER_ID = 'reviewed'\n"
    assert all((destination / name).resolve().parent == (destination / "current").resolve() for name in
               ("maintenance.py", "release_state.py", "auto_stage.py", "verify_release_proof.py"))
    assert first["runner_changed"] is False and first["timers_enabled"] is False
    receipt = Path(second["recovery_receipt"])
    assert json.loads(receipt.read_text())["status"] == "committed"
    assert receipt.stat().st_mode & 0o077 == 0
    assert receipt.parent.stat().st_mode & 0o077 == 0


def visible_state(destination, units):
    state = {}
    for root in (destination, units):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.name in {"versions", "install-receipts"}:
                continue
            state[str(path)] = ("link", os.readlink(path)) if path.is_symlink() else (
                "file", path.read_bytes(), path.stat().st_mode & 0o777
            )
    return state


def seed_previous(source, destination, units, previous):
    if previous == "generation":
        install_maintenance.install(source, destination, units)
        with (source / "monitor.py").open("a", encoding="utf-8") as target:
            target.write("\nREVIEWED_NEW_GENERATION = True\n")
    elif previous == "legacy":
        destination.mkdir()
        legacy = destination / "maintenance.py"
        legacy.write_bytes(b"LEGACY_HELPER = True\n")
        legacy.chmod(0o755)
        old_unit = units / "project-snow-cleanup.service"
        old_unit.write_bytes(b"[Service]\nExecStart=/old/reviewed/helper\n")
        old_unit.chmod(0o640)


@pytest.mark.parametrize("previous", ["first", "legacy", "generation"])
@pytest.mark.parametrize("fault", ["module", "unit", "pointer", "reload", "receipt"])
def test_publish_failure_restores_first_legacy_and_generation_installs(installation, monkeypatch, previous, fault):
    source, destination, units = installation
    seed_previous(source, destination, units, previous)
    before = visible_state(destination, units)
    real_bytes, real_link = install_maintenance.atomic_bytes, install_maintenance.atomic_link
    failed, reloads = False, []

    def fail_once(condition):
        nonlocal failed
        if condition and not failed:
            failed = True
            raise OSError("injected publication failure")

    def write_bytes(path, data, mode):
        fail_once((fault == "unit" and path == units / "project-snow-monitor.timer") or
                  (fault == "receipt" and path.name == "receipt.json" and b'"status": "committed"' in data))
        real_bytes(path, data, mode)

    def write_link(path, target):
        fail_once((fault == "module" and path.name == "monitor.py") or
                  (fault == "pointer" and path.name == "current"))
        real_link(path, target)

    def reload_units():
        reloads.append(True)
        fail_once(fault == "reload")

    monkeypatch.setattr(install_maintenance, "atomic_bytes", write_bytes)
    monkeypatch.setattr(install_maintenance, "atomic_link", write_link)
    with pytest.raises(RuntimeError, match="prior files and units were restored"):
        install_maintenance.install(source, destination, units, reload_units=reload_units)
    assert failed
    assert visible_state(destination, units) == before
    records = [json.loads(path.read_text()) for path in (destination / "install-receipts").glob("*/receipt.json")]
    record = next(record for record in records if record["status"] == "rolled_back")
    assert record["schema_version"] == "project-snow-helper-install-1"
    assert reloads  # restored units are loaded even when publication failed before reload
    if previous == "legacy":
        entry = next(item for item in record["targets"] if item["path"] == str(destination / "maintenance.py"))
        receipt_root = next(path.parent for path in (destination / "install-receipts").glob("*/receipt.json")
                            if json.loads(path.read_text())["status"] == "rolled_back")
        assert (receipt_root / entry["backup"]).read_bytes() == b"LEGACY_HELPER = True\n"


def test_late_unsafe_unit_is_rejected_before_any_publication(installation, monkeypatch):
    source, destination, units = installation
    seed_previous(source, destination, units, "generation")
    before = visible_state(destination, units)
    def reject_last_unit(path, **options):
        if path == units / "project-snow-monitor.timer":
            raise SystemExit("unsafe unit target")
    monkeypatch.setattr(install_maintenance, "controlled", reject_last_unit)
    monkeypatch.setattr(install_maintenance, "atomic_link", lambda *args: pytest.fail("No pointer may change before preflight completes"))
    with pytest.raises(SystemExit, match="unsafe unit target"):
        install_maintenance.install(source, destination, units)
    assert visible_state(destination, units) == before


def test_rollback_failure_is_reported_and_recovery_material_is_retained(installation, monkeypatch):
    source, destination, units = installation
    seed_previous(source, destination, units, "legacy")
    real_restore = install_maintenance.restore_target
    def restore(snapshot):
        if snapshot["path"] == str(units / "project-snow-cleanup.service"):
            raise OSError("injected rollback disk failure")
        real_restore(snapshot)
    monkeypatch.setattr(install_maintenance, "restore_target", restore)
    def reject_reload():
        raise OSError("injected reload failure")
    with pytest.raises(RuntimeError, match="rollback is incomplete.*injected rollback disk failure"):
        install_maintenance.install(source, destination, units, reload_units=reject_reload)
    record = json.loads(next((destination / "install-receipts").glob("*/receipt.json")).read_text())
    assert record["status"] == "rollback_failed"
    assert len(record["rollback_errors"]) == 2
    assert (destination / "maintenance.py").read_bytes() == b"LEGACY_HELPER = True\n"


def test_invalid_source_fails_before_installing_any_generation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "installed"
    def refuse(*args, **kwargs):
        raise SystemExit("untrusted source")
    monkeypatch.setattr(install_maintenance, "controlled", refuse)
    with pytest.raises(SystemExit, match="untrusted source"):
        install_maintenance.install(source, destination, tmp_path / "units")
    assert not destination.exists()
