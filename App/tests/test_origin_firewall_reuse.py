from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import verify_origin_firewall as firewall  # noqa: E402


def loaded_units() -> dict[str, dict[str, str]]:
    common = {"LoadState": "loaded", "DropInPaths": "", "NeedDaemonReload": "no", "UnitFileState": "enabled"}
    return {
        firewall.SERVICE: {
            **common, "FragmentPath": str(firewall.SYSTEMD / firewall.SERVICE),
            **dict.fromkeys(firewall.EXEC_PROPERTIES, ""),
            "ExecStart": f"{{ path={firewall.BINARY} ; argv[]={firewall.BINARY} update ; ignore_errors=no ; "
                         "start_time=[Tue 2026-09-08 01:00:00 UTC] ; stop_time=[n/a] ; "
                         "pid=45 ; code=exited ; status=0 }",
            "Before": "shutdown.target docker.service", "Type": "oneshot",
            "ActiveState": "inactive", "SubState": "dead", "Result": "success", "ExecMainStatus": "0",
        },
        firewall.TIMER: {
            **common, "FragmentPath": str(firewall.SYSTEMD / firewall.TIMER), "ActiveState": "active",
            "SubState": "waiting", "Unit": firewall.SERVICE, "Result": "success",
        },
        "docker.service": {"LoadState": "loaded", "Requires": f"sysinit.target {firewall.SERVICE}"},
    }


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Real assets and systemd reply parsing; metadata/link details tested separately."""
    source = tmp_path / "sealed"
    assets = []
    for relative, installed, mode in firewall.ASSETS:
        source_path = source / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes((relative + "\n").encode())
        destination = tmp_path / "installed" / installed.name
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(source_path.read_bytes())
        assets.append((relative, destination, mode))
    monkeypatch.setattr(firewall, "ASSETS", tuple(assets))
    monkeypatch.setattr(firewall, "controlled", lambda *args, **kwargs: None)
    monkeypatch.setattr(firewall, "regular", lambda *args: None)
    monkeypatch.setattr(firewall, "check_link", lambda *args: None)
    # Windows fixtures cannot provide Unix root metadata or O_NOFOLLOW. The
    # production flags and metadata are separately asserted below.
    monkeypatch.setattr(firewall.os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), raising=False)
    monkeypatch.setattr(firewall.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), raising=False)
    units = loaded_units()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["systemctl", "show", "--no-pager"]
        assert kwargs["check"] and kwargs["timeout"] == 15
        properties = command[3].removeprefix("--property=").split(",")
        return subprocess.CompletedProcess(command, 0, "\n".join(
            key + "=" + units[command[-1]][key] for key in properties
            if key in units[command[-1]] and (key not in firewall.EXEC_PROPERTIES or units[command[-1]][key])
        ), "")

    monkeypatch.setattr(firewall.subprocess, "run", run)
    return source, assets, units, calls


def test_reuses_exact_assets_and_only_queries_loaded_systemd(installation):
    source, assets, _, calls = installation
    before = [path.read_bytes() for _, path, _ in assets]
    firewall.verify(source)
    assert [call[-1] for call in calls] == [firewall.SERVICE, firewall.TIMER, "docker.service"]
    assert before == [path.read_bytes() for _, path, _ in assets]


@pytest.mark.parametrize("asset_index", range(3))
@pytest.mark.parametrize("fault", ("installed-tamper", "source-drift", "missing"))
def test_real_asset_drift_or_absence_fails_before_systemd(installation, asset_index, fault):
    source, assets, _, calls = installation
    relative, path, _ = assets[asset_index]
    if fault == "missing":
        path.unlink()
    else:
        (path if fault == "installed-tamper" else source / relative).write_bytes(b"unexpected change")
    with pytest.raises((firewall.VerificationError, FileNotFoundError)):
        firewall.verify(source)
    assert not calls


@pytest.mark.parametrize("unit,key,value", (
    (firewall.SERVICE, "FragmentPath", "/run/systemd/system/project-snow-origin-firewall.service"),
    (firewall.SERVICE, "DropInPaths", f"/etc/systemd/system/{firewall.SERVICE}.d/override.conf"),
    (firewall.SERVICE, "NeedDaemonReload", "yes"),
    (firewall.SERVICE, "UnitFileState", "enabled-runtime"),
    (firewall.SERVICE, "LoadState", "masked"),
    (firewall.SERVICE, "ExecStart", "{ path=/bin/false ; argv[]=/bin/false ; ignore_errors=no ; pid=0 }"),
    (firewall.SERVICE, "ExecStartPre", "{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; pid=0 }"),
    (firewall.SERVICE, "Before", "shutdown.target"),
    (firewall.SERVICE, "Type", "simple"),
    (firewall.SERVICE, "ActiveState", "failed"),
    (firewall.SERVICE, "Result", "exit-code"),
    (firewall.SERVICE, "ExecMainStatus", "1"),
    (firewall.TIMER, "ActiveState", "inactive"),
    (firewall.TIMER, "SubState", "elapsed"),
    (firewall.TIMER, "Unit", "other.service"),
    (firewall.TIMER, "Result", "resources"),
    (firewall.TIMER, "UnitFileState", "disabled"),
    (firewall.TIMER, "DropInPaths", "/run/systemd/system/override.conf"),
    ("docker.service", "Requires", "sysinit.target"),
))
def test_disk_assets_are_correct_but_loaded_unit_drift_is_rejected(installation, unit, key, value):
    source, _, units, _ = installation
    units[unit][key] = value
    with pytest.raises(firewall.VerificationError):
        firewall.verify(source)


@pytest.mark.parametrize("change", ("append-command", "extra-argument", "ignore-error"))
def test_execstart_requires_one_exact_command(installation, change):
    source, _, units, _ = installation
    value = units[firewall.SERVICE]["ExecStart"]
    units[firewall.SERVICE]["ExecStart"] = (
        value + " " + value if change == "append-command" else
        value.replace(" update ;", " update --unsafe ;") if change == "extra-argument" else
        value.replace("ignore_errors=no", "ignore_errors=yes")
    )
    with pytest.raises(firewall.VerificationError):
        firewall.verify(source)


def test_periodic_oneshot_running_is_healthy(installation):
    source, _, units, _ = installation
    units[firewall.SERVICE].update(ActiveState="activating", SubState="start")
    units[firewall.TIMER]["SubState"] = "running"
    firewall.verify(source)


def test_concurrent_installation_change_fails(installation, monkeypatch):
    source, assets, _, _ = installation
    real_show = firewall.show

    def show(unit, properties):
        result = real_show(unit, properties)
        if unit == "docker.service":
            assets[0][1].write_bytes(b"concurrent replacement")
        return result

    monkeypatch.setattr(firewall, "show", show)
    with pytest.raises(firewall.VerificationError):
        firewall.verify(source)


@pytest.mark.parametrize("reply", ("LoadState=loaded\nLoadState=loaded", "", "LoadState=loaded\nOther=x"))
def test_systemd_reply_must_be_complete_and_unambiguous(reply):
    with patch.object(firewall.subprocess, "run", return_value=SimpleNamespace(stdout=reply)):
        with pytest.raises(firewall.VerificationError):
            firewall.show("docker.service", ("LoadState", "Requires"))


@pytest.mark.parametrize("explicit_empty", ("", "ExecStartPre=\n", "ExecStartPre=\nExecStop=\n"))
def test_only_known_empty_exec_arrays_can_be_omitted(explicit_empty):
    reply = "ExecStart=present\n" + explicit_empty
    with patch.object(firewall.subprocess, "run", return_value=SimpleNamespace(stdout=reply)):
        assert firewall.show(firewall.SERVICE, ("ExecStart", "ExecStartPre", "ExecStop")) == {
            "ExecStart": "present", "ExecStartPre": "", "ExecStop": "",
        }


@pytest.mark.parametrize("missing", firewall.COMMON_PROPERTIES + ("ExecStart", "Result", "ExecMainStatus"))
def test_empty_array_compatibility_does_not_allow_missing_required_properties(installation, missing):
    source, _, units, _ = installation
    del units[firewall.SERVICE][missing]
    with pytest.raises(firewall.VerificationError):
        firewall.verify(source)


@pytest.mark.parametrize("error", (subprocess.TimeoutExpired("systemctl", 15),
                                 subprocess.CalledProcessError(1, "systemctl")))
def test_systemd_failure_never_falls_back_to_installation(installation, monkeypatch, error):
    source, _, _, _ = installation
    monkeypatch.setattr(firewall.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(subprocess.SubprocessError):
        firewall.verify(source)


@pytest.mark.parametrize("key,value", (("st_uid", 1000), ("st_gid", 1000), ("st_nlink", 2),
                                      ("st_mode", stat.S_IFREG | 0o777), ("st_mode", stat.S_IFLNK | 0o755)))
def test_regular_asset_metadata_drift_is_rejected(key, value):
    info = dict(st_uid=0, st_gid=0, st_nlink=1, st_mode=stat.S_IFREG | 0o755)
    firewall.regular(SimpleNamespace(**info), 0o755)
    info[key] = value
    with pytest.raises(firewall.VerificationError):
        firewall.regular(SimpleNamespace(**info), 0o755)


@pytest.mark.parametrize("link_target", ("approved-absolute", "approved-relative", "wrong", "chained"))
def test_enable_link_requires_controlled_exact_target(link_target):
    target = firewall.SYSTEMD / firewall.SERVICE
    link = firewall.SYSTEMD / "docker.service.requires" / firewall.SERVICE
    values = {"approved-absolute": str(target), "approved-relative": "../" + target.name,
              "wrong": "/tmp/other.service", "chained": "../alias.service"}
    with patch.object(firewall, "controlled") as controlled, patch.object(
        firewall.os, "readlink", return_value=values[link_target],
    ):
        if link_target.startswith("approved"):
            firewall.check_link(link, target)
        else:
            with pytest.raises(firewall.VerificationError):
                firewall.check_link(link, target)
        controlled.assert_any_call(link, link=True)
        for parent in link.parents:
            controlled.assert_any_call(parent, directory=True)


def test_missing_boot_link_rejects(installation):
    source, _, _, _ = installation
    with patch.object(firewall, "check_link", side_effect=FileNotFoundError("missing enable link")):
        with pytest.raises(FileNotFoundError):
            firewall.verify(source)


def test_unsafe_ancestor_rejects_before_file_open(tmp_path):
    asset = tmp_path / "helper"
    with patch.object(firewall, "controlled", side_effect=SystemExit("unsafe ancestor")), patch.object(
        firewall.os, "open",
    ) as open_file:
        with pytest.raises(SystemExit):
            firewall.digest(asset, 0o755)
        open_file.assert_not_called()
