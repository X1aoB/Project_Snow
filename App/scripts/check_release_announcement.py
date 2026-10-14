"""Check effective announcement changes between two exact committed releases.

No working-tree feed, network, model or deployment operation is used. Output
contains public identifiers, commit/feed hashes and timestamps, never notice text.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


FEED_PATH = "App/public_frontend/announcements.json"
MAX_FEED_BYTES = 1024 * 1024
COMMIT = re.compile(r"[0-9a-f]{40}")
NOTICE_ID = re.compile(r"[A-Za-z0-9._-]{1,96}")
ERRORS = {
    "arguments_invalid": "Provide exact base and candidate commit SHAs.",
    "git_unavailable": "Git could not read the committed announcement feed.",
    "commit_unavailable": "The requested revision is not an available commit.",
    "feed_unavailable": "Both commits must contain an announcement feed.",
    "feed_bounds": "The committed announcement feed exceeds its size bound.",
    "feed_invalid": "The announcement feed has invalid JSON or structure.",
    "duplicate_id": "Announcement identifiers must be unique.",
    "text_invalid": "Each announcement needs nonempty plain-text title, summary and items.",
    "date_invalid": "Announcement dates must include a valid timezone.",
    "history_deleted": "Previously published announcement identifiers must be retained.",
    "published_changed": "Existing announcement publication times must remain unchanged.",
    "revision_missing": "Changed announcement content requires an updated revision time.",
    "revision_not_effective": "Content revisions must advance the previous revision time and be effective now.",
    "update_missing": "The candidate needs an effective new announcement or substantive content revision.",
    "clock_invalid": "Announcement checking requires a timezone-aware current time.",
    "report_write_failed": "The announcement check report could not be created.",
}


class AnnouncementCheckError(ValueError):
    def __init__(self, code: str, metadata: dict[str, Any] | None = None):
        self.code = code if code in ERRORS else "feed_invalid"
        self.metadata = metadata or {}
        super().__init__(ERRORS[self.code])


def require(condition: object, code: str) -> None:
    if not condition:
        raise AnnouncementCheckError(code)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_date(value: object) -> datetime:
    require(isinstance(value, str) and bool(value), "date_invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise AnnouncementCheckError("date_invalid") from None
    require(result.tzinfo is not None and result.utcoffset() is not None, "date_invalid")
    return result


def plain_text(value: object) -> str:
    require(isinstance(value, str) and bool(value.strip()) and re.search(r"<[^>]+>", value) is None, "text_invalid")
    # Formatting-only edits must not create an effective release announcement.
    return "".join(value.split())


def parse_feed(payload: bytes) -> dict[str, dict[str, Any]]:
    require(0 < len(payload) <= MAX_FEED_BYTES, "feed_bounds")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "feed_invalid")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise AnnouncementCheckError("feed_invalid")

    try:
        feed = json.loads(payload.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise AnnouncementCheckError("feed_invalid") from None
    require(isinstance(feed, dict) and type(feed.get("schema_version")) is int and feed["schema_version"] == 1
            and feed.get("timezone") == "Asia/Shanghai" and isinstance(feed.get("updates"), list), "feed_invalid")
    result = {}
    for item in feed["updates"]:
        require(isinstance(item, dict) and isinstance(item.get("id"), str) and NOTICE_ID.fullmatch(item["id"]), "feed_invalid")
        identifier = item["id"]
        require(identifier not in result, "duplicate_id")
        require(isinstance(item.get("items"), list) and bool(item["items"]), "text_invalid")
        content = (plain_text(item.get("title")), plain_text(item.get("summary")),
                   tuple(sorted(plain_text(line) for line in item["items"])))
        published = parse_date(item.get("published_at"))
        revised = parse_date(item["updated_at"]) if "updated_at" in item else None
        require(revised is None or published <= revised, "date_invalid")
        result[identifier] = {"published_raw": item["published_at"], "published": published,
                              "revised": revised, "content": content}
    return result


def check_feeds(base: bytes, candidate: bytes, *, now: datetime) -> dict[str, list[str]]:
    require(now.tzinfo is not None and now.utcoffset() is not None, "clock_invalid")
    previous, current = parse_feed(base), parse_feed(candidate)
    require(set(previous) <= set(current), "history_deleted")
    new_ids, revised_ids = [], []
    for identifier, item in current.items():
        old = previous.get(identifier)
        if old is None:
            if item["published"] <= now and (item["revised"] is None or item["revised"] <= now):
                new_ids.append(identifier)
            continue
        require(item["published_raw"] == old["published_raw"], "published_changed")
        if old["revised"] is not None:
            require(item["revised"] is not None and old["revised"] <= item["revised"], "revision_not_effective")
        if item["content"] == old["content"]:
            continue
        require(item["revised"] is not None, "revision_missing")
        require((old["revised"] or old["published"]) < item["revised"] <= now
                and item["published"] <= now, "revision_not_effective")
        revised_ids.append(identifier)
    require(bool(new_ids or revised_ids), "update_missing")
    return {"effective_new_ids": sorted(new_ids), "effective_revision_ids": sorted(revised_ids)}


def git_read(repository: Path, arguments: list[str], failure: str) -> bytes:
    try:
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
        result = subprocess.run(["git", "--no-replace-objects", "--no-pager", "-c", "core.fsmonitor=false", *arguments],
                                cwd=repository, env=environment, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AnnouncementCheckError("git_unavailable") from None
    require(result.returncode == 0, failure)
    return result.stdout


def committed_feed(repository: Path, commit: str) -> bytes:
    require(isinstance(commit, str) and COMMIT.fullmatch(commit), "arguments_invalid")
    require(git_read(repository, ["cat-file", "-t", commit], "commit_unavailable").strip() == b"commit", "commit_unavailable")
    source = commit + ":" + FEED_PATH
    raw_size = git_read(repository, ["cat-file", "-s", source], "feed_unavailable").strip()
    require(re.fullmatch(rb"[0-9]{1,10}", raw_size), "feed_bounds")
    size = int(raw_size)
    require(0 < size <= MAX_FEED_BYTES, "feed_bounds")
    payload = git_read(repository, ["show", "--no-ext-diff", "--no-textconv", source], "feed_unavailable")
    require(len(payload) == size, "feed_bounds")
    return payload


def check_committed(repository: Path, base_sha: str, candidate_sha: str, *, now: datetime | None = None) -> dict[str, Any]:
    require(COMMIT.fullmatch(base_sha) and COMMIT.fullmatch(candidate_sha), "arguments_invalid")
    now = now or datetime.now(timezone.utc)
    metadata = {"base_sha": base_sha, "candidate_sha": candidate_sha, "checked_at": now.isoformat()}
    try:
        base = committed_feed(repository, base_sha)
        metadata["base_feed_sha256"] = digest(base)
        candidate = committed_feed(repository, candidate_sha)
        metadata["candidate_feed_sha256"] = digest(candidate)
        effective = check_feeds(base, candidate, now=now)
    except AnnouncementCheckError as error:
        raise AnnouncementCheckError(error.code, metadata) from None
    return {"passed": True, **metadata, **effective}


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise AnnouncementCheckError("arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", type=Path)
    arguments = None
    try:
        arguments = parser.parse_args(argv)
        report = check_committed(Path.cwd(), arguments.base_sha, arguments.candidate_sha)
    except AnnouncementCheckError as error:
        report = {"passed": False, **error.metadata, "error_code": error.code, "error": ERRORS[error.code]}
    if arguments is not None and arguments.output is not None:
        try:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            with arguments.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
        except OSError:
            report = {"passed": False, "error_code": "report_write_failed", "error": ERRORS["report_write_failed"]}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
