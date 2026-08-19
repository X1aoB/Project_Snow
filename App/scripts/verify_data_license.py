"""Build a fixed-revision licence review for every public knowledge source."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import httpx


API_URL = "https://wiki.biligame.com/sonw/api.php"
SITE_POLICY_URL = (
    "https://wiki.biligame.com/sonw/index.php?title=%E9%A6%96%E9%A1%B5&oldid=21546"
)
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
USER_AGENT = "ProjectSnow-data-license-review/0.9.0 (contact: admin@xiaob.dev)"


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    marker = "/sonw/"
    if parsed.netloc != "wiki.biligame.com" or marker not in parsed.path:
        raise RuntimeError(f"unsupported public knowledge source URL: {url}")
    title = unquote(parsed.path.split(marker, 1)[1]).replace("_", " ").strip()
    if not title:
        raise RuntimeError(f"empty public knowledge source title: {url}")
    return title


EXCEPTION_PATTERN = re.compile(
    r"(?im)^(?:\s*\{\{\s*(?:版权|许可|license|授权)|\s*(?:版权声明|许可协议|转载授权)\s*[：:])"
)


def _documents(path: Path) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            url = str(row.get("canonical_url") or "").strip()
            if not url:
                raise RuntimeError(f"document {line_number} lacks canonical_url")
            source = sources.setdefault(
                url,
                {"title": _title_from_url(url), "local_paths": set()},
            )
            local_path = str(row.get("local_path") or "").strip()
            if local_path:
                source["local_paths"].add(local_path)
    return sources


def _resolve_batch(client: httpx.Client, titles: list[str]) -> dict[str, dict[str, Any]]:
    response = client.post(
        API_URL,
        data={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|sha1",
            "titles": "|".join(titles),
        },
    )
    response.raise_for_status()
    payload = response.json()
    query = payload.get("query") if isinstance(payload, dict) else None
    if not isinstance(query, dict):
        raise RuntimeError("BWiki query returned no query object")

    aliases = {title: title for title in titles}
    for item in query.get("normalized") or []:
        aliases[str(item.get("from") or "")] = str(item.get("to") or "")
    for item in query.get("redirects") or []:
        source = str(item.get("from") or "")
        target = str(item.get("to") or "")
        for original, current in list(aliases.items()):
            if current == source:
                aliases[original] = target

    pages = {
        str(page.get("title") or ""): page
        for page in query.get("pages") or []
        if isinstance(page, dict)
    }
    result: dict[str, dict[str, Any]] = {}
    for title in titles:
        page = pages.get(aliases.get(title, title))
        revisions = page.get("revisions") if isinstance(page, dict) else None
        revision = revisions[0] if isinstance(revisions, list) and revisions else None
        if not isinstance(page, dict) or page.get("missing") is True or not isinstance(revision, dict):
            raise RuntimeError(f"BWiki source page or revision is missing: {title}")
        result[title] = {
            "page_id": str(page.get("pageid") or ""),
            "resolved_title": str(page.get("title") or ""),
            "source_revision_id": str(revision.get("revid") or ""),
            "source_revision_timestamp": str(revision.get("timestamp") or ""),
            "latest_editor": str(revision.get("user") or "source page history"),
            "source_revision_sha1": str(revision.get("sha1") or ""),
        }
    return result


def _local_source_evidence(source_root: Path, paths: set[str]) -> dict[str, Any]:
    if not paths:
        raise RuntimeError("public knowledge source has no local source path")
    digests: list[dict[str, str]] = []
    exception_detected = False
    for relative in sorted(paths):
        path = (source_root / relative).resolve()
        try:
            path.relative_to(source_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"source path escapes source root: {relative}") from exc
        if not path.is_file():
            raise RuntimeError(f"missing local source file: {relative}")
        content = path.read_text(encoding="utf-8")
        exception_detected = exception_detected or bool(EXCEPTION_PATTERN.search(content[:8192]))
        digests.append(
            {
                "local_path": relative.replace("\\", "/"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "local_sources": digests,
        "exception_status": (
            "manual_review_required" if exception_detected else "no_page_exception_detected"
        ),
    }


def build_review(
    documents: Path,
    output: Path,
    *,
    source_root: Path,
    batch_size: int = 40,
) -> dict[str, Any]:
    source_records = _documents(documents)
    ordered = sorted(source_records.items())
    resolved: dict[str, dict[str, Any]] = {}
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30,
        follow_redirects=False,
    ) as client:
        for offset in range(0, len(ordered), batch_size):
            batch = ordered[offset : offset + batch_size]
            batch_result: dict[str, dict[str, Any]] | None = None
            for attempt in range(3):
                try:
                    batch_result = _resolve_batch(
                        client,
                        [str(record["title"]) for _url, record in batch],
                    )
                    break
                except (httpx.HTTPError, RuntimeError):
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
            assert batch_result is not None
            for url, source in batch:
                title = str(source["title"])
                fixed = batch_result[title]
                local_evidence = _local_source_evidence(
                    source_root,
                    set(source["local_paths"]),
                )
                if local_evidence["exception_status"] != "no_page_exception_detected":
                    raise RuntimeError(f"source requires manual licence review: {url}")
                resolved[url] = {
                    "canonical_url": url,
                    **fixed,
                    "fixed_revision_url": f"{url}?oldid={fixed['source_revision_id']}",
                    "contributors_url": f"{url}?action=history",
                    **local_evidence,
                }

    payload = {
        "schema_version": "project-snow-data-license-review-1",
        "review_status": "verified_against_bwiki_source_declaration",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "site_policy_url": SITE_POLICY_URL,
        "site_policy_revision_id": "21546",
        "license": "CC BY-NC-SA 4.0",
        "license_url": LICENSE_URL,
        "modifications": [
            "normalized Wiki markup",
            "cleaned and segmented text",
            "generated retrieval metadata",
            "derived summaries, personas, graph records and embeddings",
        ],
        "source_count": len(resolved),
        "sources": [resolved[url] for url in sorted(resolved)],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=40)
    args = parser.parse_args()
    payload = build_review(
        args.documents,
        args.output,
        source_root=args.source_root,
        batch_size=max(1, min(50, args.batch_size)),
    )
    print(json.dumps({"status": "ok", "source_count": payload["source_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
