"""Bind manual promotion to a reviewed, expiring candidate and current release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from maintenance import (
    SHA,
    MaintenanceError,
    Paths,
    active_release,
    read_text,
    release_lock,
    require_inherited_release_lock,
)
from release_state import atomic_write, pending_candidate, sync

RECEIPT_SCHEMA = "project-snow-candidate-acceptance-1"
APPROVAL_SCHEMA = "project-snow-candidate-approval-1"
CHECKS = ("api_health", "build_identity", "data_media", "browser_smoke", "rollback_baseline")
HASH = re.compile(r"^[0-9a-f]{64}$")


def run(command: list[str], *, timeout: float = 20) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MaintenanceError("Candidate runtime probe failed or exceeded its deadline") from error
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise MaintenanceError("Candidate runtime probe failed or returned oversized metadata")
    return result.stdout


class DeadlineCommands:
    def __init__(self):
        self.deadline = time.monotonic() + 90

    def __call__(self, command: list[str]) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MaintenanceError("Candidate acceptance exceeded its total runtime probe deadline")
        return run(command, timeout=min(20, remaining))


def encoded(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_payload(path: Path) -> bytes:
    read_text(path)  # Reuse ownership, regular-file and size validation.
    payload = path.read_bytes()
    if len(payload) > 1024 * 1024:
        raise MaintenanceError("Acceptance metadata exceeds its size limit")
    return payload


def directory(path: Path, *, create: bool = False) -> None:
    for parent in reversed((path, *path.parents)):
        if create and not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o700)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
            raise MaintenanceError("Acceptance storage must be root-controlled")


def container_identity(colour: str, image: str, execute: Callable = run) -> dict:
    identifiers = execute(
        [
            "docker",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=project-snow-public",
            "--filter",
            f"label=com.docker.compose.service=public-api-{colour}",
        ]
    ).split()
    if len(identifiers) != 1 or not HASH.fullmatch(identifiers[0]):
        raise MaintenanceError("Acceptance requires exactly one running API for each colour")
    template = (
        '{"id":{{json .Id}},"image_id":{{json .Image}},'
        '"image":{{json .Config.Image}},"running":{{json .State.Running}}}'
    )
    actual = json.loads(execute(["docker", "inspect", "--format", template, identifiers[0]]))
    expected_id = execute(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    if (
        actual.get("id") != identifiers[0]
        or actual.get("image") != image
        or actual.get("running") is not True
        or actual.get("image_id") != expected_id
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_id)
    ):
        raise MaintenanceError("Acceptance API image differs from its immutable release")
    return actual


def snapshot(paths: Paths, colour: str, commit: str, expected_current: str, execute: Callable = run) -> dict:
    if colour not in {"blue", "green"} or not SHA.fullmatch(commit) or not SHA.fullmatch(expected_current):
        raise MaintenanceError("Acceptance requires exact candidate and current commit identities")
    directory(paths.root)
    active = active_release(paths)
    pending = pending_candidate(paths)
    if (
        active["colour"] == colour
        or active["commit_sha"] != expected_current
        or not pending
        or pending["colour"] != colour
        or pending["commit_sha"] != commit
        or pending["base_commit_sha"] != expected_current
    ):
        raise MaintenanceError("Candidate reservation or expected current release changed")
    relative_paths = (
        "releases/active-colour",
        "releases/current",
        "releases/current-manifest.json",
        "releases/current-config.json",
        "runtime/compose.env",
        "releases/pending-candidate.json",
        f"releases/colours/{colour}",
        f"releases/colours/{colour}-manifest.json",
        f"releases/colours/{colour}-config.json",
        f"releases/colours/{colour}-stage-receipt",
        f"runtime/colours/{colour}.compose.env",
    )
    payloads = {}
    for name in relative_paths:
        path = paths.root / name
        directory(path.parent)
        payloads[name] = read_payload(path)
    marker = payloads[f"releases/colours/{colour}"].decode().split()
    manifest_bytes = payloads[f"releases/colours/{colour}-manifest.json"]
    manifest = json.loads(manifest_bytes)
    application = manifest.get("application", {})
    public_image = f"{application.get('image', '')}@{application.get('digest', '')}"
    embedding_image = (
        f"{manifest.get('embedding', {}).get('image', '')}@{manifest.get('embedding', {}).get('digest', '')}"
    )
    stage = payloads[f"releases/colours/{colour}-stage-receipt"].decode().split()
    if (
        marker != [colour, commit, public_image, embedding_image]
        or manifest.get("commit_sha") != commit
        or sha256(manifest_bytes) != pending["manifest_sha256"]
        or len(stage) != 6
        or stage[0] != "project-snow-stage-receipt-1"
        or not HASH.fullmatch(stage[1])
        or stage[2:] != marker
    ):
        raise MaintenanceError("Candidate manifest, stage receipt and reservation disagree")
    candidate = container_identity(colour, public_image, execute)
    probe = (
        "import json, urllib.request; "
        "r=urllib.request.urlopen('http://127.0.0.1:8000/public/v1/build-info', timeout=10); "
        "print(json.dumps(json.load(r)))"
    )
    build = json.loads(execute(["docker", "exec", candidate["id"], "python", "-c", probe]))
    if build.get("revision") != commit or build.get("app_version") != manifest.get("app_version"):
        raise MaintenanceError("Running candidate build identity differs from the signed release")
    return {
        "files": {name: sha256(payload) for name, payload in payloads.items()},
        "candidate_container": candidate,
        "current_container": container_identity(active["colour"], active["image"], execute),
    }


def load_receipt(paths: Paths, identifier: str) -> dict:
    if not HASH.fullmatch(identifier):
        raise MaintenanceError("An exact acceptance receipt SHA256 is required")
    root = paths.root / "releases" / "acceptance"
    directory(root)
    payload = read_payload(root / (identifier + ".json"))
    if sha256(payload) != identifier:
        raise MaintenanceError("Acceptance receipt was modified")
    document = json.loads(payload)
    if document.get("schema_version") != RECEIPT_SCHEMA:
        raise MaintenanceError("Unknown acceptance receipt schema")
    return document


def validate_receipt(
    paths: Paths, receipt: dict, expected_current: str, *, now: datetime, execute: Callable = run
) -> None:
    probe_started = time.monotonic()
    issued = datetime.fromisoformat(receipt["prepared_at"])
    expires = datetime.fromisoformat(receipt["expires_at"])
    if (
        issued.tzinfo is None
        or expires.tzinfo is None
        or not issued <= now < expires
        or not timedelta(seconds=60) <= expires - issued <= timedelta(hours=24)
        or receipt.get("expected_current") != expected_current
    ):
        raise MaintenanceError("Acceptance receipt expired or targets a different current release")
    if receipt["snapshot"] != snapshot(
        paths, receipt["colour"], receipt["commit_sha"], expected_current, execute
    ):
        raise MaintenanceError("Candidate or current runtime changed after acceptance checks")
    if now + timedelta(seconds=max(0, time.monotonic() - probe_started)) >= expires:
        raise MaintenanceError("Acceptance receipt expired while checking the running candidate")


def prepare(
    paths: Paths,
    colour: str,
    commit: str,
    expected_current: str,
    evidence: dict,
    *,
    now: datetime,
    ttl_seconds: int = 86400,
    execute: Callable = run,
) -> dict:
    if not 60 <= ttl_seconds <= 86400:
        raise MaintenanceError("Acceptance lifetime must be between one minute and 24 hours")
    if (
        evidence.get("schema_version") != "project-snow-candidate-evidence-1"
        or evidence.get("commit_sha") != commit
        or evidence.get("expected_current") != expected_current
        or any((evidence.get("checks") or {}).get(name) is not True for name in CHECKS)
    ):
        raise MaintenanceError("Candidate evidence must identify this release and pass every required check")
    document = {
        "schema_version": RECEIPT_SCHEMA,
        "colour": colour,
        "commit_sha": commit,
        "expected_current": expected_current,
        "prepared_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "snapshot": snapshot(paths, colour, commit, expected_current, execute),
        "evidence": evidence,
    }
    payload = encoded(document)
    if len(payload) > 128 * 1024:
        raise MaintenanceError("Acceptance evidence exceeds the bounded receipt size")
    identifier = sha256(payload)
    root = paths.root / "releases" / "acceptance"
    directory(root, create=True)
    target = root / (identifier + ".json")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    sync(root)
    return {
        "status": "ready_for_review",
        "receipt_id": identifier,
        "commit_sha": commit,
        "expected_current": expected_current,
        "expires_at": document["expires_at"],
    }


def approve(
    paths: Paths, identifier: str, expected_current: str, *, now: datetime, execute: Callable = run
) -> dict:
    receipt = load_receipt(paths, identifier)
    validate_receipt(paths, receipt, expected_current, now=now, execute=execute)
    # This explicit root operation records the operator's decision. Preparation
    # and automatic stage never invoke it; there is no automatic approval flag.
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "receipt_id": identifier,
        "commit_sha": receipt["commit_sha"],
        "colour": receipt["colour"],
        "expected_current": expected_current,
        "approved_at": now.isoformat(),
    }
    root = paths.root / "releases" / "acceptance"
    atomic_write(root / f"{receipt['colour']}-{receipt['commit_sha']}.approval.json", encoded(approval))
    return {"status": "approved", **approval}


def verify(paths: Paths, colour: str, commit: str, *, now: datetime, execute: Callable = run) -> dict:
    if colour not in {"blue", "green"} or not SHA.fullmatch(commit):
        raise MaintenanceError("Invalid promotion candidate identity")
    root = paths.root / "releases" / "acceptance"
    directory(root)
    approval = json.loads(read_text(root / f"{colour}-{commit}.approval.json"))
    if (
        approval.get("schema_version") != APPROVAL_SCHEMA
        or approval.get("colour") != colour
        or approval.get("commit_sha") != commit
    ):
        raise MaintenanceError("No matching manual candidate approval exists")
    receipt = load_receipt(paths, approval["receipt_id"])
    if receipt.get("colour") != colour or receipt.get("commit_sha") != commit:
        raise MaintenanceError("Approval and acceptance receipt identify different candidates")
    validate_receipt(paths, receipt, approval["expected_current"], now=now, execute=execute)
    approved = datetime.fromisoformat(approval["approved_at"])
    if approved.tzinfo is None or not datetime.fromisoformat(receipt["prepared_at"]) <= approved <= now:
        raise MaintenanceError("Invalid manual approval time")
    return {
        "status": "verified",
        "receipt_id": approval["receipt_id"],
        "commit_sha": commit,
        "expected_current": approval["expected_current"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    prepare_parser.add_argument("--sha", required=True)
    prepare_parser.add_argument("--expected-current", required=True)
    prepare_parser.add_argument("--evidence", type=Path, required=True)
    prepare_parser.add_argument("--ttl-seconds", type=int, default=86400)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--receipt-id", required=True)
    approve_parser.add_argument("--expected-current", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--colour", choices=("blue", "green"), required=True)
    verify_parser.add_argument("--sha", required=True)
    verify_parser.add_argument("--lock-held", action="store_true")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise MaintenanceError("Candidate acceptance requires root")
        paths = Paths()
        execute = DeadlineCommands()

        def perform():
            now = datetime.now(UTC)
            if args.operation == "prepare":
                directory(args.evidence.parent)
                evidence = json.loads(read_text(args.evidence))
                return prepare(
                    paths,
                    args.colour,
                    args.sha,
                    args.expected_current,
                    evidence,
                    now=now,
                    ttl_seconds=args.ttl_seconds,
                    execute=execute,
                )
            if args.operation == "approve":
                return approve(paths, args.receipt_id, args.expected_current, now=now, execute=execute)
            return verify(paths, args.colour, args.sha, now=now, execute=execute)

        if getattr(args, "lock_held", False):
            require_inherited_release_lock(paths.lock)
            result = perform()
        else:
            with release_lock(paths.lock):
                result = perform()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MaintenanceError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
        print(json.dumps({"status": "error", "error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
