"""Verify Wiki sticker license evidence and optionally annotate the source index.

The media bytes remain the crawler's local, hash-verified copies.  This script
only records the public license policy, source-page revisions, and the latest
revision of each file page so a release can be audited without hot-linking the
Wiki at runtime.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WIKI_API = "https://wiki.biligame.com/sonw/api.php"
LICENSE_SOURCE_PAGE = "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5"
LICENSE_SOURCE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
STICKER_SOURCE_PAGE = "https://wiki.biligame.com/sonw/%E8%81%8A%E5%A4%A9%E8%A1%A8%E6%83%85"
USER_AGENT = "ProjectSnow-public-media-review/0.9.1 (contact: admin@xiaob.dev)"
SPECIAL_NOTICE = re.compile(
    r"特殊说明|CC\s*BY|版权声明|许可证|许可协议|禁止转载|版权所有|保留所有权利",
    re.IGNORECASE,
)


def _base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    output = ""
    while number:
        number, remainder = divmod(number, 36)
        output = alphabet[remainder] + output
    return output or "0"


def _source_sha1_matches(content: bytes, declared: str) -> bool:
    digest = sha1(content).hexdigest()
    normalized = str(declared or "").casefold().strip()
    if re.fullmatch(r"[0-9a-f]{40}", normalized):
        return normalized == digest
    if re.fullmatch(r"[0-9a-z]{1,31}", normalized):
        return normalized.lstrip("0") == _base36(int(digest, 16)).lstrip("0")
    return False


def _title(url: str) -> str:
    path = urlsplit(url).path
    return unquote(path[path.rfind("/") + 1 :])


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _query(session: httpx.Client, titles: list[str]) -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for start in range(0, len(titles), 20):
        batch = titles[start : start + 20]
        response = session.post(
            WIKI_API,
            data={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": "|".join(batch),
                "prop": "revisions|imageinfo",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "iiprop": "url|sha1|timestamp|user|size|mime",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        for page in payload.get("query", {}).get("pages", []):
            revisions = page.get("revisions") or []
            revision = revisions[0] if revisions else {}
            pages[str(page.get("title") or "")] = {
                "missing": bool(page.get("missing")),
                "revid": str(revision.get("revid") or ""),
                "timestamp": str(revision.get("timestamp") or ""),
                "content": str(
                    ((revision.get("slots") or {}).get("main") or {}).get("content") or ""
                ),
                "imageinfo": (page.get("imageinfo") or [{}])[0],
            }
        time.sleep(0.25)
    return pages


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def review(*, project_root: Path, apply: bool) -> dict[str, Any]:
    index_path = project_root / "Data" / "Manifest" / "chat_stickers_index.jsonl"
    rows = _read_rows(index_path)
    titles = [_title(str(row["file_page_url"])) for row in rows]
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60.0),
        follow_redirects=False,
    ) as session:
        pages = _query(session, titles)
        source_pages = _query(session, [_title(LICENSE_SOURCE_PAGE), _title(STICKER_SOURCE_PAGE)])
        home = source_pages.get(_title(LICENSE_SOURCE_PAGE)) or {}
        gallery = source_pages.get(_title(STICKER_SOURCE_PAGE)) or {}
        home_html_response = session.get(
            LICENSE_SOURCE_PAGE,
            params={"license-review": "project-snow-0.9.1"},
            timeout=30,
        )
        home_html_response.raise_for_status()
        home_html = home_html_response.text

    errors: list[str] = []
    if home.get("missing") or not home.get("revid") or LICENSE_SOURCE_URL not in home_html:
        errors.append("Wiki homepage does not expose the expected CC BY-NC-SA 4.0 policy.")
    if "特殊说明" not in home_html:
        errors.append("Wiki homepage policy is missing its special-notice qualifier.")
    if gallery.get("missing") or not gallery.get("revid"):
        errors.append("Chat-sticker source page is missing a revision.")

    evidence: list[dict[str, str]] = []
    annotated: list[dict[str, Any]] = []
    for row, title in zip(rows, titles, strict=True):
        page = pages.get(title) or {}
        asset_id = str(row.get("asset_id") or "")
        if page.get("missing") or not page.get("revid"):
            errors.append(f"{asset_id}: file page is missing a revision")
            continue
        content = str(page.get("content") or "")
        image_info = page.get("imageinfo") if isinstance(page.get("imageinfo"), dict) else {}
        if SPECIAL_NOTICE.search(content):
            errors.append(f"{asset_id}: file page contains a license or special-notice marker")
            continue
        local_relative = Path(str(row.get("local_path") or "").replace("\\", "/"))
        local_path = (project_root / local_relative).resolve()
        try:
            local_path.relative_to(project_root.resolve())
        except ValueError:
            errors.append(f"{asset_id}: unsafe local source path")
            continue
        if not local_path.is_file():
            errors.append(f"{asset_id}: local source file is missing")
            continue
        local_content = local_path.read_bytes()
        source_sha1 = str(image_info.get("sha1") or "")
        if not _source_sha1_matches(local_content, source_sha1):
            errors.append(f"{asset_id}: local bytes do not match MediaWiki SHA1")
            continue
        if sha256(local_content).hexdigest() != str(row.get("content_hash") or "").casefold():
            errors.append(f"{asset_id}: local bytes do not match recorded SHA256")
            continue
        record = dict(row)
        record.update(
            {
                "license_version": "4.0",
                "license_status": "verified",
                "license_source_page": LICENSE_SOURCE_PAGE,
                "license_source_url": LICENSE_SOURCE_URL,
                "license_source_revision_id": str(home.get("revid") or ""),
                "license_verification_note": (
                    f"首页修订 {home.get('revid')} 声明默认 CC BY-NC-SA 4.0；"
                    f"文件页修订 {page.get('revid')} 未列出特殊说明。"
                ),
                "source_revision_id": str(page.get("revid") or ""),
                "source_revision_timestamp": str(page.get("timestamp") or ""),
                "source_uploader": str(image_info.get("user") or ""),
                "source_sha1": source_sha1,
                "source_image_url": str(image_info.get("url") or row.get("source_image_url") or ""),
                "source_width": int(image_info.get("width") or 0),
                "source_height": int(image_info.get("height") or 0),
                "source_mime_type": str(image_info.get("mime") or ""),
                "source_page_revision_id": str(gallery.get("revid") or ""),
                "source_page_revision_timestamp": str(gallery.get("timestamp") or ""),
            }
        )
        annotated.append(record)
        evidence.append(
            {
                "asset_id": asset_id,
                "file_page_url": str(row.get("file_page_url") or ""),
                "source_revision_id": str(page.get("revid") or ""),
                "source_revision_timestamp": str(page.get("timestamp") or ""),
                "source_uploader": str(image_info.get("user") or ""),
                "source_sha1": source_sha1,
            }
        )

    if len(annotated) != len(rows):
        errors.append(f"Only {len(annotated)} of {len(rows)} file pages passed review.")
    report = {
        "schema_version": "project-snow-sticker-license-review-1",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "asset_count": len(rows),
        "passed_count": len(annotated),
        "status": "verified_public_release" if not errors else "blocked",
        "license": "CC BY-NC-SA 4.0",
        "license_version": "4.0",
        "license_source_page": LICENSE_SOURCE_PAGE,
        "license_source_url": LICENSE_SOURCE_URL,
        "license_source_revision_id": str(home.get("revid") or ""),
        "license_policy_note": "首页声明：若无特殊说明，wiki 内容按 CC BY-NC-SA 协议提供。",
        "sticker_source_page": STICKER_SOURCE_PAGE,
        "sticker_source_page_revision_id": str(gallery.get("revid") or ""),
        "sticker_source_page_revision_timestamp": str(gallery.get("timestamp") or ""),
        "errors": errors,
        "files": evidence,
    }
    if errors:
        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))
    if apply:
        _atomic_write(
            index_path,
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in annotated),
        )
        _atomic_write(
            project_root / "App" / "config" / "public_media" / "sticker_license_review.json",
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--apply", action="store_true", help="write verified evidence to the source index")
    args = parser.parse_args()
    result = review(project_root=args.project_root.resolve(), apply=args.apply)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
