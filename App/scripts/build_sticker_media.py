"""Build a versioned private sticker media release from the local crawl."""

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
        entries.append(
            {
                "asset_id": asset_id,
                "caption": str(row.get("caption_text") or row.get("caption") or "")[:120],
                "section": str(row.get("section") or "未分类")[:120],
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
                "license": str(row.get("source_license") or "CC BY-NC-SA"),
                "license_version": "version unspecified by source",
                "attribution": str(row.get("attribution") or ""),
                "content_hash": str(row.get("content_hash") or ""),
                "release_basis": "private_acceptance_user_approved",
            }
        )
    entries.sort(key=lambda item: (item["section"], item["caption"], item["asset_id"]))
    animated_count = sum(1 for item in entries if item["animated"])
    if len(entries) != 363 or animated_count != 29:
        raise ValueError(
            f"private candidate sticker release must contain 363 resources and 29 GIFs; got {len(entries)} and {animated_count}"
        )
    manifest = {
        "schema_version": "project-snow-sticker-1",
        "media_version": version,
        "count": len(entries),
        "generated_at": datetime.now(UTC).isoformat(),
        "private_candidate": True,
        "license_policy": "Source declarations are preserved; public release requires separate review.",
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
    parser.add_argument("--version", default="2026.08.16.sticker.1")
    args = parser.parse_args()
    print(json.dumps(build(data_root=args.data_root.resolve(), output_root=args.output_root.resolve(), version=args.version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
