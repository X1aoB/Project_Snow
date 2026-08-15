"""Build the separately deployed Project Snow public avatar media package."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


APP_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json"
DEFAULT_SOURCE_ROOT = APP_ROOT / "frontend" / "assets" / "characters"
DEFAULT_OUTPUT_ROOT = APP_ROOT / "media" / "releases"
DEFAULT_VERSION = "2026.08.15.avatar.1"


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _webp(source: Path, size: int, focus_x: int, focus_y: int) -> bytes:
    with Image.open(source) as image:
        image.seek(0)
        normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        fitted = ImageOps.fit(
            normalized,
            (size, size),
            method=Image.Resampling.LANCZOS,
            centering=(max(0, min(100, focus_x)) / 100, max(0, min(100, focus_y)) / 100),
        )
        output = BytesIO()
        fitted.save(output, format="WEBP", quality=88, method=6, exact=True)
        return output.getvalue()


def build_release(
    *,
    source_root: Path,
    output_root: Path,
    version: str,
) -> Path:
    source_manifest_path = source_root / "avatars.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_by_id = {
        str(item.get("character_id") or ""): item
        for item in source_manifest.get("characters") or []
        if isinstance(item, dict) and item.get("character_id")
    }
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    characters = list(registry.get("characters") or [])
    if len(characters) != 22:
        raise ValueError(f"expected 22 registry characters, found {len(characters)}")

    release_root = output_root / version
    if release_root.exists():
        raise FileExistsError(
            f"release already exists; choose an empty output root: {release_root}"
        )
    avatar_root = release_root / "avatars"
    avatar_root.mkdir(parents=True)

    manifest_rows: list[dict[str, Any]] = []
    for character in characters:
        character_id = str(character["character_id"])
        source = source_by_id.get(character_id)
        if not source:
            raise ValueError(f"avatar metadata missing for {character_id}")
        source_path = source_root / f"{character_id}.png"
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if not bool(source.get("publishable")):
            raise ValueError(f"avatar is not marked publishable: {character_id}")

        original_content = source_path.read_bytes()
        focus_x = int(source.get("portrait_focus_x") or 50)
        focus_y = int(source.get("portrait_focus_y") or 50)
        thumbnail_content = _webp(source_path, 96, focus_x, focus_y)
        stage_content = _webp(source_path, 200, focus_x, focus_y)
        thumbnail_name = f"{character_id}-96.webp"
        stage_name = f"{character_id}-200.webp"
        (avatar_root / thumbnail_name).write_bytes(thumbnail_content)
        (avatar_root / stage_name).write_bytes(stage_content)
        with Image.open(source_path) as image:
            source_width, source_height = image.size

        manifest_rows.append(
            {
                "character_id": character_id,
                "character_name": str(character["display_name"]),
                "original_sha256": _digest(original_content),
                "original_width": source_width,
                "original_height": source_height,
                "thumbnail_path": f"avatars/{thumbnail_name}",
                "thumbnail_sha256": _digest(thumbnail_content),
                "thumbnail_width": 96,
                "thumbnail_height": 96,
                "stage_path": f"avatars/{stage_name}",
                "stage_sha256": _digest(stage_content),
                "stage_width": 200,
                "stage_height": 200,
                "crop_mode": "square_focus",
                "portrait_kind": str(source.get("portrait_kind") or "headshot"),
                "portrait_scale": float(source.get("portrait_scale") or 1.0),
                "portrait_focus_x": focus_x,
                "portrait_focus_y": focus_y,
                "source_page": str(source.get("source_page") or ""),
                "source_revision_id": source.get("source_revision_id") or "unknown",
                "source_author": source.get("source_author") or "unknown",
                "license": "CC BY-NC-SA",
                "license_version": "version unspecified by source",
                "release_basis": "private_acceptance_user_approved",
            }
        )

    manifest = {
        "schema_version": "project-snow-avatar-media-1",
        "media_version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "character_count": len(manifest_rows),
        "release_basis": "private_acceptance_user_approved",
        "public_release_review_required": True,
        "characters": manifest_rows,
    }
    manifest_path = release_root / "manifest.json"
    _write_json(manifest_path, manifest)

    checksum_paths = [manifest_path, *sorted(avatar_root.glob("*.webp"))]
    checksum_lines = [
        f"{_digest(path.read_bytes())}  {path.relative_to(release_root).as_posix()}"
        for path in checksum_paths
    ]
    (release_root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )
    return release_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args()
    release_root = build_release(
        source_root=args.source_root.resolve(),
        output_root=args.output_root.resolve(),
        version=str(args.version),
    )
    print(
        json.dumps(
            {"status": "ok", "media_version": args.version, "path": str(release_root)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
