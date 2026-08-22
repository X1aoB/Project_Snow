#!/usr/bin/env python3
"""Maintain a fail-closed Cloudflare allowlist for the public HTTPS origin.

The host firewall has two enforcement layers:

* UFW allows TCP/443 only from the validated Cloudflare networks in INPUT.
* A separate nftables inet table applies the same policy before Docker's
  forwarding rules.  The forward chain matches the original destination port,
  because Docker DNAT has already run by the time a forwarded packet is
  filtered.

Updates are deliberately conservative.  Three independent Cloudflare sources
must agree, the nftables replacement is a single transaction, and the last
known-good snapshot is replaced only after both firewall layers succeed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only by non-POSIX test hosts
    fcntl = None  # type: ignore[assignment]


IPV4_URL = "https://www.cloudflare.com/ips-v4"
IPV6_URL = "https://www.cloudflare.com/ips-v6"
API_URL = "https://api.cloudflare.com/client/v4/ips"
EXPECTED_HOSTS = {
    IPV4_URL: "www.cloudflare.com",
    IPV6_URL: "www.cloudflare.com",
    API_URL: "api.cloudflare.com",
}

STATE_SCHEMA = "project-snow-cloudflare-origin-firewall-1"
NFT_TABLE = "project_snow_origin_firewall"
ORIGIN_UPLINK_INTERFACE = "ps-origin0"
ORIGIN_BACKEND_INTERFACE = "ps-origin1"
ORIGIN_INPUT_DROP_COMMENT = "project-snow-origin-input-drop"
ORIGIN_BACKEND_INPUT_DROP_COMMENT = "project-snow-origin-backend-input-drop"
ORIGIN_FORWARD_DROP_COMMENT = "project-snow-origin-forward-drop"
UFW_COMMENT = "project-snow-cloudflare-origin"
MAX_RESPONSE_BYTES = 64 * 1024
MIN_IPV4_NETWORKS = 10
MIN_IPV6_NETWORKS = 3
MAX_NETWORKS_PER_FAMILY = 256
DEFAULT_STATE_DIR = Path("/var/lib/project-snow-origin-firewall")
DEFAULT_UFW_DEFAULTS = Path("/etc/default/ufw")
DEFAULT_NFT = "/usr/sbin/nft"
DEFAULT_UFW = "/usr/sbin/ufw"
DEFAULT_IP = "/usr/sbin/ip"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Fetcher = Callable[[str, str, int, float], bytes]
Runner = Callable[..., subprocess.CompletedProcess[str]]


class FirewallError(RuntimeError):
    """Raised when an update cannot be completed without weakening policy."""


@dataclass(frozen=True)
class CloudflareRanges:
    ipv4: tuple[ipaddress.IPv4Network, ...]
    ipv6: tuple[ipaddress.IPv6Network, ...]
    api_etag: str | None = None

    @property
    def ipv4_strings(self) -> tuple[str, ...]:
        return tuple(str(network) for network in self.ipv4)

    @property
    def ipv6_strings(self) -> tuple[str, ...]:
        return tuple(str(network) for network in self.ipv6)

    @property
    def all_strings(self) -> tuple[str, ...]:
        return self.ipv4_strings + self.ipv6_strings


@dataclass(frozen=True)
class RuntimePaths:
    state_dir: Path = DEFAULT_STATE_DIR
    ufw_defaults: Path = DEFAULT_UFW_DEFAULTS
    nft: str = DEFAULT_NFT
    ufw: str = DEFAULT_UFW
    ip: str = DEFAULT_IP

    @property
    def current_state(self) -> Path:
        return self.state_dir / "last-known-good.json"

    @property
    def previous_state(self) -> Path:
        return self.state_dir / "previous-known-good.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "update.lock"


def _network_sort_key(network: Network) -> tuple[int, int, int]:
    return (network.version, int(network.network_address), network.prefixlen)


def _parse_network_values(
    values: Iterable[Any],
    *,
    version: int,
    source: str,
    enforce_count_floor: bool = True,
) -> tuple[Network, ...]:
    raw_values = list(values)
    minimum = MIN_IPV4_NETWORKS if version == 4 else MIN_IPV6_NETWORKS
    if enforce_count_floor and len(raw_values) < minimum:
        raise FirewallError(f"{source} returned too few IPv{version} networks")
    if not raw_values or len(raw_values) > MAX_NETWORKS_PER_FAMILY:
        raise FirewallError(f"{source} returned an invalid IPv{version} network count")

    parsed: list[Network] = []
    seen: set[str] = set()
    for item in raw_values:
        if not isinstance(item, str) or not item or item != item.strip():
            raise FirewallError(f"{source} contains a malformed IPv{version} CIDR")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise FirewallError(f"{source} contains an invalid CIDR: {item!r}") from exc
        if network.version != version:
            raise FirewallError(f"{source} mixed address families")
        if str(network) != item.lower():
            raise FirewallError(f"{source} contains a non-canonical CIDR: {item!r}")
        if not network.is_global:
            raise FirewallError(f"{source} contains a non-global CIDR: {item!r}")
        minimum_prefix = 8 if version == 4 else 20
        if network.prefixlen < minimum_prefix:
            raise FirewallError(f"{source} contains an excessively broad CIDR: {item!r}")
        canonical = str(network)
        if canonical in seen:
            raise FirewallError(f"{source} contains a duplicate CIDR: {canonical}")
        seen.add(canonical)
        parsed.append(network)

    ordered = sorted(parsed, key=_network_sort_key)
    for index, network in enumerate(ordered):
        for other in ordered[index + 1 :]:
            if network.overlaps(other):
                raise FirewallError(
                    f"{source} contains overlapping CIDRs: {network} and {other}"
                )
    return tuple(ordered)


def parse_plaintext_ranges(payload: bytes, *, version: int, source: str) -> tuple[Network, ...]:
    if not payload or len(payload) > MAX_RESPONSE_BYTES:
        raise FirewallError(f"{source} returned an empty or oversized response")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FirewallError(f"{source} did not return ASCII CIDRs") from exc
    if "\x00" in text:
        raise FirewallError(f"{source} returned a NUL byte")
    lines = text.splitlines()
    if any(not line for line in lines):
        raise FirewallError(f"{source} returned a blank CIDR line")
    return _parse_network_values(lines, version=version, source=source)


def parse_api_ranges(payload: bytes) -> CloudflareRanges:
    if not payload or len(payload) > MAX_RESPONSE_BYTES:
        raise FirewallError("Cloudflare API returned an empty or oversized response")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirewallError("Cloudflare API returned invalid JSON") from exc
    if not isinstance(document, dict) or document.get("success") is not True:
        raise FirewallError("Cloudflare API did not report success")
    if document.get("errors") not in (None, []):
        raise FirewallError("Cloudflare API reported errors")
    result = document.get("result")
    if not isinstance(result, dict):
        raise FirewallError("Cloudflare API response has no result object")
    ipv4 = result.get("ipv4_cidrs")
    ipv6 = result.get("ipv6_cidrs")
    if not isinstance(ipv4, list) or not isinstance(ipv6, list):
        raise FirewallError("Cloudflare API response has malformed CIDR arrays")
    etag = result.get("etag")
    if etag is not None and (not isinstance(etag, str) or not etag or len(etag) > 256):
        raise FirewallError("Cloudflare API returned an invalid etag")
    return CloudflareRanges(
        ipv4=tuple(
            _parse_network_values(ipv4, version=4, source="Cloudflare API IPv4")
        ),
        ipv6=tuple(
            _parse_network_values(ipv6, version=6, source="Cloudflare API IPv6")
        ),
        api_etag=etag,
    )


def fetch_bytes(url: str, expected_host: str, limit: int, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain;q=0.9",
            "User-Agent": "project-snow-origin-firewall/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS URLs
            status = getattr(response, "status", None)
            if status != 200:
                raise FirewallError(f"{url} returned HTTP {status}")
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != expected_host:
                raise FirewallError(f"{url} redirected outside its trusted HTTPS host")
            payload = response.read(limit + 1)
    except FirewallError:
        raise
    except Exception as exc:
        raise FirewallError(f"could not fetch {url}: {exc}") from exc
    if len(payload) > limit:
        raise FirewallError(f"{url} response exceeded {limit} bytes")
    return payload


def fetch_cloudflare_ranges(
    *, fetcher: Fetcher = fetch_bytes, timeout: float = 15.0
) -> CloudflareRanges:
    ipv4_payload = fetcher(
        IPV4_URL, EXPECTED_HOSTS[IPV4_URL], MAX_RESPONSE_BYTES, timeout
    )
    ipv6_payload = fetcher(
        IPV6_URL, EXPECTED_HOSTS[IPV6_URL], MAX_RESPONSE_BYTES, timeout
    )
    api_payload = fetcher(API_URL, EXPECTED_HOSTS[API_URL], MAX_RESPONSE_BYTES, timeout)

    plaintext_ipv4 = parse_plaintext_ranges(
        ipv4_payload, version=4, source="Cloudflare ips-v4"
    )
    plaintext_ipv6 = parse_plaintext_ranges(
        ipv6_payload, version=6, source="Cloudflare ips-v6"
    )
    api_ranges = parse_api_ranges(api_payload)
    if set(plaintext_ipv4) != set(api_ranges.ipv4):
        raise FirewallError("Cloudflare IPv4 sources disagree; keeping last-known-good rules")
    if set(plaintext_ipv6) != set(api_ranges.ipv6):
        raise FirewallError("Cloudflare IPv6 sources disagree; keeping last-known-good rules")
    return CloudflareRanges(
        ipv4=tuple(sorted(plaintext_ipv4, key=_network_sort_key)),
        ipv6=tuple(sorted(plaintext_ipv6, key=_network_sort_key)),
        api_etag=api_ranges.api_etag,
    )


def run_command(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            input=input_text,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FirewallError(f"could not run {command[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise FirewallError(f"command failed ({result.returncode}): {command[0]}: {detail}")
    return result


def _validate_interface(interface: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise FirewallError(f"unsafe network interface name: {interface!r}")
    return interface


def detect_external_interfaces(*, runner: Runner, ip_binary: str) -> tuple[str, ...]:
    interfaces: set[str] = set()
    successful_queries = 0
    for family in ("-4", "-6"):
        result = runner(
            [ip_binary, "-j", family, "route", "show", "default"], check=False
        )
        if result.returncode != 0:
            continue
        successful_queries += 1
        try:
            routes = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FirewallError("ip returned malformed JSON while detecting interfaces") from exc
        if not isinstance(routes, list):
            raise FirewallError("ip returned a malformed default-route list")
        for route in routes:
            if not isinstance(route, dict):
                raise FirewallError("ip returned a malformed default route")
            device = route.get("dev")
            if isinstance(device, str):
                interfaces.add(_validate_interface(device))
    if not successful_queries or not interfaces:
        raise FirewallError("could not determine an external default-route interface")
    return tuple(sorted(interfaces))


def _render_set(name: str, nft_type: str, elements: Sequence[str]) -> list[str]:
    lines = [f"\tset {name} {{", f"\t\ttype {nft_type}"]
    if nft_type in {"ipv4_addr", "ipv6_addr"}:
        lines.append("\t\tflags interval")
    if elements:
        rendered = ", ".join(elements)
        lines.append(f"\t\telements = {{ {rendered} }}")
    lines.append("\t}")
    return lines


def render_nft_ruleset(
    ranges: CloudflareRanges,
    interfaces: Sequence[str],
    *,
    replace_existing: bool,
) -> str:
    safe_interfaces = tuple(sorted({_validate_interface(item) for item in interfaces}))
    if not safe_interfaces:
        raise FirewallError("at least one external interface is required")
    lines: list[str] = []
    if replace_existing:
        lines.append(f"delete table inet {NFT_TABLE}")
        lines.append("")
    lines.append(f"table inet {NFT_TABLE} {{")
    lines.extend(
        _render_set(
            "external_interfaces",
            "ifname",
            tuple(f'"{interface}"' for interface in safe_interfaces),
        )
    )
    lines.extend(_render_set("cloudflare_ipv4", "ipv4_addr", ranges.ipv4_strings))
    lines.extend(_render_set("cloudflare_ipv6", "ipv6_addr", ranges.ipv6_strings))
    lines.extend(
        [
            "",
            "\tchain origin_input {",
            "\t\ttype filter hook input priority -10; policy accept;",
            (
                f'\t\tiifname "{ORIGIN_UPLINK_INTERFACE}" '
                "ct direction reply ct state established,related counter accept"
            ),
            (
                f'\t\tiifname "{ORIGIN_UPLINK_INTERFACE}" counter drop '
                f'comment "{ORIGIN_INPUT_DROP_COMMENT}"'
            ),
            (
                f'\t\tiifname "{ORIGIN_BACKEND_INTERFACE}" '
                "ct direction reply ct state established,related counter accept"
            ),
            (
                f'\t\tiifname "{ORIGIN_BACKEND_INTERFACE}" counter drop '
                f'comment "{ORIGIN_BACKEND_INPUT_DROP_COMMENT}"'
            ),
            "\t\tiifname @external_interfaces tcp dport 443 ip saddr @cloudflare_ipv4 counter accept",
            "\t\tiifname @external_interfaces tcp dport 443 ip6 saddr @cloudflare_ipv6 counter accept",
            "\t\tiifname @external_interfaces tcp dport 443 counter drop",
            "\t\tiifname @external_interfaces udp dport 443 counter drop",
            "\t}",
            "",
            "\tchain origin_forward {",
            "\t\ttype filter hook forward priority -10; policy accept;",
            (
                "\t\tiifname @external_interfaces ct direction original meta l4proto tcp "
                "ct original proto-dst 443 ip saddr @cloudflare_ipv4 counter accept"
            ),
            (
                "\t\tiifname @external_interfaces ct direction original meta l4proto tcp "
                "ct original proto-dst 443 ip6 saddr @cloudflare_ipv6 counter accept"
            ),
            (
                "\t\tiifname @external_interfaces ct direction original meta l4proto tcp "
                "ct original proto-dst 443 counter drop"
            ),
            (
                "\t\tiifname @external_interfaces ct direction original meta l4proto udp "
                "ct original proto-dst 443 counter drop"
            ),
            # origin-edge holds the origin TLS private key but has no reason to
            # initiate Internet connections.  Its dedicated bridge may only
            # emit reply packets for connections admitted above.  Every other
            # forwarded packet entering from this bridge is dropped; the input
            # chain above separately rejects container-initiated host traffic.
            (
                f'\t\tiifname "{ORIGIN_UPLINK_INTERFACE}" '
                "ct direction reply ct state established,related counter accept"
            ),
            (
                f'\t\tiifname "{ORIGIN_UPLINK_INTERFACE}" counter drop '
                f'comment "{ORIGIN_FORWARD_DROP_COMMENT}"'
            ),
            "\t}",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def nft_table_exists(*, runner: Runner, nft_binary: str) -> bool:
    result = runner(
        [nft_binary, "list", "table", "inet", NFT_TABLE], check=False
    )
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout).lower()
    if "no such file" in detail or "does not exist" in detail:
        return False
    raise FirewallError(f"could not inspect nftables table: {detail.strip()[:500]}")


def apply_nft_ruleset(
    ranges: CloudflareRanges,
    interfaces: Sequence[str],
    *,
    runner: Runner,
    nft_binary: str,
) -> None:
    ruleset = render_nft_ruleset(
        ranges,
        interfaces,
        replace_existing=nft_table_exists(runner=runner, nft_binary=nft_binary),
    )
    runner([nft_binary, "--check", "--file", "-"], input_text=ruleset)
    runner([nft_binary, "--file", "-"], input_text=ruleset)


def parse_nft_drop_counters(payload: str | bytes) -> dict[str, int]:
    try:
        document = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirewallError("nftables counter output is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("nftables"), list):
        raise FirewallError("nftables counter output has an invalid document shape")
    expected = {
        ORIGIN_INPUT_DROP_COMMENT: "input_uplink",
        ORIGIN_BACKEND_INPUT_DROP_COMMENT: "input_backend",
        ORIGIN_FORWARD_DROP_COMMENT: "forward",
    }
    counters: dict[str, int] = {}
    for item in document["nftables"]:
        if not isinstance(item, dict) or not isinstance(item.get("rule"), dict):
            continue
        rule = item["rule"]
        field = expected.get(rule.get("comment"))
        if field is None:
            continue
        expressions = rule.get("expr")
        if not isinstance(expressions, list):
            raise FirewallError("origin drop rule has no expression array")
        packets = [
            expression["counter"].get("packets")
            for expression in expressions
            if isinstance(expression, dict)
            and isinstance(expression.get("counter"), dict)
            and "packets" in expression["counter"]
        ]
        if len(packets) != 1:
            raise FirewallError("origin drop rule has an ambiguous packet counter")
        packet_count = packets[0]
        if isinstance(packet_count, bool) or not isinstance(packet_count, int) or packet_count < 0:
            raise FirewallError("origin drop rule has an invalid packet counter")
        if field in counters:
            raise FirewallError("origin drop rule comment is duplicated")
        counters[field] = packet_count
    if set(counters) != {"input_uplink", "input_backend", "forward"}:
        raise FirewallError("origin drop counters are missing")
    return counters


def read_nft_drop_counters(*, runner: Runner, nft_binary: str) -> dict[str, int]:
    result = runner(
        [nft_binary, "--json", "list", "table", "inet", NFT_TABLE]
    )
    return parse_nft_drop_counters(result.stdout)


def _read_ufw_ipv6_setting(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FirewallError(f"could not read {path}: {exc}") from exc
    matches = re.findall(r"^\s*IPV6\s*=\s*([^#\s]+)", content, flags=re.MULTILINE)
    return bool(matches) and matches[-1].lower() == "yes"


def assert_ufw_fail_closed(*, runner: Runner, paths: RuntimePaths) -> None:
    if not _read_ufw_ipv6_setting(paths.ufw_defaults):
        raise FirewallError("UFW IPv6 support must be enabled before managing origin rules")
    status_result = runner([paths.ufw, "status", "verbose"])
    status = status_result.stdout
    if not re.search(r"^Status:\s+active\s*$", status, flags=re.MULTILINE):
        raise FirewallError("UFW must already be active")
    if not re.search(r"^Default:\s+deny \(incoming\)", status, flags=re.MULTILINE):
        raise FirewallError("UFW incoming policy must already be deny")


def _ufw_allow(network: str, *, runner: Runner, ufw_binary: str) -> None:
    runner(
        [
            ufw_binary,
            "--force",
            "allow",
            "from",
            network,
            "to",
            "any",
            "port",
            "443",
            "proto",
            "tcp",
            "comment",
            UFW_COMMENT,
        ]
    )


def _ufw_delete(network: str, *, runner: Runner, ufw_binary: str) -> None:
    runner(
        [
            ufw_binary,
            "--force",
            "delete",
            "allow",
            "from",
            network,
            "to",
            "any",
            "port",
            "443",
            "proto",
            "tcp",
        ]
    )


def _empty_ranges() -> CloudflareRanges:
    return CloudflareRanges(ipv4=(), ipv6=())


def _ranges_payload(ranges: CloudflareRanges) -> dict[str, Any]:
    return {
        "ipv4_cidrs": list(ranges.ipv4_strings),
        "ipv6_cidrs": list(ranges.ipv6_strings),
    }


def _ranges_digest(ranges: CloudflareRanges) -> str:
    canonical = json.dumps(
        _ranges_payload(ranges), sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return sha256(canonical).hexdigest()


def serialize_state(ranges: CloudflareRanges, *, validated_at: datetime) -> bytes:
    payload = {
        "schema_version": STATE_SCHEMA,
        **_ranges_payload(ranges),
        "ranges_sha256": _ranges_digest(ranges),
        "cloudflare_api_etag": ranges.api_etag,
        "validated_at": validated_at.astimezone(UTC).isoformat(),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def deserialize_state(payload: bytes, *, source: str) -> CloudflareRanges:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirewallError(f"{source} is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != STATE_SCHEMA:
        raise FirewallError(f"{source} has an unsupported schema")
    ipv4_values = document.get("ipv4_cidrs")
    ipv6_values = document.get("ipv6_cidrs")
    if not isinstance(ipv4_values, list) or not isinstance(ipv6_values, list):
        raise FirewallError(f"{source} has malformed CIDR arrays")
    etag = document.get("cloudflare_api_etag")
    if etag is not None and not isinstance(etag, str):
        raise FirewallError(f"{source} has a malformed API etag")
    ranges = CloudflareRanges(
        ipv4=tuple(
            _parse_network_values(
                ipv4_values, version=4, source=f"{source} IPv4"
            )
        ),
        ipv6=tuple(
            _parse_network_values(
                ipv6_values, version=6, source=f"{source} IPv6"
            )
        ),
        api_etag=etag,
    )
    digest = document.get("ranges_sha256")
    if not isinstance(digest, str) or digest != _ranges_digest(ranges):
        raise FirewallError(f"{source} failed its CIDR integrity check")
    validated_at = document.get("validated_at")
    if not isinstance(validated_at, str):
        raise FirewallError(f"{source} has no validation timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(validated_at)
    except ValueError as exc:
        raise FirewallError(f"{source} has an invalid validation timestamp") from exc
    if parsed_timestamp.tzinfo is None:
        raise FirewallError(f"{source} validation timestamp is not timezone-aware")
    return ranges


def _requires_root_owner(path: Path) -> bool:
    if os.name != "posix":
        return False
    try:
        path.resolve(strict=False).relative_to(DEFAULT_STATE_DIR)
    except ValueError:
        return False
    return True


def _assert_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FirewallError(f"could not inspect {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1:
        raise FirewallError(f"protected state path is not a single regular file: {path}")
    if _requires_root_owner(path) and metadata.st_uid != 0:
        raise FirewallError(f"protected state file is not owned by root: {path}")
    if os.name == "posix" and metadata.st_mode & 0o077:
        raise FirewallError(f"protected state file permissions are too broad: {path}")


def load_state(path: Path, *, required: bool) -> CloudflareRanges | None:
    if path.is_symlink():
        raise FirewallError(f"protected state path must not be a symlink: {path}")
    if not path.exists():
        if required:
            raise FirewallError(f"last-known-good state does not exist: {path}")
        return None
    _assert_regular_file(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise FirewallError(f"could not read {path}: {exc}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise FirewallError(f"state file is oversized: {path}")
    return deserialize_state(payload, source=str(path))


def _ensure_state_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise FirewallError(f"state directory is not a real directory: {path}")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise FirewallError(f"could not secure state directory {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    _ensure_state_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - compatibility for non-POSIX Python builds
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def persist_state(
    ranges: CloudflareRanges,
    *,
    paths: RuntimePaths,
    validated_at: datetime,
) -> None:
    if paths.current_state.exists():
        current_payload = paths.current_state.read_bytes()
        deserialize_state(current_payload, source=str(paths.current_state))
        _atomic_write(paths.previous_state, current_payload)
    _atomic_write(paths.current_state, serialize_state(ranges, validated_at=validated_at))


@contextmanager
def state_lock(paths: RuntimePaths) -> Iterable[None]:
    _ensure_state_directory(paths.state_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(paths.lock_file, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FirewallError("firewall update lock is not a single regular file")
        if _requires_root_owner(paths.lock_file) and metadata.st_uid != 0:
            raise FirewallError("firewall update lock is not owned by root")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - compatibility for non-POSIX Python builds
            os.chmod(paths.lock_file, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_ufw_ranges(
    ranges: CloudflareRanges, *, runner: Runner, paths: RuntimePaths
) -> None:
    for network in ranges.all_strings:
        _ufw_allow(network, runner=runner, ufw_binary=paths.ufw)


def _delete_ufw_ranges(
    networks: Iterable[str], *, runner: Runner, paths: RuntimePaths
) -> None:
    for network in sorted(set(networks)):
        _ufw_delete(network, runner=runner, ufw_binary=paths.ufw)


def _rollback_firewalls(
    baseline: CloudflareRanges,
    attempted: CloudflareRanges,
    interfaces: Sequence[str],
    *,
    runner: Runner,
    paths: RuntimePaths,
) -> None:
    errors: list[str] = []
    try:
        apply_nft_ruleset(
            baseline, interfaces, runner=runner, nft_binary=paths.nft
        )
    except FirewallError as exc:
        errors.append(f"nftables rollback failed: {exc}")
    try:
        _ensure_ufw_ranges(baseline, runner=runner, paths=paths)
        stale = set(attempted.all_strings) - set(baseline.all_strings)
        _delete_ufw_ranges(stale, runner=runner, paths=paths)
    except FirewallError as exc:
        errors.append(f"UFW rollback failed: {exc}")
    if errors:
        raise FirewallError("; ".join(errors))


def _apply_transition(
    target: CloudflareRanges,
    baseline: CloudflareRanges | None,
    interfaces: Sequence[str],
    *,
    runner: Runner,
    paths: RuntimePaths,
    persist: bool,
    validated_at: datetime,
) -> None:
    safe_baseline = baseline or _empty_ranges()
    assert_ufw_fail_closed(runner=runner, paths=paths)

    # Re-establish a known policy before adding any new UFW permits.  With no
    # snapshot this creates an empty allowlist, which drops every external 443
    # packet until the validated target transaction commits.
    apply_nft_ruleset(
        safe_baseline, interfaces, runner=runner, nft_binary=paths.nft
    )
    try:
        _ensure_ufw_ranges(target, runner=runner, paths=paths)
        apply_nft_ruleset(target, interfaces, runner=runner, nft_binary=paths.nft)
        stale = set(safe_baseline.all_strings) - set(target.all_strings)
        _delete_ufw_ranges(stale, runner=runner, paths=paths)
        if persist:
            persist_state(target, paths=paths, validated_at=validated_at)
    except (FirewallError, OSError) as exc:
        try:
            _rollback_firewalls(
                safe_baseline, target, interfaces, runner=runner, paths=paths
            )
        except FirewallError as rollback_exc:
            raise FirewallError(f"transition failed: {exc}; {rollback_exc}") from exc
        raise FirewallError(f"transition failed and was rolled back: {exc}") from exc


def restore_firewall(
    *,
    paths: RuntimePaths,
    interfaces: Sequence[str] | None = None,
    runner: Runner = run_command,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CloudflareRanges:
    del now  # Kept injectable for a stable public operation signature.
    with state_lock(paths):
        target = load_state(paths.current_state, required=True)
        assert target is not None
        resolved_interfaces = (
            tuple(_validate_interface(item) for item in interfaces)
            if interfaces
            else detect_external_interfaces(runner=runner, ip_binary=paths.ip)
        )
        _apply_transition(
            target,
            target,
            resolved_interfaces,
            runner=runner,
            paths=paths,
            persist=False,
            validated_at=datetime.now(UTC),
        )
        return target


def update_firewall(
    *,
    paths: RuntimePaths,
    interfaces: Sequence[str] | None = None,
    runner: Runner = run_command,
    fetcher: Fetcher = fetch_bytes,
    timeout: float = 15.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CloudflareRanges:
    with state_lock(paths):
        baseline = load_state(paths.current_state, required=False)
        resolved_interfaces = (
            tuple(_validate_interface(item) for item in interfaces)
            if interfaces
            else detect_external_interfaces(runner=runner, ip_binary=paths.ip)
        )
        try:
            target = fetch_cloudflare_ranges(fetcher=fetcher, timeout=timeout)
        except FirewallError as fetch_error:
            if baseline is None:
                raise
            print(
                f"Cloudflare refresh failed; restoring last-known-good rules: {fetch_error}",
                file=sys.stderr,
            )
            _apply_transition(
                baseline,
                baseline,
                resolved_interfaces,
                runner=runner,
                paths=paths,
                persist=False,
                validated_at=now(),
            )
            return baseline

        _apply_transition(
            target,
            baseline,
            resolved_interfaces,
            runner=runner,
            paths=paths,
            persist=True,
            validated_at=now(),
        )
        return target


def _require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise FirewallError("run this command as root from the VPS console")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("update", "restore", "counters"))
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIR, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--ufw-defaults",
        type=Path,
        default=DEFAULT_UFW_DEFAULTS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--interface", action="append", dest="interfaces")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        _require_root()
        paths = RuntimePaths(
            state_dir=arguments.state_dir, ufw_defaults=arguments.ufw_defaults
        )
        if arguments.command == "counters":
            counters = read_nft_drop_counters(runner=run_command, nft_binary=paths.nft)
            print(json.dumps(counters, sort_keys=True, separators=(",", ":")))
            return 0
        if arguments.command == "update":
            ranges = update_firewall(
                paths=paths,
                interfaces=arguments.interfaces,
                timeout=arguments.timeout,
            )
        else:
            ranges = restore_firewall(
                paths=paths,
                interfaces=arguments.interfaces,
            )
    except FirewallError as exc:
        print(f"origin firewall error: {exc}", file=sys.stderr)
        return 1
    print(
        f"origin firewall active with {len(ranges.ipv4)} IPv4 and "
        f"{len(ranges.ipv6)} IPv6 Cloudflare networks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
