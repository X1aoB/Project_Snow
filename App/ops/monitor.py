"""Read-only probes and local structured state-transition alerts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
import subprocess
from typing import Callable
import urllib.request

from maintenance import MaintenanceError, Paths, active_release, read_text
from release_state import atomic_write
from routing import live_container

PUBLIC_URL = "https://snow.xiaob.dev/public/v1/health/live"
INTERNAL_PROBE = """import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/full', timeout=17) as response:
    value = json.loads(response.read(131073))
print(json.dumps({key: value.get(key) for key in ('database', 'dependencies', 'generation_queue', 'draining')}))
"""


def bounded_run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=22)
    if result.returncode or len(result.stdout) > 131072:
        raise MaintenanceError("Internal health probe failed or exceeded its metadata limit")
    return result.stdout


def public_probe() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(PUBLIC_URL, headers={"User-Agent": "project-snow-health/1", "Cache-Control": "no-cache"})
    with opener.open(request, timeout=8) as response:
        payload = response.read(65537)
        return response.url == PUBLIC_URL and len(payload) <= 65536 and json.loads(payload).get("status") == "ok"


def collect(paths: Paths, *, execute: Callable = bounded_run, probe: Callable = public_probe,
            disk_usage: Callable = shutil.disk_usage, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks = {}
    try:
        checks["public"] = {"severity": "ok" if probe() else "critical"}
    except Exception:
        checks["public"] = {"severity": "critical"}
    try:
        active = active_release(paths)
        container = live_container("public-api-" + active["colour"], execute)
        if not container:
            raise MaintenanceError("Active API is absent")
        expected = json.loads(execute(["docker", "image", "inspect", active["image"]]))
        if len(expected) != 1 or container.get("Image") != expected[0].get("Id"):
            raise MaintenanceError("Active API image drift")
        health = json.loads(execute(["docker", "exec", container["Id"], "python", "-c", INTERNAL_PROBE]))
        checks["database"] = {"severity": "ok" if health.get("database") == "ok" else "critical"}
        dependencies = health.get("dependencies") or {}
        checks["retrieval"] = {"severity": "ok" if all(dependencies.get(name) == "ok" for name in ("embedding", "qdrant", "neo4j")) else "warning"}
        queue = health.get("generation_queue") or {}
        fields = ("active", "queued", "active_limit", "queue_limit")
        valid = all(type(queue.get(field)) is int and queue[field] >= 0 for field in fields)
        if not valid or queue.get("active_limit", 0) < 1 or queue.get("queue_limit", 0) < 1:
            checks["queue"] = {"severity": "warning", "reason": "instrumentation-unavailable"}
        else:
            saturated = queue["active"] >= queue["active_limit"] and queue["queued"] >= queue["queue_limit"]
            checks["queue"] = {"severity": "warning" if saturated else "ok", **{key: queue[key] for key in fields}}
        checks["draining"] = {"severity": "warning" if health.get("draining") else "ok"}
    except Exception:
        for name in ("database", "retrieval", "queue", "draining"):
            checks[name] = {"severity": "critical", "reason": "internal-probe-unavailable"}
    try:
        usages = (disk_usage(paths.root), disk_usage(paths.docker_storage))
        used = max(100 * (usage.total - usage.free) / usage.total for usage in usages)
        checks["disk"] = {"severity": "critical" if used >= 90 else "warning" if used >= 80 else "ok", "used_percent": round(used, 1)}
    except Exception:
        checks["disk"] = {"severity": "critical", "reason": "capacity-unavailable"}
    try:
        success = json.loads(read_text(paths.root / "backups" / "last-success.json"))
        if success.get("schema_version") != "project-snow-backup-success-1":
            raise ValueError("Unsupported backup record")
        age = (now - datetime.fromisoformat(success["completed_at"])).total_seconds()
        checks["backup"] = {"severity": "ok" if 0 <= age <= 26 * 3600 else "critical", "age_seconds": int(age)}
    except Exception:
        checks["backup"] = {"severity": "critical", "reason": "successful-backup-unrecorded"}
    return checks


def monitor(paths: Paths, *, collector: Callable = collect, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    source = paths.root / "runtime" / "monitor-state.json"
    previous = json.loads(read_text(source)) if source.exists() or source.is_symlink() else {}
    if previous and previous.get("schema_version") != "project-snow-monitor-1":
        raise MaintenanceError("Unrecognized previous monitor state")
    states, transitions = {}, []
    for name, observed in collector(paths, now=now).items():
        old = (previous.get("checks") or {}).get(name) or {}
        desired = observed["severity"]
        streak = old.get("failure_streak", 0) + 1 if desired != "ok" else 0
        threshold = 1 if name in {"disk", "backup"} else 3
        effective = desired if streak >= threshold or desired == "ok" else old.get("severity", "ok")
        states[name] = {**observed, "observed_severity": desired, "severity": effective, "failure_streak": streak}
        if effective != old.get("severity", "ok"):
            transitions.append({"check": name, "previous_state": old.get("severity", "ok"), "state": effective,
                                "severity": "info" if effective == "ok" else effective,
                                "event": "recovered" if effective == "ok" else "alert", "failure_streak": streak})
    record = {"schema_version": "project-snow-monitor-1", "checked_at": now.isoformat(), "checks": states}
    atomic_write(source, (json.dumps(record, sort_keys=True) + "\n").encode())
    return {"status": "changed" if transitions else "unchanged", "checked_at": now.isoformat(), "transitions": transitions,
            "notification_delivery": "local journal only; no external notification channel configured"}
