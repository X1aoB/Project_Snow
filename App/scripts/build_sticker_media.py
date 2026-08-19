"""Build a versioned, license-gated sticker media release from the local crawl."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import shutil
from typing import Any

from PIL import Image, ImageOps, ImageSequence


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


def _display(source: Path, destination: Path) -> tuple[int, int, bool]:
    """Create a bounded display derivative and strip source metadata."""
    with Image.open(source) as image:
        animated = bool(getattr(image, "is_animated", False))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not animated:
            frame = ImageOps.contain(image.convert("RGBA"), (320, 320))
            frame.save(destination, format="WEBP", quality=84, method=6, exact=True)
            return frame.width, frame.height, False

        frames: list[Image.Image] = []
        durations: list[int] = []
        for frame in ImageSequence.Iterator(image):
            frames.append(ImageOps.contain(frame.convert("RGBA"), (320, 320)))
            durations.append(max(20, int(frame.info.get("duration") or image.info.get("duration") or 100)))
        if not frames:
            raise ValueError(f"animated sticker has no frames: {source}")
        frames[0].save(
            destination,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=int(image.info.get("loop") or 0),
            quality=80,
            method=6,
        )
        return frames[0].width, frames[0].height, True


def _render_derivatives(
    source: Path,
    thumbnail: Path,
    display: Path,
) -> tuple[int, int, int, int, bool]:
    width, height = _thumbnail(source, thumbnail)
    display_width, display_height, display_animated = _display(source, display)
    return width, height, display_width, display_height, display_animated


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
    displays_dir = release / "display"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    displays_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    character_lookup = _character_lookup()
    prepared: list[tuple[dict[str, Any], str, Path, Path, Path, Path, Any]] = []
    # Pillow's WebP encoder releases the GIL.  Rendering independent immutable
    # sources concurrently keeps a full 363-item build practical while the
    # manifest remains deterministic because results are collected in row order.
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="sticker-webp") as pool:
        for row in rows:
            asset_id = str(row.get("asset_id") or "").strip()
            source = _source_path(PROJECT_ROOT, str(row.get("local_path") or ""))
            if not ASSET_ID_PATTERN.fullmatch(asset_id) or asset_id in seen_ids:
                raise ValueError(f"invalid or duplicate sticker asset id: {asset_id}")
            seen_ids.add(asset_id)
            if not source.is_file():
                raise FileNotFoundError(f"sticker source missing: {asset_id} {source}")
            source_content = source.read_bytes()
            if not _source_sha1_matches(source_content, str(row.get("source_sha1") or "")):
                raise ValueError(f"sticker source SHA1 mismatch: {asset_id}")
            if sha256(source_content).hexdigest() != str(row.get("content_hash") or "").casefold():
                raise ValueError(f"sticker source SHA256 mismatch: {asset_id}")
            suffix = source.suffix.casefold() or ".png"
            destination = stickers_dir / f"{asset_id}{suffix}"
            shutil.copy2(source, destination)
            thumbnail = thumbnails_dir / f"{asset_id}.webp"
            display = displays_dir / f"{asset_id}.webp"
            future = pool.submit(_render_derivatives, source, thumbnail, display)
            prepared.append((row, asset_id, source, destination, thumbnail, display, future))

        for row, asset_id, source, destination, thumbnail, display, future in prepared:
            width, height, display_width, display_height, display_animated = future.result()
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
            provenance_complete = all(
                (
                    str(row.get("source_revision_id") or "").isdigit(),
                    bool(str(row.get("source_revision_timestamp") or "").strip()),
                    str(row.get("source_uploader") or "").casefold()
                    not in {"", "unknown", "未知"},
                    bool(str(row.get("source_sha1") or "").strip()),
                    str(row.get("license_source_revision_id") or "").isdigit(),
                )
            )
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
                    "display_path": display.relative_to(release).as_posix(),
                    "sha256": _hash(destination),
                    "thumbnail_sha256": _hash(thumbnail),
                    "display_sha256": _hash(display),
                    "mime_type": str(row.get("mime_type") or "image/png"),
                    "animated": animated,
                    "display_mime_type": "image/webp",
                    "display_animated": display_animated,
                    "width": int(source_width),
                    "height": int(source_height),
                    "thumbnail_width": int(width),
                    "thumbnail_height": int(height),
                    "display_width": int(display_width),
                    "display_height": int(display_height),
                    "file_page_url": str(row.get("file_page_url") or ""),
                    "source_page_url": str(row.get("source_page_url") or ""),
                    "source_image_url": str(row.get("source_image_url") or ""),
                    "license": license_name,
                    "license_version": license_version,
                    "license_status": license_status,
                    "source_revision_id": str(row.get("source_revision_id") or ""),
                    "source_revision_timestamp": str(
                        row.get("source_revision_timestamp") or ""
                    ),
                    "source_uploader": str(row.get("source_uploader") or ""),
                    "source_sha1": str(row.get("source_sha1") or ""),
                    "license_source_page": str(row.get("license_source_page") or ""),
                    "license_source_url": str(row.get("license_source_url") or ""),
                    "license_source_revision_id": str(
                        row.get("license_source_revision_id") or ""
                    ),
                    "attribution": str(row.get("attribution") or ""),
                    "content_hash": str(row.get("content_hash") or ""),
                    "transformations": [
                        "160px bounding-box WebP thumbnail, metadata removed",
                        "320px bounding-box WebP display derivative, metadata removed",
                    ],
                    "release_basis": (
                        "verified_public_release"
                        if license_status == "verified"
                        and license_version == "4.0"
                        and provenance_complete
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
        and item["release_basis"] == "verified_public_release"
        for item in entries
    )
    manifest = {
        "schema_version": "project-snow-sticker-2",
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
    parser.add_argument("--version", default="2026.08.19.sticker.1")
    args = parser.parse_args()
    print(json.dumps(build(data_root=args.data_root.resolve(), output_root=args.output_root.resolve(), version=args.version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
