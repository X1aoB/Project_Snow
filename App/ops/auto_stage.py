"""Pull signed metadata for the newest main commit; never promote traffic."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from maintenance import MaintenanceError, Paths, SHA, active_release, read_text, release_lock, run
from release_state import atomic_write, pending_candidate

REPOSITORY = "X1aoB/Project_Snow"
MAIN_URL = f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main"
VERIFIER = "/usr/local/libexec/project-snow/verify_release_proof.py"
RUNNER = "/usr/local/sbin/project-snow-release"
MAX_JSON_BYTES = 128 * 1024
ALLOWED_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise MaintenanceError("Candidate metadata redirected outside the fixed GitHub HTTPS endpoints")


class FixedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_json_bytes(url: str, *, optional: bool = False) -> bytes | None:
    validate_url(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "project-snow-candidate-pull/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FixedRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            validate_url(response.url)
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_JSON_BYTES):
                raise MaintenanceError("Candidate metadata exceeds the download limit")
            payload = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return None
        raise MaintenanceError(f"Candidate metadata HTTP request failed ({error.code})") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise MaintenanceError("Candidate metadata endpoint is unavailable") from error
    if not 2 <= len(payload) <= MAX_JSON_BYTES or not isinstance(json.loads(payload), dict):
        raise MaintenanceError("Candidate metadata is not a bounded JSON object")
    return payload


def publish_inbox(paths: Paths, name: str, payload: bytes) -> None:
    import pwd

    if not name.startswith("release-") or "/" in name or "\\" in name:
        raise MaintenanceError("Invalid candidate inbox name")
    account = pwd.getpwnam("deploy")
    descriptor = os.open(paths.root / "inbox", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".auto-stage-" + secrets.token_hex(16)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise MaintenanceError("Candidate inbox must belong to deploy with mode 0700")
        file_descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), account.pw_uid, account.pw_gid)
        os.rename(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def auto_stage(paths: Paths, *, retry: bool = False, fetch: Callable = fetch_json_bytes,
               execute: Callable = run, publish: Callable = publish_inbox) -> dict:
    document = json.loads(fetch(MAIN_URL))
    commit = str((document.get("object") or {}).get("sha", ""))
    if document.get("ref") != "refs/heads/main" or not SHA.fullmatch(commit):
        raise MaintenanceError("GitHub returned an invalid main ref")
    # Do not hold the release lock while invoking the runner, which acquires it
    # itself. candidate-record rechecks the reservation under that same lock.
    with release_lock(paths.lock):
        active = active_release(paths)
        pending = pending_candidate(paths)
        if commit == active["commit_sha"]:
            return {"status": "current", "commit_sha": commit}
        if pending and pending["commit_sha"] != active["commit_sha"]:
            if pending["commit_sha"] != commit or not retry:
                return {"status": "awaiting-review", "commit_sha": pending["commit_sha"], "latest_main_sha": commit}
        state_path = paths.root / "releases" / "auto-stage.json"
        if state_path.exists() or state_path.is_symlink():
            previous = json.loads(read_text(state_path))
            if previous.get("commit_sha") == commit and previous.get("status") in {"failed", "attempting"} and not retry:
                return {"status": "retry-required", "commit_sha": commit}
    base = f"https://github.com/{REPOSITORY}/releases/download/candidate-{commit}"
    manifest = fetch(base + "/release-manifest.json", optional=True)
    proof = fetch(base + "/release-proof.json", optional=True)
    if manifest is None or proof is None:
        return {"status": "awaiting-signed-candidate", "commit_sha": commit}
    with tempfile.TemporaryDirectory(prefix="project-snow-candidate-", dir="/run") as temporary:
        manifest_path, proof_path = Path(temporary) / "manifest.json", Path(temporary) / "proof.json"
        atomic_write(manifest_path, manifest)
        atomic_write(proof_path, proof)
        # Only this independently installed verifier is trusted. A candidate's
        # checkout cannot replace the process that authorizes it.
        execute(["python3", VERIFIER, "--manifest", str(manifest_path), "--proof", str(proof_path), "--expected-sha", commit])
    colour = "green" if active["colour"] == "blue" else "blue"
    record = {"schema_version": "project-snow-auto-stage-1", "commit_sha": commit, "colour": colour,
              "attempted_at": datetime.now(timezone.utc).isoformat(), "status": "attempting"}
    latest = json.loads(fetch(MAIN_URL))
    if latest.get("ref") != "refs/heads/main" or (latest.get("object") or {}).get("sha") != commit:
        return {"status": "main-changed", "commit_sha": commit}
    with release_lock(paths.lock):
        # Final pre-publication read protects an operator's newly reserved
        # candidate; the runner will also reject a racing change after this.
        now = active_release(paths)
        pending = pending_candidate(paths)
        if now != active or (pending and pending["commit_sha"] not in {commit, now["commit_sha"]}):
            return {"status": "state-changed", "commit_sha": commit}
        atomic_write(state_path, (json.dumps(record, sort_keys=True) + "\n").encode())
        publish(paths, f"release-{commit}.proof.json", proof)
        publish(paths, f"release-{commit}.json", manifest)
    try:
        # The runner accepts no data/media URL. Missing exact local artifacts
        # fail closed, leaving current traffic and release markers untouched.
        execute([RUNNER, "stage", colour, commit])
    except Exception:
        record["status"] = "failed"
        atomic_write(state_path, (json.dumps(record, sort_keys=True) + "\n").encode())
        raise MaintenanceError("Candidate stage failed; inspect the release journal and explicitly retry after resolving it") from None
    record["status"] = "staged"
    atomic_write(state_path, (json.dumps(record, sort_keys=True) + "\n").encode())
    return {**record, "promotion": "manual review required"}
