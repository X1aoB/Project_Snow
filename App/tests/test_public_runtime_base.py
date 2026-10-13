"""Synthetic faults for the explicit immutable runtime dependency reuse gate."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_runtime_base", APP / "scripts/verify_public_runtime_base.py")
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)


@pytest.fixture
def locks(tmp_path):
    # Read the actual approved Git object, independent of checkout line endings.
    raw = subprocess.check_output(["git", "show", base.APPROVED_REVISION + ":App/requirements-public.txt"], cwd=APP)
    assert hashlib.sha256(raw).hexdigest() == base.APPROVED_LOCK_SHA256
    installed, candidate = tmp_path / "installed.lock", tmp_path / "candidate.lock"
    installed.write_bytes(raw)
    candidate.write_bytes(raw)
    return installed, candidate, raw


def checked(locks, **options):
    installed, candidate, _raw = locks
    calls = []
    def package_check(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    arguments = dict(revision=base.APPROVED_REVISION, uid_lookup=lambda: 10001,
                     executable_lookup=lambda _name: str(Path(sys.executable).absolute()), package_check=package_check)
    arguments.update(options)
    base.verify(installed, candidate, **arguments)
    return calls


def test_approved_base_checks_package_consistency_once_and_preserves_files(locks):
    installed, candidate, raw = locks
    calls = checked(locks)
    assert calls == [([sys.executable, "-I", "-B", "-m", "pip", "check"],
                     {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                      "stderr": subprocess.DEVNULL, "check": False, "timeout": 60})]
    assert installed.read_bytes() == candidate.read_bytes() == raw
    assert not any("install" in command for command, _kwargs in calls)


@pytest.mark.parametrize("revision", ["", "a" * 40, base.APPROVED_REVISION + "-modified", None])
def test_source_identity_failure_happens_before_file_or_package_access(tmp_path, monkeypatch, revision):
    monkeypatch.setenv("APP_REVISION", "a" * 40)
    with pytest.raises(base.RuntimeBaseError, match="source differs"):
        base.verify(tmp_path / "missing", tmp_path / "missing", revision=revision,
                    package_check=lambda *_args, **_kwargs: pytest.fail("identity failed first"))


@pytest.mark.parametrize("changed", ["installed", "candidate", "both"])
def test_lock_drift_never_installs_or_falls_back(locks, changed):
    installed, candidate, _raw = locks
    for path in ((installed,) if changed == "installed" else (candidate,) if changed == "candidate" else (installed, candidate)):
        path.write_bytes(b"synthetic==0\n")
    with pytest.raises(base.RuntimeBaseError, match="fresh dependency build"):
        checked(locks, package_check=lambda *_args, **_kwargs: pytest.fail("lock failed before packages"))


@pytest.mark.parametrize("uid", [0, 1000, True, "10001"])
def test_wrong_user_refuses_before_package_probe(locks, uid):
    with pytest.raises(base.RuntimeBaseError, match="unprivileged user"):
        checked(locks, uid_lookup=lambda: uid,
                package_check=lambda *_args, **_kwargs: pytest.fail("UID failed before packages"))


@pytest.mark.parametrize("gosu", [None, "", "relative/gosu"])
def test_missing_privilege_drop_refuses_before_package_probe(locks, gosu):
    with pytest.raises(base.RuntimeBaseError, match="privilege-drop executable"):
        checked(locks, executable_lookup=lambda _name: gosu,
                package_check=lambda *_args, **_kwargs: pytest.fail("gosu failed before packages"))


@pytest.mark.parametrize("code", [1, -1, False, "0"])
def test_inconsistent_package_result_refuses_reuse(locks, code):
    with pytest.raises(base.RuntimeBaseError, match="inconsistent installed packages"):
        checked(locks, package_check=lambda *_args, **_kwargs: SimpleNamespace(returncode=code))


def test_package_probe_timeout_does_not_fallback(locks):
    calls = []
    def timed_out(command, **_kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 60)
    with pytest.raises(subprocess.TimeoutExpired):
        checked(locks, package_check=timed_out)
    assert calls == [[sys.executable, "-I", "-B", "-m", "pip", "check"]]


def test_isolated_package_check_explicitly_disables_bytecode_writes():
    # -I ignores PYTHONDONTWRITEBYTECODE, so the immutable dependency check
    # must use -B itself rather than relying on the inherited image setting.
    result = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys; assert sys.dont_write_bytecode"],
                            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0


@pytest.mark.parametrize("bad", ["directory", "empty", "oversize", "hardlink"])
def test_unbounded_or_non_regular_lock_refuses(locks, tmp_path, bad):
    installed, _candidate, raw = locks
    installed.unlink()
    if bad == "directory":
        installed.mkdir()
    elif bad == "empty":
        installed.touch()
    elif bad == "oversize":
        installed.write_bytes(b"x" * (base.MAX_LOCK_BYTES + 1))
    else:
        target = tmp_path / "target.lock"
        target.write_bytes(raw)
        os.link(target, installed)
    with pytest.raises(base.RuntimeBaseError, match="bounded regular file"):
        checked(locks)


def test_symlink_lock_refuses_when_platform_allows_symlinks(locks):
    installed, candidate, _raw = locks
    installed.unlink()
    try:
        installed.symlink_to(candidate)
    except OSError:
        pytest.skip("Local platform cannot create a symlink fixture")
    with pytest.raises(base.RuntimeBaseError, match="bounded regular file"):
        checked(locks)


def test_cli_failure_contains_no_lock_bytes_or_path(locks, monkeypatch, capsys):
    installed, candidate, _raw = locks
    monkeypatch.setenv("APP_REVISION", "synthetic-private-wrong-revision")
    monkeypatch.setattr(sys, "argv", ["verify", "--installed-lock", str(installed), "--candidate-lock", str(candidate)])
    with pytest.raises(SystemExit) as failure:
        base.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Approved public runtime dependency base verification failed.\n"
