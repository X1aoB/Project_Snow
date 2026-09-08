#!/usr/bin/env python3
"""Read-only gate for reusing the installed direct-origin firewall."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

from install_maintenance import controlled

BINARY = Path("/usr/local/sbin/project-snow-origin-firewall")
SYSTEMD = Path("/etc/systemd/system")
SERVICE = "project-snow-origin-firewall.service"
TIMER = "project-snow-origin-firewall.timer"
ASSETS = (
    ("scripts/cloudflare_origin_firewall.py", BINARY, 0o755),
    (f"ops/{SERVICE}", SYSTEMD / SERVICE, 0o644),
    (f"ops/{TIMER}", SYSTEMD / TIMER, 0o644),
)
COMMON_PROPERTIES = ("LoadState", "FragmentPath", "DropInPaths", "NeedDaemonReload", "UnitFileState")
EXEC_PROPERTIES = ("ExecStartPre", "ExecStartPost", "ExecCondition", "ExecReload", "ExecStop", "ExecStopPost")


class VerificationError(ValueError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise VerificationError(reason)


def controlled_parents(path: Path) -> None:
    for parent in reversed(path.parents):
        controlled(parent, directory=True)


def regular(info: os.stat_result, mode: int) -> None:
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1,
        "firewall asset ownership, mode, type or link count differs",
    )


def identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def digest(path: Path, mode: int) -> str:
    controlled_parents(path)
    controlled(path)
    before = path.lstat()
    regular(before, mode)
    # All ancestors are root-controlled. Still refuse link substitution and
    # recheck the opened inode so a concurrent maintenance run cannot mix assets.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        regular(opened, mode)
        require(identity(before) == identity(opened), "firewall asset changed while opening")
        payload = source.read(1024 * 1024 + 1)
        require(len(payload) <= 1024 * 1024, "firewall asset exceeds size limit")
        require(identity(opened) == identity(os.fstat(source.fileno())) == identity(path.lstat()),
                "firewall asset changed while reading")
    return hashlib.sha256(payload).hexdigest()


def check_link(path: Path, target: Path) -> None:
    controlled_parents(path)
    controlled(path, link=True)
    # systemctl enable may write either an absolute target or ../<unit>.
    require(os.readlink(path) in (str(target), f"../{target.name}"), "firewall enable link differs")


def show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", "--no-pager", "--property=" + ",".join(properties), unit],
        check=True, capture_output=True, text=True, timeout=15,
        env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "cat"},
    )
    require(len(result.stdout) <= 16384, "systemd reply exceeds size limit")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        require(bool(separator) and key in properties and key not in values, "ambiguous systemd reply")
        values[key] = value
    # systemd's Exec* array printer emits no line for an empty array, even
    # with --property (systemctl-show.c v252). Only these known optional
    # command arrays may be absent; ExecStart and all identity/health fields
    # must still be present. A nonempty array always emits its command(s).
    for key in EXEC_PROPERTIES:
        if key in properties:
            values.setdefault(key, "")
    require(set(values) == set(properties), "incomplete systemd reply")
    return values


def check_unit(unit: str, values: dict[str, str]) -> None:
    expected = {
        "LoadState": "loaded", "FragmentPath": str(SYSTEMD / unit),
        "DropInPaths": "", "NeedDaemonReload": "no", "UnitFileState": "enabled",
    }
    require(all(values[key] == value for key, value in expected.items()), "loaded firewall unit differs")


def verify(configuration_root: Path) -> None:
    snapshots = []
    for relative, installed, mode in ASSETS:
        expected = digest(configuration_root / relative, 0o444)
        require(digest(installed, mode) == expected, "installed firewall asset differs from candidate")
        snapshots.append((installed, mode, expected))
    for directory, unit in (("multi-user.target.wants", SERVICE), ("docker.service.requires", SERVICE),
                            ("timers.target.wants", TIMER)):
        check_link(SYSTEMD / directory / unit, SYSTEMD / unit)
    service = show(SERVICE, COMMON_PROPERTIES + EXEC_PROPERTIES + (
        "ExecStart", "Before", "Type", "ActiveState", "SubState", "Result", "ExecMainStatus",
    ))
    check_unit(SERVICE, service)
    require(all(service[key] == "" for key in EXEC_PROPERTIES), "extra firewall unit commands")
    expected_start = re.escape(f"{{ path={BINARY} ; argv[]={BINARY} update ; ignore_errors=no ; ")
    require(re.fullmatch(expected_start + r"[^{}]*\}", service["ExecStart"]) is not None,
            "loaded firewall ExecStart differs")
    require(service["Type"] == "oneshot" and "docker.service" in service["Before"].split(),
            "firewall boot ordering differs")
    require((service["ActiveState"], service["SubState"]) in (("inactive", "dead"), ("activating", "start"))
            and service["Result"] == "success" and service["ExecMainStatus"] == "0",
            "firewall service is unhealthy")
    timer = show(TIMER, COMMON_PROPERTIES + ("ActiveState", "SubState", "Unit", "Result"))
    check_unit(TIMER, timer)
    require(timer["ActiveState"] == "active" and timer["SubState"] in ("waiting", "running")
            and timer["Unit"] == SERVICE and timer["Result"] == "success", "firewall timer is unhealthy")
    docker = show("docker.service", ("LoadState", "Requires"))
    require(docker["LoadState"] == "loaded" and SERVICE in docker["Requires"].split(),
            "Docker no longer requires the firewall")
    for installed, mode, expected in snapshots:
        require(digest(installed, mode) == expected, "installed firewall changed during verification")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.configuration_root)
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit):
        # Do not echo systemd output or exception details from a privileged gate.
        print("Origin-firewall verification failed; independent maintenance is required.", file=os.sys.stderr)
        return 1
    print("Installed origin-firewall assets and loaded units verified without changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
