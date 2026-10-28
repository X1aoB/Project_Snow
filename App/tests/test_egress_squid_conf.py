"""Parse infra/egress-squid.conf and assert the variant-B invariants.

The relay egress proxy must DENY every private/internal destination before any
allow rule, and must still allow public CONNECT 443 so official vendors and a
user's chosen relay host are reachable. A regression here is an SSRF hole, so
the ordering and the full private-range list are pinned in a test.
"""

from __future__ import annotations

from pathlib import Path

CONF = Path(__file__).resolve().parents[1] / "infra" / "egress-squid.conf"

# Every destination range that MUST be denied before any allow rule.
REQUIRED_DENY_RANGES = {
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "127.0.0.0/8",
    "100.64.0.0/10",
    "169.254.169.254/32",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "fd00:ec2::254/128",
}


def _lines() -> list[str]:
    return [line.strip() for line in CONF.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _acl_ranges() -> dict[str, set[str]]:
    """Map each `acl <name> dst <range...>` to the set of ranges it names."""
    ranges: dict[str, set[str]] = {}
    for line in _lines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "acl" and parts[2] == "dst":
            ranges.setdefault(parts[1], set()).update(parts[3:])
    return ranges


def test_conf_exists_and_is_https_connect_only() -> None:
    lines = _lines()
    assert "acl SSL_ports port 443" in lines
    assert "acl CONNECT method CONNECT" in lines
    assert "http_access deny !SSL_ports" in lines
    assert "http_access allow CONNECT SSL_ports" in lines
    assert lines[-1 - lines[::-1].index("http_access deny all")] == "http_access deny all"


def test_every_private_range_is_declared() -> None:
    declared: set[str] = set()
    for acl_ranges in _acl_ranges().values():
        declared.update(acl_ranges)
    missing = REQUIRED_DENY_RANGES - declared
    assert not missing, f"private ranges not declared in an acl: {sorted(missing)}"


def test_private_ranges_are_denied_before_any_allow() -> None:
    lines = _lines()
    acl_ranges = _acl_ranges()

    first_allow = next((index for index, line in enumerate(lines)
                        if line.startswith("http_access allow")), None)
    assert first_allow is not None, "conf must contain at least one allow rule"

    # Build the set of acl names whose deny appears before the first allow.
    denied_before_allow: set[str] = set()
    for line in lines[:first_allow]:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "http_access" and parts[1] == "deny":
            denied_before_allow.add(parts[2])

    # Every required range must belong to an acl that is denied before allow.
    for required in REQUIRED_DENY_RANGES:
        owning_acls = {name for name, ranges in acl_ranges.items() if required in ranges}
        assert owning_acls, f"no acl declares {required}"
        assert owning_acls & denied_before_allow, (
            f"{required} is declared but not denied before the first allow rule"
        )


def test_official_vendor_hosts_remain_reachable() -> None:
    text = CONF.read_text(encoding="utf-8")
    for host in ("api.anthropic.com", "api.x.ai", "generativelanguage.googleapis.com"):
        assert host in text, f"official vendor host {host} is not present in the conf"


def test_no_allow_precedes_a_private_deny() -> None:
    """A stricter ordering check: no http_access allow line may appear before
    the last private-range deny line."""
    lines = _lines()
    deny_indexes = [index for index, line in enumerate(lines)
                    if line.startswith("http_access deny") and any(
                        name in line for name in _acl_ranges())]
    allow_indexes = [index for index, line in enumerate(lines)
                     if line.startswith("http_access allow")]
    assert deny_indexes and allow_indexes
    assert max(deny_indexes) < min(allow_indexes), (
        "a private-range deny appears after an allow rule"
    )


def test_no_rule_names_the_v4_mapped_range() -> None:
    """Squid keeps every IPv4 address as v4-mapped IPv6 (::ffff:a.b.c.d), so a
    dst ACL over ::ffff:0:0/96 matches all IPv4 targets. 1.1.0's first promotion
    stopped on exactly that: public IPv4 CONNECTs answered 403 until the line was
    removed. The v4 ranges already cover mapped forms of private addresses."""
    for name, ranges in _acl_ranges().items():
        mapped = sorted(value for value in ranges if value.lower().startswith("::ffff:"))
        assert not mapped, f"acl {name} names v4-mapped ranges {mapped}"
