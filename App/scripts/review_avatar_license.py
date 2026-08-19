"""Audit and download the exact BWiki portrait files used by a public release.

The command fails closed: every registry character and the analyst must resolve
to a Wiki File page with a fixed revision, uploader, original URL/hash and no
page-specific license exception before source metadata is written.
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
from urllib.parse import quote, unquote, urlsplit

import httpx


APP_ROOT = Path(__file__).resolve().parents[1]
WIKI_API = "https://wiki.biligame.com/sonw/api.php"
WIKI_HOME = "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
EXPECTED_HOME_REVISION = "21546"
USER_AGENT = "ProjectSnow-public-avatar-review/0.9.1 (contact: admin@xiaob.dev)"
SPECIAL_NOTICE = re.compile(
    r"特殊说明|版权声明|许可证|许可协议|禁止转载|版权所有|保留所有权利",
    re.IGNORECASE,
)
SOURCE_TITLE_OVERRIDES = {
    # The 0.8.x seed pointed to the character article, not a File page.  This
    # exact title was reviewed explicitly for the 0.9.0 public package.
    "447ed3c401c9": "文件:胧嫣·瑞狐抽卡立绘.png",
}


def _title(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]).replace("%3A", ":")


def _base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    output = ""
    while number:
        number, remainder = divmod(number, 36)
        output = alphabet[remainder] + output
    return output


def _source_sha1_matches(content: bytes, declared: str) -> bool:
    digest = sha1(content).hexdigest()
    normalized = str(declared or "").casefold().strip()
    if re.fullmatch(r"[0-9a-f]{40}", normalized):
        return normalized == digest
    if re.fullmatch(r"[0-9a-z]{1,31}", normalized):
        return normalized.lstrip("0") == _base36(int(digest, 16)).lstrip("0")
    return False


def _query(session: httpx.Client, title: str) -> dict[str, Any]:
    payload = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": title,
            "prop": "revisions|imageinfo",
            "rvprop": "ids|timestamp|user|content",
            "rvslots": "main",
            "iiprop": "url|sha1|timestamp|user|size|mime",
        }
    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            response = session.post(WIKI_API, data=payload, timeout=30)
            response.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    if response is None:  # pragma: no cover - defensive only
        raise RuntimeError("Wiki query produced no response")
    pages = response.json().get("query", {}).get("pages", [])
    return pages[0] if pages else {"missing": True, "title": title}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _download(session: httpx.Client, url: str) -> bytes:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    content = response.content
    if not content or not str(response.headers.get("Content-Type") or "").startswith("image/"):
        raise RuntimeError(f"source is not an image: {url}")
    return content


def _review_one(
    session: httpx.Client,
    *,
    identity: str,
    display_name: str,
    seed: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    file_page_url = str(seed.get("file_page_url") or seed.get("source_page") or "")
    title = _title(file_page_url)
    if not title.startswith("文件:"):
        title = SOURCE_TITLE_OVERRIDES.get(identity, "")
    if not title.startswith("文件:"):
        raise RuntimeError(f"{identity}: seed does not identify a reviewed Wiki File page")
    page = _query(session, title)
    if page.get("missing"):
        raise RuntimeError(f"{identity}: Wiki file page does not exist: {title}")
    revision = (page.get("revisions") or [{}])[0]
    image_info = (page.get("imageinfo") or [{}])[0]
    content = str(((revision.get("slots") or {}).get("main") or {}).get("content") or "")
    if SPECIAL_NOTICE.search(content):
        raise RuntimeError(f"{identity}: file page contains a license exception marker")
    source_url = str(image_info.get("url") or "")
    source_bytes = _download(session, source_url)
    source_sha1 = str(image_info.get("sha1") or "")
    if not _source_sha1_matches(source_bytes, source_sha1):
        raise RuntimeError(f"{identity}: downloaded bytes do not match MediaWiki SHA1")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source_bytes)
    canonical_title = str(page.get("title") or title)
    canonical_page = "https://wiki.biligame.com/sonw/" + quote(canonical_title, safe=":")
    return {
        **seed,
        "publishable": True,
        "file_page_url": canonical_page,
        "source_image_url": source_url,
        "source_revision_id": str(revision.get("revid") or ""),
        "source_revision_timestamp": str(revision.get("timestamp") or ""),
        "source_uploader": str(image_info.get("user") or revision.get("user") or ""),
        "source_sha1": source_sha1,
        "original_sha1": sha1(source_bytes).hexdigest(),
        "original_sha256": sha256(source_bytes).hexdigest(),
        "source_width": int(image_info.get("width") or 0),
        "source_height": int(image_info.get("height") or 0),
        "source_mime_type": str(image_info.get("mime") or ""),
        "license": "CC BY-NC-SA 4.0",
        "license_version": "4.0",
        "license_status": "verified_site_policy_no_page_exception",
        "license_source_page": WIKI_HOME,
        "license_source_url": LICENSE_URL,
        "license_source_revision_id": EXPECTED_HOME_REVISION,
        "license_verification_note": (
            f"BWiki 首页修订 {EXPECTED_HOME_REVISION} 适用站点级 CC BY-NC-SA 4.0；"
            f"文件页修订 {revision.get('revid')} 未发现特别例外。"
        ),
        "reviewed_at": datetime.now(UTC).isoformat(),
    }


def review(*, seed_manifest: Path, source_root: Path, analyst_root: Path) -> dict[str, Any]:
    seed = json.loads(seed_manifest.read_text(encoding="utf-8"))
    seed_by_id = {
        str(item.get("character_id") or ""): item
        for item in seed.get("characters") or []
        if isinstance(item, dict)
    }
    registry = json.loads(
        (APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json").read_text(
            encoding="utf-8"
        )
    )
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60.0), follow_redirects=False
    ) as session:
        home = _query(session, "首页")
        home_revision = str(((home.get("revisions") or [{}])[0]).get("revid") or "")
        if home_revision != EXPECTED_HOME_REVISION:
            raise RuntimeError(
                f"BWiki homepage revision changed ({home_revision}); repeat the license review"
            )
        rows: list[dict[str, Any]] = []
        for character in registry.get("characters") or []:
            character_id = str(character.get("character_id") or "")
            row = _review_one(
                session,
                identity=character_id,
                display_name=str(character.get("display_name") or ""),
                seed=dict(seed_by_id.get(character_id) or {}),
                destination=source_root / f"{character_id}.png",
            )
            row["character_id"] = character_id
            rows.append(row)
        analyst_seed = dict(seed.get("analyst") or {})
        analyst = _review_one(
            session,
            identity="analyst-default",
            display_name="分析员",
            seed=analyst_seed,
            destination=analyst_root / "analyst-default.png",
        )
    analyst["asset_id"] = "analyst-default"
    _atomic_json(source_root / "avatars.json", {"characters": rows})
    _atomic_json(analyst_root / "analyst.json", analyst)
    return {"status": "verified", "characters": len(rows), "analyst": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument(
        "--source-root", type=Path, default=APP_ROOT / "frontend" / "assets" / "characters"
    )
    parser.add_argument(
        "--analyst-root", type=Path, default=APP_ROOT / "frontend" / "assets" / "analyst"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            review(
                seed_manifest=args.seed_manifest.resolve(),
                source_root=args.source_root.resolve(),
                analyst_root=args.analyst_root.resolve(),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
