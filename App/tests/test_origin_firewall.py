from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from scripts.cloudflare_origin_firewall import (
    API_URL,
    IPV4_URL,
    IPV6_URL,
    ORIGIN_BACKEND_INPUT_DROP_COMMENT,
    ORIGIN_BACKEND_INTERFACE,
    ORIGIN_UPLINK_INTERFACE,
    ORIGIN_FORWARD_DROP_COMMENT,
    ORIGIN_INPUT_DROP_COMMENT,
    CloudflareRanges,
    FirewallError,
    RuntimePaths,
    deserialize_state,
    fetch_cloudflare_ranges,
    load_state,
    parse_plaintext_ranges,
    parse_nft_drop_counters,
    persist_state,
    render_nft_ruleset,
    serialize_state,
    update_firewall,
)

IPV4_CIDRS = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
IPV6_CIDRS = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def _source_payloads(
    *, ipv4: tuple[str, ...] = IPV4_CIDRS, ipv6: tuple[str, ...] = IPV6_CIDRS
) -> dict[str, bytes]:
    return {
        IPV4_URL: ("\n".join(ipv4) + "\n").encode("ascii"),
        IPV6_URL: ("\n".join(ipv6) + "\n").encode("ascii"),
        API_URL: json.dumps(
            {
                "success": True,
                "errors": [],
                "messages": [],
                "result": {
                    "ipv4_cidrs": list(ipv4),
                    "ipv6_cidrs": list(ipv6),
                    "etag": "fixture-etag",
                },
            }
        ).encode("ascii"),
    }


def _fetcher_for(payloads: dict[str, bytes]):
    calls: list[tuple[str, str, int, float]] = []

    def fetcher(url: str, host: str, limit: int, timeout: float) -> bytes:
        calls.append((url, host, limit, timeout))
        return payloads[url]

    return fetcher, calls


def _ranges(payloads: dict[str, bytes] | None = None) -> CloudflareRanges:
    fetcher, _ = _fetcher_for(payloads or _source_payloads())
    return fetch_cloudflare_ranges(fetcher=fetcher)


class FakeRunner:
    def __init__(self, *, fail_nft_apply_number: int | None = None) -> None:
        self.commands: list[tuple[tuple[str, ...], str | None]] = []
        self.table_exists = False
        self.nft_apply_count = 0
        self.fail_nft_apply_number = fail_nft_apply_number

    def __call__(
        self,
        command,
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout
        argv = tuple(command)
        self.commands.append((argv, input_text))
        if argv[0].endswith("/ip") or argv[0] == "ip":
            stdout = '[{"dst":"default","dev":"eth0"}]' if "-4" in argv else "[]"
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        if argv[0].endswith("/ufw") or argv[0] == "ufw":
            if argv[1:3] == ("status", "verbose"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Status: active\nDefault: deny (incoming), allow (outgoing), deny (routed)\n",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "list" in argv and "table" in argv:
            if self.table_exists:
                return subprocess.CompletedProcess(argv, 0, "table inet fixture {}", "")
            return subprocess.CompletedProcess(argv, 1, "", "No such file or directory")
        if "--check" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "--file" in argv:
            self.nft_apply_count += 1
            if self.nft_apply_count == self.fail_nft_apply_number:
                raise FirewallError("injected nft apply failure")
            self.table_exists = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class CloudflareOriginFirewallTests(TestCase):
    def test_three_official_sources_must_match_exactly(self) -> None:
        payloads = _source_payloads()
        fetcher, calls = _fetcher_for(payloads)
        ranges = fetch_cloudflare_ranges(fetcher=fetcher, timeout=7.5)

        self.assertEqual({item[0] for item in calls}, {IPV4_URL, IPV6_URL, API_URL})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(item[3] == 7.5 for item in calls))
        self.assertEqual(set(ranges.ipv4_strings), set(IPV4_CIDRS))
        self.assertEqual(set(ranges.ipv6_strings), set(IPV6_CIDRS))

        api_document = json.loads(payloads[API_URL])
        api_document["result"]["ipv4_cidrs"] = list(IPV4_CIDRS[:-1])
        payloads[API_URL] = json.dumps(api_document).encode("ascii")
        with self.assertRaisesRegex(FirewallError, "IPv4 sources disagree"):
            fetch_cloudflare_ranges(fetcher=_fetcher_for(payloads)[0])

    def test_plaintext_parser_rejects_noncanonical_private_and_duplicates(self) -> None:
        invalid_payloads = (
            ("173.245.48.1/20\n" + "\n".join(IPV4_CIDRS[1:])).encode("ascii"),
            ("10.0.0.0/8\n" + "\n".join(IPV4_CIDRS[1:])).encode("ascii"),
            ("\n".join(IPV4_CIDRS[:-1] + (IPV4_CIDRS[0],))).encode("ascii"),
            ("\n".join(IPV6_CIDRS + ("2400:cb00::/32",))).encode("ascii"),
        )
        for payload in invalid_payloads[:3]:
            with self.subTest(payload=payload[:30]):
                with self.assertRaises(FirewallError):
                    parse_plaintext_ranges(payload, version=4, source="fixture")
        with self.assertRaises(FirewallError):
            parse_plaintext_ranges(invalid_payloads[3], version=6, source="fixture")

    def test_nft_rules_cover_input_and_docker_forward_without_global_flush(self) -> None:
        ruleset = render_nft_ruleset(_ranges(), ("eth0",), replace_existing=True)

        self.assertIn("table inet project_snow_origin_firewall", ruleset)
        self.assertIn("type filter hook input priority -10; policy accept;", ruleset)
        self.assertIn("type filter hook forward priority -10; policy accept;", ruleset)
        self.assertIn("ct direction original meta l4proto tcp", ruleset)
        self.assertIn("ct original proto-dst 443", ruleset)
        self.assertIn("meta l4proto udp ct original proto-dst 443 counter drop", ruleset)
        self.assertIn("tcp dport 443 counter drop", ruleset)
        reply_rule = (
            f'iifname "{ORIGIN_UPLINK_INTERFACE}" '
            "ct direction reply ct state established,related counter accept"
        )
        backend_reply_rule = (
            f'iifname "{ORIGIN_BACKEND_INTERFACE}" '
            "ct direction reply ct state established,related counter accept"
        )
        outbound_drop = f'iifname "{ORIGIN_UPLINK_INTERFACE}" counter drop'
        backend_input_drop = f'iifname "{ORIGIN_BACKEND_INTERFACE}" counter drop'
        self.assertEqual(ruleset.count(reply_rule), 2)
        self.assertEqual(ruleset.count(outbound_drop), 2)
        self.assertEqual(ruleset.count(backend_reply_rule), 1)
        self.assertEqual(ruleset.count(backend_input_drop), 1)
        self.assertEqual(ruleset.count('comment "project-snow-origin-input-drop"'), 1)
        self.assertEqual(
            ruleset.count('comment "project-snow-origin-backend-input-drop"'), 1
        )
        self.assertEqual(ruleset.count('comment "project-snow-origin-forward-drop"'), 1)
        forward_chain = ruleset.index("chain origin_forward")
        self.assertNotIn(f'iifname "{ORIGIN_BACKEND_INTERFACE}"', ruleset[forward_chain:])
        forward_reply = ruleset.index(reply_rule, forward_chain)
        forward_drop = ruleset.index(outbound_drop, forward_reply)
        self.assertLess(forward_reply, forward_drop)
        input_chain = ruleset.index("chain origin_input")
        input_reply = ruleset.index(reply_rule, input_chain)
        input_drop = ruleset.index(outbound_drop, input_reply)
        backend_input_reply = ruleset.index(backend_reply_rule, input_drop)
        backend_drop = ruleset.index(backend_input_drop, backend_input_reply)
        input_cloudflare_accept = ruleset.index(
            "tcp dport 443 ip saddr @cloudflare_ipv4 counter accept", input_chain
        )
        self.assertLess(input_reply, input_drop)
        self.assertLess(input_drop, backend_input_reply)
        self.assertLess(backend_input_reply, backend_drop)
        self.assertLess(backend_drop, input_cloudflare_accept)
        self.assertLess(
            ruleset.index("ct original proto-dst 443 ip saddr @cloudflare_ipv4"),
            forward_reply,
        )
        self.assertNotIn("flush ruleset", ruleset)
        self.assertNotIn("DOCKER-USER", ruleset)

    def test_nft_counter_parser_ignores_match_and_verdict_expressions(self) -> None:
        fixture = {
            "nftables": [
                {"metainfo": {"json_schema_version": 1}},
                {
                    "rule": {
                        "family": "inet",
                        "table": "project_snow_origin_firewall",
                        "chain": "origin_input",
                        "comment": ORIGIN_INPUT_DROP_COMMENT,
                        "expr": [
                            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "ps-origin0"}},
                            {"counter": {"packets": 3, "bytes": 252}},
                            {"drop": None},
                        ],
                    }
                },
                {
                    "rule": {
                        "family": "inet",
                        "table": "project_snow_origin_firewall",
                        "chain": "origin_input",
                        "comment": ORIGIN_BACKEND_INPUT_DROP_COMMENT,
                        "expr": [
                            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "ps-origin1"}},
                            {"counter": {"packets": 5, "bytes": 420}},
                            {"drop": None},
                        ],
                    }
                },
                {
                    "rule": {
                        "family": "inet",
                        "table": "project_snow_origin_firewall",
                        "chain": "origin_forward",
                        "comment": ORIGIN_FORWARD_DROP_COMMENT,
                        "expr": [
                            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "ps-origin0"}},
                            {"counter": {"packets": 7, "bytes": 588}},
                            {"drop": None},
                        ],
                    }
                },
            ]
        }
        self.assertEqual(
            parse_nft_drop_counters(json.dumps(fixture)),
            {"input_uplink": 3, "input_backend": 5, "forward": 7},
        )

        fixture["nftables"][1]["rule"]["expr"].append(
            {"counter": {"packets": 8, "bytes": 1}}
        )
        with self.assertRaisesRegex(FirewallError, "ambiguous packet counter"):
            parse_nft_drop_counters(json.dumps(fixture))

    def test_state_round_trip_is_integrity_checked_and_atomic(self) -> None:
        ranges = _ranges()
        timestamp = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
        payload = serialize_state(ranges, validated_at=timestamp)
        self.assertEqual(deserialize_state(payload, source="fixture"), ranges)

        tampered = json.loads(payload)
        tampered["ipv4_cidrs"] = list(IPV4_CIDRS[:-1])
        with self.assertRaises(FirewallError):
            deserialize_state(json.dumps(tampered).encode(), source="fixture")

        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths(state_dir=Path(directory))
            persist_state(ranges, paths=paths, validated_at=timestamp)
            self.assertEqual(load_state(paths.current_state, required=True), ranges)
            if os.name == "posix":
                self.assertEqual(paths.current_state.stat().st_mode & 0o777, 0o600)

    def test_successful_update_applies_checked_nft_then_commits_lkg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ufw_defaults = root / "ufw"
            ufw_defaults.write_text("IPV6=yes\n", encoding="utf-8")
            paths = RuntimePaths(
                state_dir=root / "state",
                ufw_defaults=ufw_defaults,
                nft="nft",
                ufw="ufw",
                ip="ip",
            )
            runner = FakeRunner()
            fetcher, _ = _fetcher_for(_source_payloads())
            target = update_firewall(
                paths=paths,
                interfaces=("eth0",),
                runner=runner,
                fetcher=fetcher,
                now=lambda: datetime(2026, 8, 22, tzinfo=UTC),
            )

            self.assertEqual(load_state(paths.current_state, required=True), target)
            command_argv = [item[0] for item in runner.commands]
            check_indexes = [i for i, item in enumerate(command_argv) if "--check" in item]
            apply_indexes = [
                i
                for i, item in enumerate(command_argv)
                if "--file" in item and "--check" not in item
            ]
            self.assertTrue(check_indexes)
            self.assertTrue(all(check < apply for check, apply in zip(check_indexes, apply_indexes)))
            self.assertTrue(
                any(item[1:3] == ("--force", "allow") for item in command_argv)
            )

    def test_failed_nft_transition_keeps_old_lkg_and_rolls_back(self) -> None:
        old_payloads = _source_payloads()
        new_ipv4 = IPV4_CIDRS[:-1] + ("8.6.112.0/24",)
        new_payloads = _source_payloads(ipv4=new_ipv4)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ufw_defaults = root / "ufw"
            ufw_defaults.write_text("IPV6=yes\n", encoding="utf-8")
            paths = RuntimePaths(
                state_dir=root / "state",
                ufw_defaults=ufw_defaults,
                nft="nft",
                ufw="ufw",
                ip="ip",
            )
            old_ranges = _ranges(old_payloads)
            persist_state(
                old_ranges,
                paths=paths,
                validated_at=datetime(2026, 8, 21, tzinfo=UTC),
            )
            original_state = paths.current_state.read_bytes()
            runner = FakeRunner(fail_nft_apply_number=2)

            with self.assertRaisesRegex(FirewallError, "rolled back"):
                update_firewall(
                    paths=paths,
                    interfaces=("eth0",),
                    runner=runner,
                    fetcher=_fetcher_for(new_payloads)[0],
                )

            self.assertEqual(paths.current_state.read_bytes(), original_state)
            self.assertGreaterEqual(runner.nft_apply_count, 3)

    def test_systemd_contract_orders_firewall_before_docker_and_refreshes(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        service = (app_root / "ops" / "project-snow-origin-firewall.service").read_text(
            encoding="utf-8"
        )
        timer = (app_root / "ops" / "project-snow-origin-firewall.timer").read_text(
            encoding="utf-8"
        )

        self.assertIn("Before=docker.service", service)
        self.assertIn("RequiredBy=docker.service", service)
        self.assertIn(
            "ExecStart=/usr/local/sbin/project-snow-origin-firewall update", service
        )
        self.assertIn("ReadWritePaths=/etc/ufw /var/lib/project-snow-origin-firewall", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=project-snow-origin-firewall.service", timer)
