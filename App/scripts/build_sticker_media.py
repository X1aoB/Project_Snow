"""Build a versioned, license-gated sticker media release from the local crawl."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from typing import Any

from PIL import Image, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}\Z")


def _character_lookup() -> dict[str, str]:
    registry = json.loads(
        (PROJECT_ROOT / "App" / "backend" / "snow_app" / "mvp_character_registry.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        name: str(item["character_id"])
        for item in registry.get("characters") or []
        for name in {
            str(item.get("display_name") or ""),
            str(item.get("source_name") or ""),
            *[str(value) for value in item.get("aliases") or []],
        }
        if name
    }


def _resolve_character_ids(
    character_names: list[str],
    character_lookup: dict[str, str],
) -> list[str]:
    """Resolve Wiki costume labels such as ``凯茜娅·蓝闪`` to the base role."""

    resolved: set[str] = set()
    for raw_name in character_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        candidates = [name]
        base_name = re.split(r"[·・]", name, maxsplit=1)[0].strip()
        if base_name and base_name != name:
            candidates.append(base_name)
        for candidate in candidates:
            character_id = character_lookup.get(candidate)
            if character_id:
                resolved.add(character_id)
                break
    return sorted(resolved)


def _emotion_tags(caption: str) -> list[str]:
    value = str(caption or "")
    tags: list[str] = []
    groups = (
        ("celebration", ("庆祝", "恭喜", "好耶", "太棒", "干杯")),
        ("strong", ("大哭", "暴怒", "震惊", "救命", "狂喜")),
        ("playful", ("哈哈", "笑", "调皮", "摸鱼", "摆烂", "可爱")),
        ("emotion", ("哭", "生气", "无语", "害羞", "委屈", "尴尬", "开心")),
    )
    for tag, terms in groups:
        if any(term in value for term in terms):
            tags.append(tag)
    return tags or ["neutral"]


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(project_root: Path, value: str) -> Path:
    raw = Path(value)
    return raw if raw.is_absolute() else project_root / raw


def _thumbnail(source: Path, destination: Path) -> tuple[int, int]:
    with Image.open(source) as image:
        image.seek(0)
        image = ImageOps.contain(image.convert("RGBA"), (160, 160))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="WEBP", quality=84, method=6)
        return image.size


def build(
    *,
    data_root: Path,
    output_root: Path,
    version: str,
) -> dict[str, Any]:
    manifest_path = data_root / "Manifest" / "chat_stickers_index.jsonl"
    rows = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    release = output_root / version
    stickers_dir = release / "stickers"
    thumbnails_dir = release / "thumbnails"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    character_lookup = _character_lookup()
    for row in rows:
        asset_id = str(row.get("asset_id") or "").strip()
        source = _source_path(PROJECT_ROOT, str(row.get("local_path") or ""))
        if not ASSET_ID_PATTERN.fullmatch(asset_id) or asset_id in seen_ids:
            raise ValueError(f"invalid or duplicate sticker asset id: {asset_id}")
        seen_ids.add(asset_id)
        if not source.is_file():
            raise FileNotFoundError(f"sticker source missing: {asset_id} {source}")
        suffix = source.suffix.casefold() or ".png"
        destination = stickers_dir / f"{asset_id}{suffix}"
        shutil.copy2(source, destination)
        thumbnail = thumbnails_dir / f"{asset_id}.webp"
        width, height = _thumbnail(source, thumbnail)
        with Image.open(source) as image:
            animated = bool(getattr(image, "is_animated", False))
            source_width, source_height = image.size
        character_names = [
            str(value)
            for value in row.get("character_names") or []
            if str(value).strip()
        ]
        character_ids = _resolve_character_ids(character_names, character_lookup)
        caption = str(row.get("caption_text") or row.get("caption") or "")[:120]
        license_name = str(row.get("source_license") or "CC BY-NC-SA")
        license_version = str(row.get("license_version") or "version unspecified by source")
        license_status = str(row.get("license_status") or "pending_review")
        entries.append(
            {
                "asset_id": asset_id,
                "caption": caption,
                "section": str(row.get("section") or "未分类")[:120],
                "character_ids": character_ids,
                "emotion_tags": _emotion_tags(caption),
                "candidate_scope": "character" if character_ids else "generic",
                "path": destination.relative_to(release).as_posix(),
                "thumbnail_path": thumbnail.relative_to(release).as_posix(),
                "sha256": _hash(destination),
                "thumbnail_sha256": _hash(thumbnail),
                "mime_type": str(row.get("mime_type") or "image/png"),
                "animated": animated,
                "width": int(source_width),
                "height": int(source_height),
                "thumbnail_width": int(width),
                "thumbnail_height": int(height),
                "file_page_url": str(row.get("file_page_url") or ""),
                "source_page_url": str(row.get("source_page_url") or ""),
                "source_image_url": str(row.get("source_image_url") or ""),
                "license": license_name,
                "license_version": license_version,
                "license_status": license_status,
                "source_revision_id": str(row.get("source_revision_id") or ""),
                "attribution": str(row.get("attribution") or ""),
                "content_hash": str(row.get("content_hash") or ""),
                "release_basis": (
                    "verified_public_release"
                    if license_status == "verified" and license_version == "4.0"
                    else "private_acceptance_user_approved"
                ),
            }
        )
    entries.sort(key=lambda item: (item["section"], item["caption"], item["asset_id"]))
    animated_count = sum(1 for item in entries if item["animated"])
    if len(entries) != 363 or animated_count != 29:
        raise ValueError(
            f"sticker release must contain 363 resources and 29 GIFs; got {len(entries)} and {animated_count}"
        )
    license_review_complete = all(
        item["license_status"] == "verified"
        and item["license_version"] == "4.0"
        and "CC BY-NC-SA 4.0" in item["license"]
        for item in entries
    )
    manifest = {
        "schema_version": "project-snow-sticker-1",
        "media_version": version,
        "count": len(entries),
        "generated_at": datetime.now(UTC).isoformat(),
        "private_candidate": not license_review_complete,
        "license_review_status": (
            "verified_public_release" if license_review_complete else "pending_review"
        ),
        "license_policy": (
            "Every source and page-specific exception was reviewed for CC BY-NC-SA 4.0 public release."
            if license_review_complete
            else "Source declarations are preserved; public release requires separate review."
        ),
        "stickers": entries,
    }
    (release / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files = sorted(
        path
        for path in release.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    (release / "SHA256SUMS").write_text(
        "".join(f"{_hash(path)}  {path.relative_to(release).as_posix()}\n" for path in files),
        encoding="utf-8",
    )
    return {
        "media_version": version,
        "count": len(entries),
        "animated": animated_count,
        "release": str(release),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "Data")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "App" / "media" / "releases")
    parser.add_argument("--version", default="2026.08.18.sticker.1")
    args = parser.parse_args()
    print(json.dumps(build(data_root=args.data_root.resolve(), output_root=args.output_root.resolve(), version=args.version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
