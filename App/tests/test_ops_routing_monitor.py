from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import shutil
from types import SimpleNamespace

import pytest

from .test_release_state import state, portable_metadata_checks, write
import maintenance
import monitor
import routing


@pytest.fixture
def edge(state, monkeypatch):
    root = state.root / "runtime" / "routing"
    root.mkdir()
    monkeypatch.setattr(routing, "routing_root", lambda paths: root)
    configuration = state.root / "releases" / "configurations" / ("d" * 40)
    write(configuration / "infra" / "Caddyfile", routing.IMPORT + "\n")
    write(configuration / "compose.prod.yml", "name: project-snow-public\n")
    environment = state.root / "runtime" / "colours" / "green.compose.env"
    containers = {name: {"Id": char * 64, "Image": "sha256:" + "f" * 64, "Config": {"Image": "pinned"}, "HostConfig": {},
                         "Mounts": [{"Source": str(root), "Destination": "/etc/project-snow-routing", "RW": False}],
                         "NetworkSettings": {"Networks": {"app": {"NetworkID": "d" * 64}}}}
                  for name, char in (("caddy", "1"), ("cloudflared", "2"), ("egress-proxy", "3"))}
    calls = []
    def execute(command):
        calls.append(command)
        if command[1] == "compose":
            return json.dumps({"services": {name: {"image": "pinned", "networks": {"app": {}}} for name in containers},
                               "networks": {"app": {"name": "project-snow-public_app", "internal": True}}})
        if command[1] == "ps":
            name = command[-1].split("=")[-1]
            return containers[name]["Id"]
        if command[1] == "inspect":
            return json.dumps([next(item for item in containers.values() if item["Id"] == command[2])])
        if command[1] == "exec":
            return ""
        raise AssertionError(command)
    return state, environment, configuration, root, containers, calls, execute


def test_first_routing_mount_requires_recreation_then_unchanged_services_are_retained(edge):
    state, environment, configuration, root, containers, calls, execute = edge
    services = ["caddy", "cloudflared", "egress-proxy"]
    first = routing.prepare(state, environment, configuration, "green", services, execute)
    assert first["recreate"] == services
    assert not any(command[1] == "exec" for command in calls)
    routing.record(state, environment, configuration, "green", services, execute)
    second = routing.prepare(state, environment, configuration, "blue", services, execute)
    assert second["retained"] == services and second["recreate"] == []
    assert [command[1] for command in calls].count("exec") == 1
    assert (root / "upstream.caddy").read_bytes() == b"to public-api-blue:8000\n"
    # Reopening the startup import models a fresh Caddy process, independent of
    # the environment in which the previous reload command ran.
    assert "public-api-blue:8000" in Path(root / "upstream.caddy").read_text()
    assert "SNOW_UPSTREAM" not in (Path(__file__).parents[1] / "infra" / "Caddyfile").read_text()


def test_failed_reload_restores_durable_startup_source_before_rollback(edge):
    state, environment, configuration, root, containers, calls, execute = edge
    routing.prepare(state, environment, configuration, "blue", ["caddy"], execute)
    routing.record(state, environment, configuration, "blue", ["caddy"], execute)
    def broken(command):
        if command[1] == "exec":
            raise maintenance.MaintenanceError("injected uncertain reload")
        return execute(command)
    with pytest.raises(maintenance.MaintenanceError, match="previous startup route restored"):
        routing.prepare(state, environment, configuration, "green", ["caddy"], broken)
    assert (root / "upstream.caddy").read_bytes() == routing.route_payload("blue")


def test_manual_container_drift_disables_retention(edge):
    state, environment, configuration, root, containers, calls, execute = edge
    routing.prepare(state, environment, configuration, "blue", ["caddy"], execute)
    routing.record(state, environment, configuration, "blue", ["caddy"], execute)
    containers["caddy"]["HostConfig"]["Privileged"] = True
    result = routing.prepare(state, environment, configuration, "green", ["caddy"], execute)
    assert result["recreate"] == ["caddy"]


def test_inspect_mount_order_changes_preserve_recorded_services(edge):
    paths, environment, configuration, root, containers, calls, execute = edge
    services = ["caddy", "cloudflared", "egress-proxy"]
    for name, container in containers.items():
        container["Mounts"].append({
            "Type": "bind", "Source": str(configuration / name),
            "Destination": "/etc/" + name, "Mode": "ro", "RW": False,
            "Propagation": "rprivate",
        })
    original = deepcopy(containers)
    inspections = 0

    def reordered_inspect(command):
        nonlocal inspections
        payload = execute(command)
        if command[1] == "inspect":
            document = json.loads(payload)
            inspections += 1
            # Reproduce Docker returning the same Mounts in opposite order
            # between record, prepare and the next record/readback.
            if inspections % 2:
                document[0]["Mounts"].reverse()
            return json.dumps(document)
        return payload

    routing.prepare(paths, environment, configuration, "blue", services, reordered_inspect)
    routing.record(paths, environment, configuration, "blue", services, reordered_inspect)
    first_bindings = routing.saved_bindings(root)
    result = routing.prepare(paths, environment, configuration, "green", services, reordered_inspect)
    assert result["retained"] == services and result["recreate"] == []
    routing.record(paths, environment, configuration, "green", services, reordered_inspect)
    assert routing.saved_bindings(root) == first_bindings
    assert containers == original  # Fingerprinting must not reorder caller data.


@pytest.mark.parametrize("field,value", [
    ("Source", "/unexpected/config"),
    ("Destination", "/unexpected/container-path"),
    ("RW", True),
    ("Mode", "rw"),
    ("Propagation", "shared"),
    ("Type", "volume"),
    ("FutureDockerMetadata", {"security": "changed"}),
])
def test_mount_content_changes_still_require_recreation(edge, field, value):
    paths, environment, configuration, root, containers, calls, execute = edge
    services = ["cloudflared"]
    routing.prepare(paths, environment, configuration, "blue", services, execute)
    routing.record(paths, environment, configuration, "blue", services, execute)
    containers["cloudflared"]["Mounts"][0][field] = value
    result = routing.prepare(paths, environment, configuration, "green", services, execute)
    assert result["recreate"] == services and result["retained"] == []


def test_duplicate_mounts_are_preserved_in_fingerprint(edge):
    paths, environment, configuration, root, containers, calls, execute = edge
    services = ["cloudflared"]
    routing.prepare(paths, environment, configuration, "blue", services, execute)
    routing.record(paths, environment, configuration, "blue", services, execute)
    mount = containers["cloudflared"]["Mounts"][0]
    containers["cloudflared"]["Mounts"].append(deepcopy(mount))
    result = routing.prepare(paths, environment, configuration, "green", services, execute)
    assert result["recreate"] == services and result["retained"] == []


def test_network_identity_changes_still_require_recreation(edge):
    paths, environment, configuration, root, containers, calls, execute = edge
    services = ["cloudflared"]
    routing.prepare(paths, environment, configuration, "blue", services, execute)
    routing.record(paths, environment, configuration, "blue", services, execute)
    containers["cloudflared"]["NetworkSettings"]["Networks"]["app"]["NetworkID"] = "e" * 64
    result = routing.prepare(paths, environment, configuration, "green", services, execute)
    assert result["recreate"] == services and result["retained"] == []


def test_config_array_order_remains_part_of_fingerprint(edge):
    paths, environment, configuration, root, containers, calls, execute = edge
    services = ["cloudflared"]
    containers["cloudflared"]["Config"]["Env"] = ["SETTING=first", "SETTING=last"]
    routing.prepare(paths, environment, configuration, "blue", services, execute)
    routing.record(paths, environment, configuration, "blue", services, execute)
    containers["cloudflared"]["Config"]["Env"].reverse()
    result = routing.prepare(paths, environment, configuration, "green", services, execute)
    assert result["recreate"] == services and result["retained"] == []


def test_failed_new_routing_followed_by_legacy_rollback_restores_restart_source(edge):
    state, environment, configuration, root, containers, calls, execute = edge
    routing.prepare(state, environment, configuration, "blue", ["caddy"], execute)
    routing.record(state, environment, configuration, "blue", ["caddy"], execute)
    routing.prepare(state, environment, configuration, "green", ["caddy"], execute)
    # Simulate a later edge worker/recovery-binding failure and the existing
    # shell rollback selecting a pre-import immutable Caddy configuration.
    legacy = state.root / "releases" / "configurations" / ("c" * 40)
    write(legacy / "infra" / "Caddyfile", "reverse_proxy {$SNOW_UPSTREAM:public-api-blue:8000}\n")
    routing.select_legacy(state, legacy, "blue")
    assert (root / "upstream.caddy").read_bytes() == routing.route_payload("blue")


def test_same_config_bytes_in_different_immutable_release_paths_have_same_model(state):
    environment = state.root / "runtime" / "colours" / "green.compose.env"
    roots = [state.root / "releases" / "configurations" / (char * 40) for char in "de"]
    for root in roots:
        write(root / "infra" / "Caddyfile", "same content\n")
        write(root / "compose.prod.yml", "name: project-snow-public\n")
    def execute(command):
        source = Path(command[command.index("-f") + 1]).parent / "infra" / "Caddyfile"
        return json.dumps({"services": {"caddy": {"image": "pinned", "volumes": [
            {"type": "bind", "source": str(source), "target": "/etc/caddy/Caddyfile", "read_only": True}]}}, "networks": {}})
    one, two = [routing.service_models(state, environment, root, "green", execute) for root in roots]
    assert one == two
    write(roots[1] / "infra" / "Caddyfile", "changed security headers\n")
    assert one != routing.service_models(state, environment, roots[1], "green", execute)


def test_monitor_alerts_after_three_failures_and_logs_recovery_once(state):
    observed = {"public": {"severity": "critical"}}
    collector = lambda *args, **kwargs: observed
    assert monitor.monitor(state, collector=collector)["transitions"] == []
    assert monitor.monitor(state, collector=collector)["transitions"] == []
    third = monitor.monitor(state, collector=collector)
    assert third["transitions"][0]["severity"] == "critical"
    assert third["transitions"][0]["failure_streak"] == 3
    assert monitor.monitor(state, collector=collector)["transitions"] == []
    observed["public"]["severity"] = "ok"
    assert monitor.monitor(state, collector=collector)["transitions"][0]["event"] == "recovered"
    assert monitor.monitor(state, collector=collector)["transitions"] == []


def test_disk_and_backup_thresholds_are_immediate_and_have_no_notification_side_effect(state, monkeypatch):
    now = datetime.now(timezone.utc)
    write(state.root / "backups" / "last-success.json", {"schema_version": "project-snow-backup-success-1",
                                                        "completed_at": (now - timedelta(hours=27)).isoformat()})
    def unavailable(command):
        raise maintenance.MaintenanceError("unavailable")
    checks = monitor.collect(state, execute=unavailable, probe=lambda: True,
                             disk_usage=lambda path: SimpleNamespace(total=1000, free=90), now=now)
    assert checks["disk"] == {"severity": "critical", "used_percent": 91.0}
    assert checks["backup"]["severity"] == "critical"
    result = monitor.monitor(state, collector=lambda *args, **kwargs: checks, now=now)
    assert {item["check"] for item in result["transitions"]} == {"disk", "backup"}
    assert result["notification_delivery"].startswith("local journal only")


def test_unproved_stage_exits_before_git_checkout_or_candidate_script(tmp_path):
    script = (Path(__file__).parents[1] / "ops" / "project-snow-release").read_text()
    start = script.index("run_stage() {")
    end = script.index("\nrun_switch() {", start)
    body = script[start:end]
    prefix = """set -eu
requested_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
inbox=.
cleanup_paths=''
stat() { case "$*" in *%U*) echo deploy ;; *%a*) echo 700 ;; esac; }
mktemp() { echo manifest.json; }
copy_inbox_manifest() { printf '{}' > "$2"; }
verify_inbox_release_proof() { echo proof-rejected >> order.txt; return 1; }
fetch_main() { echo forbidden-git >> order.txt; }
checkout_controller() { echo forbidden-checkout >> order.txt; }
validate_release_manifest() { echo forbidden-candidate-python >> order.txt; }
"""
    target = tmp_path / "test.sh"
    target.write_bytes((prefix + body + "\nrun_stage\n").encode())
    shell = "C:/Program Files/Git/bin/bash.exe" if Path("C:/Program Files/Git/bin/bash.exe").exists() else shutil.which("sh")
    result = subprocess.run([shell, str(target)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 71
    assert (tmp_path / "order.txt").read_text().splitlines() == ["proof-rejected"]
    assert "python3 /usr/local/libexec/project-snow/verify_release_proof.py" in script
