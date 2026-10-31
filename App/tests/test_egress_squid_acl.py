"""Opt-in real squid check of infra/egress-squid.conf.

test_egress_squid_conf.py reads the conf as text. It passed while the 1.1.0
conf listed `acl to_reserved6 dst ::ffff:0:0/96`: squid keeps every IPv4
address in that v4-mapped form, so the line denied all public IPv4, and only
promote's post-switch egress probe caught it (exit 73). This module starts the
production egress-proxy image as compose.prod.yml runs it, with the repository
conf mounted read-only but with no network at all, and sends CONNECT requests
through it from inside the container. Nothing leaves the host: squid answers
403 when an ACL denies the target and a gateway error (503) when it allows the
target but cannot reach it.

Run on a Docker host after pulling the immutable image:
SNOW_SQUID_INTEGRATION=1 SNOW_TEST_SQUID_IMAGE=<ubuntu/squid digest reference> pytest -q this_file
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import pytest
import yaml

pytestmark = pytest.mark.skipif(os.environ.get("SNOW_SQUID_INTEGRATION") != "1",
                                reason="explicit isolated Docker validation required")

APP = Path(__file__).resolve().parents[1]
CONF = APP / "infra" / "egress-squid.conf"
IMAGE = os.environ.get("SNOW_TEST_SQUID_IMAGE", "")
# Allowed but unreachable: squid tried to dial out and had no network.
UNREACHABLE = {"502", "503", "504"}

PUBLIC = (
    "1.1.1.1:443", "8.8.8.8:443",
    # The addresses just outside each denied IPv4 range; a widened mask denies them.
    "9.255.255.254:443", "11.0.0.1:443",
    "172.15.255.254:443", "172.32.0.1:443",
    "192.167.255.254:443", "192.169.0.1:443",
    "126.255.255.254:443", "128.0.0.1:443",
    "169.253.255.254:443", "169.255.0.1:443",
    "100.63.255.254:443", "100.128.0.1:443",
    # A public address written v4-mapped, and public IPv6
    "[::ffff:1.1.1.1]:443",
    "[2606:4700:4700::1111]:443", "[2001:4860:4860::8888]:443",
)

DENIED = (
    # RFC 1918, loopback, link-local with the metadata address, CGNAT
    "10.0.0.1:443", "10.255.255.254:443",
    "172.16.0.1:443", "172.31.255.254:443",
    "192.168.0.1:443", "192.168.255.254:443",
    "127.0.0.1:443", "127.255.255.254:443",
    "169.254.0.1:443", "169.254.169.254:443",
    "100.64.0.1:443", "100.127.255.254:443",
    # The same targets v4-mapped, which the IPv4 rules match, and in NAT64
    # 64:ff9b::/96 form, which a NAT64 gateway would turn back into them
    "[::ffff:10.0.0.1]:443", "[::ffff:172.16.0.1]:443", "[::ffff:192.168.0.1]:443",
    "[::ffff:127.0.0.1]:443", "[::ffff:169.254.169.254]:443", "[::ffff:100.64.0.1]:443",
    "[64:ff9b::10.0.0.1]:443", "[64:ff9b::172.16.0.1]:443", "[64:ff9b::192.168.0.1]:443",
    "[64:ff9b::127.0.0.1]:443", "[64:ff9b::169.254.169.254]:443", "[64:ff9b::100.64.0.1]:443",
    # IPv6 loopback, ULA, link-local and the IPv6 metadata address
    "[::1]:443", "[fc00::1]:443", "[fd00::1]:443", "[fe80::1]:443", "[fd00:ec2::254]:443",
    # CONNECT is allowed to port 443 only, even for a public address
    "1.1.1.1:80",
)

# One connection per target to squid in its own network namespace. Prints
# "<target> <status>", or 000 when squid sent no status line.
PROBE = r"""
for target in "$@"; do
  status=000
  if exec 3<>/dev/tcp/127.0.0.1/3128; then
    printf 'CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' "$target" "$target" >&3
    read -r -t 10 _ status _ <&3 || status=000
    exec 3<&-
  fi
  echo "$target ${status%$'\r'}"
done
"""


def _docker(*arguments: str, timeout: float = 120) -> str:
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f"docker {arguments[0]} failed: {result.stderr.strip()[-2000:]}"
    return result.stdout.strip()


def _service_options() -> list[str]:
    """Run options of the compose egress-proxy service; --network none replaces its networks."""
    service = yaml.safe_load((APP / "compose.prod.yml").read_text(encoding="utf-8"))["services"]["egress-proxy"]
    unmirrored = set(service) - {"image", "restart", "volumes", "cpus", "mem_limit", "read_only", "tmpfs",
                                 "logging", "networks"}
    assert not unmirrored, f"compose egress-proxy gained {sorted(unmirrored)}; mirror them here"
    assert service["volumes"] == ["./infra/egress-squid.conf:/etc/squid/squid.conf:ro"]
    options = ["--read-only"] if service.get("read_only") else []
    options += ["--cpus", str(service["cpus"]), "--memory", str(service["mem_limit"])]
    for mount in service.get("tmpfs", []):
        options += ["--tmpfs", mount]
    return options


def _answers(conf: Path) -> dict[str, str]:
    """Start squid on `conf` with no network and return each target's CONNECT status."""
    assert re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", IMAGE), "Explicit digest pin required"
    options = _service_options()
    container = "snow-egress-acl-" + uuid.uuid4().hex
    try:
        _docker("run", "--detach", "--name", container, "--pull", "never", "--network", "none", *options,
                "--mount", f"type=bind,source={conf},target=/etc/squid/squid.conf,readonly", IMAGE)
        deadline = time.monotonic() + 60
        while subprocess.run(["docker", "exec", container, "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/3128"],
                             capture_output=True, timeout=30).returncode != 0:
            assert _docker("inspect", "--format", "{{.State.Running}}", container) == "true", "squid exited"
            assert time.monotonic() < deadline, "squid did not listen on 3128 within 60 s"
            time.sleep(0.5)
        reply = _docker("exec", container, "bash", "-c", PROBE, "probe", *PUBLIC, *DENIED, timeout=300)
        answers = dict(line.split(" ", 1) for line in reply.splitlines())
        assert sorted(answers) == sorted({*PUBLIC, *DENIED}), reply
        print(json.dumps({"conf_sha256": hashlib.sha256(conf.read_bytes()).hexdigest(), "answers": answers},
                         sort_keys=True))
        return answers
    except Exception:
        logs = subprocess.run(["docker", "logs", "--tail", "100", container], capture_output=True, text=True,
                              timeout=30)
        print(logs.stdout + logs.stderr)
        raise
    finally:
        # Only the uniquely named container started here is removed; never prune a shared Docker daemon.
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)


def _ipv4(target: str) -> bool:
    address = ipaddress.ip_address(target.rsplit(":", 1)[0].strip("[]"))
    return address.version == 4 or address.ipv4_mapped is not None


@pytest.fixture(scope="module")
def answers() -> dict[str, str]:
    return _answers(CONF)


@pytest.mark.parametrize("target", PUBLIC)
def test_public_target_is_not_denied(answers: dict[str, str], target: str) -> None:
    assert answers[target] in UNREACHABLE, f"CONNECT {target} answered {answers[target]}"


@pytest.mark.parametrize("target", DENIED)
def test_internal_target_is_denied(answers: dict[str, str], target: str) -> None:
    assert answers[target] == "403", f"CONNECT {target} answered {answers[target]}"


def test_the_1_1_0_v4_mapped_line_is_caught(tmp_path: Path) -> None:
    """Put the 1.1.0 line back in a temporary copy: the public target check must
    then fail for every IPv4 target, plain or v4-mapped, as production's probe
    did, while public IPv6 and every internal denial still pass."""
    anchor = "acl to_reserved6 dst ::/128\n"
    text = CONF.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    broken = tmp_path / "egress-squid.conf"
    broken.write_text(text.replace(anchor, anchor + "acl to_reserved6 dst ::ffff:0:0/96\n"), encoding="utf-8",
                      newline="\n")
    answers = _answers(broken)
    for target in PUBLIC:
        if _ipv4(target):
            with pytest.raises(AssertionError, match="answered 403"):
                test_public_target_is_not_denied(answers, target)
        else:
            test_public_target_is_not_denied(answers, target)
    for target in DENIED:
        test_internal_target_is_denied(answers, target)
