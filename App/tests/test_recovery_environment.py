"""A candidate cannot leave the active recovery environment half rewritten."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import maintenance  # noqa: E402
import release_state  # noqa: E402


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    paths = maintenance.Paths(root=tmp_path)
    path = tmp_path / "runtime/colours/blue.compose.env"
    path.parent.mkdir(parents=True)
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases/current-manifest.json").write_text(json.dumps({"data_version": "2026.08.19.1"}))
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    monkeypatch.setattr(release_state, "active_release", lambda _: {"colour": "blue", "image": image})
    monkeypatch.setattr(maintenance, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "_controlled_directory", lambda *a: None)
    monkeypatch.setattr(release_state, "sync", lambda *a: None)
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a: None, raising=False)
    path.write_bytes(f"# retained\r\nPUBLIC_API_IMAGE={image}\r\nUNCHANGED=literal value\r\n".encode())
    data_root = tmp_path / "data/releases/2026.08.19.1"
    data_root.mkdir(parents=True)
    (data_root / "manifest.json").write_text(json.dumps({"data_version": "2026.08.19.1"}))
    return paths, path, data_root


def test_correct_recovery_preserves_exact_bytes_and_inode(recovery):
    paths, path, data_root = recovery
    original = (
        f"PUBLIC_DATA_ROOT={data_root}\r\nPUBLIC_MAILER_ENV_FILE=/etc/project-snow/feedback-mailer.env\r\n".encode()
        + path.read_bytes()
    )
    path.write_bytes(original)
    identity = path.stat().st_ino
    with patch.object(release_state, "atomic_write") as writer:
        assert release_state.pin_recovery_environment(paths, "blue", data_root)["status"] == "unchanged"
        writer.assert_not_called()
    assert path.read_bytes() == original and path.stat().st_ino == identity


def test_missing_pins_published_together_without_touching_unrelated_lines(recovery):
    paths, path, data_root = recovery
    original = path.read_bytes()
    assert release_state.pin_recovery_environment(paths, "blue", data_root)["status"] == "updated"
    assert path.read_bytes().startswith(original)
    values = maintenance.read_environment(path)
    assert values["PUBLIC_DATA_ROOT"] == str(data_root)
    assert values["PUBLIC_MAILER_ENV_FILE"] == "/etc/project-snow/feedback-mailer.env"


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_publication_keeps_original_complete_file(recovery, failure):
    paths, path, data_root = recovery
    original = path.read_bytes()
    with patch.object(release_state.os, failure, side_effect=OSError("injected publication failure")):
        with pytest.raises(OSError, match="injected"):
            release_state.pin_recovery_environment(paths, "blue", data_root)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_directory_sync_failure_leaves_complete_new_file_and_retry_syncs(recovery):
    paths, path, data_root = recovery
    with patch.object(release_state, "sync", side_effect=OSError("directory sync failed")):
        with pytest.raises(OSError, match="directory sync"):
            release_state.pin_recovery_environment(paths, "blue", data_root)
    values = maintenance.read_environment(path)
    assert values["PUBLIC_DATA_ROOT"] == str(data_root)
    assert values["PUBLIC_MAILER_ENV_FILE"] == "/etc/project-snow/feedback-mailer.env"
    complete = path.read_bytes()
    with patch.object(release_state, "sync") as sync:
        assert release_state.pin_recovery_environment(paths, "blue", data_root)["status"] == "unchanged"
        sync.assert_called_once_with(path.parent)
    assert path.read_bytes() == complete


@pytest.mark.parametrize(
    "case", ["inactive", "wrong-data", "duplicate", "wrong-image", "missing-data", "wrong-version"]
)
def test_untrusted_or_mismatched_recovery_is_not_rewritten(recovery, case):
    paths, path, data_root = recovery
    colour = "green" if case == "inactive" else "blue"
    if case == "wrong-data":
        data_root = paths.root / "data/releases/other"
    if case == "duplicate":
        path.write_bytes(path.read_bytes() + b"UNCHANGED=duplicate\n")
    if case == "wrong-image":
        path.write_bytes(path.read_bytes().replace(b"sha256:", b"sha256:other"))
    if case == "missing-data":
        (data_root / "manifest.json").unlink()
        data_root.rmdir()
    if case == "wrong-version":
        (data_root / "manifest.json").write_text(json.dumps({"data_version": "other"}))
    original = path.read_bytes()
    with pytest.raises(maintenance.MaintenanceError):
        release_state.pin_recovery_environment(paths, colour, data_root)
    assert path.read_bytes() == original
