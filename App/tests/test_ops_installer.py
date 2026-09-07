from __future__ import annotations

from pathlib import Path
import os
import shutil

import pytest

from .test_release_state import portable_metadata_checks
import install_maintenance


def test_installer_generations_keep_verifier_independent_of_candidate_checkout(tmp_path, monkeypatch):
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
