"""Validate the single visual contract shared by local and public clients.

The immersive UI has two different controllers (the private workspace and the
registration-free public client), but their layout and scene assets must come
from one design layer.  This small, dependency-free check is deliberately
safe to run before the application environment is installed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


REQUIRED_SCENES = (
    "generic",
    "archive",
    "canteen",
    "corridor",
    "lounge",
    "medical",
    "observation",
    "quarters",
    "training",
)


def _stylesheet_reference(html: str, stylesheet: str) -> str | None:
    pattern = re.compile(
        rf'<link[^>]+href=["\']{re.escape(stylesheet)}(?:\?[^"\']*)?["\']',
        re.IGNORECASE,
    )
    match = pattern.search(html)
    return match.group(0) if match else None


def validate(app_root: Path) -> dict[str, Any]:
    app_root = app_root.resolve()
    shared_root = app_root / "frontend" / "shared"
    scenes_root = app_root / "frontend" / "assets" / "immersive" / "scenes"
    public_scenes_root = app_root / "public_frontend" / "assets" / "immersive" / "scenes"
    design_path = shared_root / "design-version.json"
    stylesheet_path = shared_root / "immersive.css"

    errors: list[str] = []
    design: dict[str, Any] = {}
    try:
        parsed = json.loads(design_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            design = parsed
        else:
            errors.append("design_version_not_object")
    except FileNotFoundError:
        errors.append("design_version_missing")
    except (OSError, json.JSONDecodeError):
        errors.append("design_version_invalid")

    stylesheet = str(design.get("canonical_stylesheet") or "")
    scene_root = str(design.get("canonical_scene_root") or "")
    design_version = str(design.get("design_version") or "")
    if stylesheet != "/shared/immersive.css":
        errors.append("canonical_stylesheet_mismatch")
    if scene_root != "/assets/immersive/scenes":
        errors.append("canonical_scene_root_mismatch")
    if not design_version:
        errors.append("design_version_missing_value")
    if not stylesheet_path.is_file():
        errors.append("shared_stylesheet_missing")

    local_html_path = app_root / "frontend" / "index.html"
    public_html_path = app_root / "public_frontend" / "index.html"
    html_checks: dict[str, bool] = {}
    for label, path in (("local", local_html_path), ("public", public_html_path)):
        try:
            html = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            errors.append(f"{label}_index_missing")
            html_checks[label] = False
            continue
        reference = _stylesheet_reference(html, stylesheet or "/shared/immersive.css")
        html_checks[label] = reference is not None
        if reference is None:
            errors.append(f"{label}_does_not_reference_shared_stylesheet")

    missing_scenes = [
        name for name in REQUIRED_SCENES if not (scenes_root / f"{name}.svg").is_file()
    ]
    if missing_scenes:
        errors.append("scenes_missing:" + ",".join(missing_scenes))

    # Public clients must consume the canonical mounted scene directory.  A
    # second copy is a drift hazard even if it happens to contain identical
    # SVGs today.
    duplicate_public_scenes = sorted(
        path.relative_to(public_scenes_root).as_posix()
        for path in public_scenes_root.rglob("*")
        if path.is_file()
    ) if public_scenes_root.is_dir() else []
    if duplicate_public_scenes:
        errors.append("duplicate_public_scene_assets")

    return {
        "status": "ok" if not errors else "invalid",
        "design_version": design_version,
        "canonical_stylesheet": stylesheet,
        "canonical_scene_root": scene_root,
        "required_scene_count": len(REQUIRED_SCENES),
        "missing_scenes": missing_scenes,
        "shared_references": html_checks,
        "duplicate_public_scene_assets": duplicate_public_scenes,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the App directory",
    )
    args = parser.parse_args()
    result = validate(args.app_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
