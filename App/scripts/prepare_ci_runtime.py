"""Create minimal generated artifacts required by tests in a clean CI checkout.

The real runtime corpus and Wiki-derived avatar manifest intentionally stay out
of Git.  This helper creates contract-only fixtures from the checked-in
character registry; it does not contain source text, embeddings, graph data or
redistributable character artwork.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json"


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = json.loads(path.read_text(encoding="utf-8"))
    characters = registry.get("characters") or []
    character_ids = [str(item.get("character_id") or "") for item in characters]
    if len(characters) != 22 or any(not item for item in character_ids):
        raise ValueError("CI fixtures require the checked-in 22-character registry")
    if len(set(character_ids)) != len(character_ids):
        raise ValueError("character_id values must be unique")
    return registry


def write_character_views(registry: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for character in registry["characters"]:
        rows.append(
            {
                "character_id": character["character_id"],
                "character_name": character["display_name"],
                "display_name": character["display_name"],
                "aliases": list(character.get("aliases") or []),
                "selector_enabled": bool(character.get("selector_enabled", True)),
                "coverage": {
                    "level": "limited",
                    "direct_document_count": 0,
                    "linked_document_count": 0,
                    "global_context_document_count": 0,
                },
                "retrieval_document_ids": [],
                "document_origins": {},
                "provisional_relations": [],
            }
        )
    destination.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return destination


def write_avatar_manifest(registry: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    characters = []
    for index, character in enumerate(registry["characters"]):
        characters.append(
            {
                "character_id": character["character_id"],
                "character_name": character["display_name"],
                "local_path": None,
                "source_page": None,
                "source_url": None,
                "source_alt": None,
                "status": "fallback",
                "license": "CC BY-NC-SA",
                "license_version": "version unspecified by source",
                "license_source_page": None,
                "page_specific_exception": False,
                "publishable": False,
                "stage_src": None,
                "stage_src_deprecated": True,
                "stage_focus_x": 50,
                "stage_focus_y": 50,
                "stage_fit": "contain",
                "portrait_kind": "full_body" if index == 0 else "headshot",
                "portrait_scale": 1.8 if index == 0 else 1.0,
                "portrait_focus_x": 50,
                "portrait_focus_y": 22 if index == 0 else 50,
            }
        )
    manifest = {
        "schema_version": "project-snow-avatar-1.2",
        "registry_version": registry.get("version"),
        "policy": "CI contract fixture; contains no character artwork.",
        "characters": characters,
    }
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def prepare(
    *,
    app_root: Path = APP_ROOT,
    registry_path: Path | None = None,
    views: bool = True,
    avatars: bool = True,
) -> dict[str, str]:
    registry = load_registry(
        registry_path
        or app_root / "backend" / "snow_app" / "mvp_character_registry.json"
    )
    outputs: dict[str, str] = {}
    if views:
        path = write_character_views(
            registry,
            app_root / "runtime" / "mvp" / "character_views.jsonl",
        )
        outputs["character_views"] = str(path)
    if avatars:
        path = write_avatar_manifest(
            registry,
            app_root / "frontend" / "assets" / "characters" / "avatars.json",
        )
        outputs["avatar_manifest"] = str(path)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--views",
        action="store_true",
        help="Create only character views",
    )
    selection.add_argument(
        "--avatars",
        action="store_true",
        help="Create only the avatar manifest",
    )
    args = parser.parse_args()
    outputs = prepare(
        views=not args.avatars,
        avatars=not args.views,
    )
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
