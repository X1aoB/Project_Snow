"""Build the separately deployed Project Snow public avatar media package."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

APP_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json"
DEFAULT_SOURCE_ROOT = APP_ROOT / "frontend" / "assets" / "characters"
DEFAULT_ANALYST_SOURCE_ROOT = APP_ROOT / "frontend" / "assets" / "analyst"
DEFAULT_OUTPUT_ROOT = APP_ROOT / "media" / "releases"
DEFAULT_VERSION = "2026.08.17.avatar.2"


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


def _load_verified_analyst_source(source_root: Path) -> tuple[dict[str, Any], Path]:
    metadata_path = source_root / "analyst.json"
    source_path = source_root / "analyst-default.png"
    if not metadata_path.is_file() or not source_path.is_file():
        raise FileNotFoundError(
            f"analyst source is incomplete: {metadata_path} and {source_path}"
        )
    source = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not bool(source.get("publishable")):
        raise ValueError("analyst avatar is not marked publishable")
    license_status = str(source.get("license_status") or "").casefold()
    if license_status not in {
        "verified",
        "verified_explicit",
        "verified_site_policy_no_page_exception",
    }:
        raise ValueError("analyst avatar license review is incomplete")
    if str(source.get("license") or "").strip() != "CC BY-NC-SA 4.0":
        raise ValueError("analyst avatar license must be recorded explicitly")
    if str(source.get("license_version") or "").strip() != "4.0":
        raise ValueError("analyst avatar license version is missing")
    if not str(source.get("license_source_page") or "").startswith("https://wiki.biligame.com/"):
        raise ValueError("analyst avatar license source page is missing")
    if str(source.get("license_source_url") or "") != "https://creativecommons.org/licenses/by-nc-sa/4.0/":
        raise ValueError("analyst avatar license source URL is missing")
    if not str(source.get("license_source_revision_id") or "").strip():
        raise ValueError("analyst avatar license source revision is missing")
    return source, source_path


def build_release(
    *,
    source_root: Path,
    analyst_source_root: Path = DEFAULT_ANALYST_SOURCE_ROOT,
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
    analyst_source, analyst_source_path = _load_verified_analyst_source(
        analyst_source_root
    )

    release_root = output_root / version
    if release_root.exists():
        raise FileExistsError(
            f"release already exists; choose an empty output root: {release_root}"
        )
    avatar_root = release_root / "avatars"
    avatar_root.mkdir(parents=True)
    analyst_release_root = release_root / "analyst"
    analyst_release_root.mkdir(parents=True)

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

    license_status = str(analyst_source.get("license_status") or "").casefold()
    analyst_original = analyst_source_path.read_bytes()
    analyst_focus_x = int(analyst_source.get("portrait_focus_x") or 50)
    analyst_focus_y = int(analyst_source.get("portrait_focus_y") or 50)
    analyst_thumbnail = _webp(analyst_source_path, 96, analyst_focus_x, analyst_focus_y)
    analyst_stage = _webp(analyst_source_path, 200, analyst_focus_x, analyst_focus_y)
    analyst_thumbnail_name = "analyst-default-96.webp"
    analyst_stage_name = "analyst-default-200.webp"
    (analyst_release_root / analyst_thumbnail_name).write_bytes(analyst_thumbnail)
    (analyst_release_root / analyst_stage_name).write_bytes(analyst_stage)
    with Image.open(analyst_source_path) as image:
        analyst_width, analyst_height = image.size
    analyst_row = {
        "asset_id": str(analyst_source.get("asset_id") or "analyst-default"),
        "display_name": str(analyst_source.get("display_name") or "分析员（默认头像）"),
        "original_sha256": _digest(analyst_original),
        "original_width": analyst_width,
        "original_height": analyst_height,
        "thumbnail_path": f"analyst/{analyst_thumbnail_name}",
        "thumbnail_sha256": _digest(analyst_thumbnail),
        "thumbnail_width": 96,
        "thumbnail_height": 96,
        "stage_path": f"analyst/{analyst_stage_name}",
        "stage_sha256": _digest(analyst_stage),
        "stage_width": 200,
        "stage_height": 200,
        "crop_mode": "square_focus",
        "portrait_kind": str(analyst_source.get("portrait_kind") or "headshot"),
        "portrait_scale": float(analyst_source.get("portrait_scale") or 1.0),
        "portrait_focus_x": analyst_focus_x,
        "portrait_focus_y": analyst_focus_y,
        "source_page": str(analyst_source.get("source_page") or ""),
        "source_url": str(analyst_source.get("source_url") or ""),
        "source_revision_id": analyst_source.get("source_revision_id") or "unknown",
        "source_fetched_at": analyst_source.get("source_fetched_at") or "",
        "license": str(analyst_source.get("license") or ""),
        "license_version": str(analyst_source.get("license_version") or ""),
        "license_status": license_status,
        "license_source_page": str(analyst_source.get("license_source_page") or ""),
        "license_source_url": str(analyst_source.get("license_source_url") or ""),
        "license_source_revision_id": str(analyst_source.get("license_source_revision_id") or ""),
        "license_verification_note": str(analyst_source.get("license_verification_note") or ""),
        "release_basis": str(analyst_source.get("release_basis") or "private_acceptance_user_approved"),
        "public_release_review_required": bool(analyst_source.get("public_release_review_required", True)),
    }

    manifest = {
        "schema_version": "project-snow-avatar-media-2",
        "media_version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "character_count": len(manifest_rows),
        "release_basis": "private_acceptance_user_approved",
        "public_release_review_required": True,
        "characters": manifest_rows,
        "analyst": analyst_row,
    }
    manifest_path = release_root / "manifest.json"
    _write_json(manifest_path, manifest)

    checksum_paths = [
        manifest_path,
        *sorted(avatar_root.glob("*.webp")),
        *sorted(analyst_release_root.glob("*.webp")),
    ]
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
    parser.add_argument("--analyst-source-root", type=Path, default=DEFAULT_ANALYST_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args()
    release_root = build_release(
        source_root=args.source_root.resolve(),
        analyst_source_root=args.analyst_source_root.resolve(),
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
