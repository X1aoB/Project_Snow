"""Fingerprint public frontend assets inside the production image.

The checked-in HTML keeps stable, version-query URLs so it remains convenient
to run directly during development.  The production Docker build runs this
script after copying the frontend and shared design files.  It creates
content-addressed copies and rewrites only the copied HTML, allowing the edge
to cache those immutable assets for a year while HTML remains ``no-store``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


FINGERPRINT_LENGTH = 16


@dataclass(frozen=True)
class Asset:
    source: str
    public_url: str


ASSETS = (
    Asset("public_frontend/app.js", "/app.js"),
    Asset("public_frontend/app.css", "/app.css"),
    Asset("public_frontend/privacy/privacy.js", "/privacy/privacy.js"),
    Asset("frontend/shared/immersive.css", "/shared/immersive.css"),
)

HTML_DOCUMENTS = (
    "public_frontend/index.html",
    "public_frontend/privacy/index.html",
)

SCENE_ASSIGNMENT = re.compile(
    r'(?m)^(?P<indent>[ \t]*)\$\("scene-backdrop"\)\.src\s*=\s*'
    r'`/assets/immersive/scenes/\$\{visualKey\}\.svg`;[ \t]*$'
)
SCENE_MAP_DECLARATION = re.compile(
    r"(?m)^const SCENE_ASSET_URLS = Object\.freeze\(.+\);[ \t]*$"
)
SCENE_KEYS_DECLARATION = re.compile(
    r"(?m)^(const SCENE_KEYS\s*=\s*new Set\([^\n]+\);\r?\n)"
)
EXISTING_FINGERPRINT = re.compile(r"\.[0-9a-f]{12,64}$")


def _fingerprinted_path(source: Path, digest: str) -> Path:
    return source.with_name(f"{source.stem}.{digest[:FINGERPRINT_LENGTH]}{source.suffix}")


def _replace_reference(document: str, old_url: str, new_url: str) -> tuple[str, int]:
    # Version queries are a development-only cache key.  Restrict matching to
    # an HTML attribute value so unrelated text or JavaScript is untouched.
    pattern = re.compile(
        rf"(?P<quote>[\"']){re.escape(old_url)}(?:\?v=[^\"']+)?(?P=quote)"
    )
    return pattern.subn(lambda match: f"{match.group('quote')}{new_url}{match.group('quote')}", document)


def _fingerprint_scenes(app_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    scene_root = app_root / "frontend" / "assets" / "immersive" / "scenes"
    scene_urls: dict[str, str] = {}
    scene_files: dict[str, str] = {}
    sources = sorted(
        path
        for path in scene_root.glob("*.svg")
        if not EXISTING_FINGERPRINT.search(path.stem)
    )
    if not sources:
        raise ValueError("no canonical immersive scene SVG files found")
    for source in sources:
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        destination = _fingerprinted_path(source, digest)
        destination.write_bytes(payload)
        url = f"/assets/immersive/scenes/{destination.name}"
        scene_urls[source.stem] = url
        scene_files[source.stem] = destination.relative_to(app_root).as_posix()
    if "generic" not in scene_urls:
        raise ValueError("the canonical generic immersive scene is missing")
    return scene_urls, scene_files


def _rewrite_scene_loader(app_js: Path, scene_urls: dict[str, str]) -> None:
    source = app_js.read_text(encoding="utf-8")
    declaration = (
        "const SCENE_ASSET_URLS = Object.freeze("
        + json.dumps(scene_urls, ensure_ascii=True, separators=(",", ":"))
        + ");"
    )
    if SCENE_MAP_DECLARATION.search(source):
        source = SCENE_MAP_DECLARATION.sub(declaration, source, count=1)
    else:
        source, count = SCENE_KEYS_DECLARATION.subn(
            lambda match: match.group(1) + declaration + "\n", source, count=1
        )
        if count != 1:
            raise ValueError("app.js has no stable SCENE_KEYS declaration")

    replacement = (
        r'\g<indent>$("scene-backdrop").src = '
        r'SCENE_ASSET_URLS[visualKey] || SCENE_ASSET_URLS.generic;'
    )
    source, count = SCENE_ASSIGNMENT.subn(replacement, source, count=1)
    if count == 0 and "SCENE_ASSET_URLS[visualKey]" not in source:
        raise ValueError("app.js has no supported immersive scene asset assignment")

    temporary = app_js.with_name(f".{app_js.name}.scene-map.tmp")
    temporary.write_text(source, encoding="utf-8", newline="")
    temporary.replace(app_js)


def fingerprint(app_root: Path) -> dict[str, Any]:
    app_root = app_root.resolve()
    url_map: dict[str, str] = {}
    files: dict[str, str] = {}

    scene_urls, scene_files = _fingerprint_scenes(app_root)
    _rewrite_scene_loader(app_root / "public_frontend" / "app.js", scene_urls)

    for asset in ASSETS:
        source = app_root / asset.source
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        destination = _fingerprinted_path(source, digest)
        destination.write_bytes(payload)
        fingerprinted_url = asset.public_url.rsplit(".", 1)
        new_url = f"{fingerprinted_url[0]}.{digest[:FINGERPRINT_LENGTH]}.{fingerprinted_url[1]}"
        url_map[asset.public_url] = new_url
        files[asset.source] = destination.relative_to(app_root).as_posix()

    rewritten: list[str] = []
    referenced_urls: set[str] = set()
    for relative_path in HTML_DOCUMENTS:
        path = app_root / relative_path
        document = path.read_text(encoding="utf-8")
        changed = False
        for scene_name, new_url in scene_urls.items():
            old_url = f"/assets/immersive/scenes/{scene_name}.svg"
            document, count = _replace_reference(document, old_url, new_url)
            changed = changed or bool(count)
        for old_url, new_url in url_map.items():
            document, count = _replace_reference(document, old_url, new_url)
            if count:
                changed = True
                referenced_urls.add(old_url)
            elif new_url in document:
                referenced_urls.add(old_url)
        if changed:
            temporary = path.with_name(f".{path.name}.fingerprint.tmp")
            temporary.write_text(document, encoding="utf-8", newline="")
            temporary.replace(path)
        rewritten.append(relative_path)

    missing_references = sorted(set(url_map) - referenced_urls)
    if missing_references:
        raise ValueError(
            "frontend HTML does not reference required assets: "
            + ", ".join(missing_references)
        )

    return {
        "schema_version": "project-snow-static-assets-1",
        "fingerprint_length": FINGERPRINT_LENGTH,
        "assets": url_map,
        "files": files,
        "scene_assets": scene_urls,
        "scene_files": scene_files,
        "rewritten_documents": rewritten,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the App directory copied into the production image",
    )
    args = parser.parse_args()
    result = fingerprint(args.app_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
