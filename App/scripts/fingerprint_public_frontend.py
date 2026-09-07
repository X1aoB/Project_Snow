"""Fingerprint public frontend assets inside the production image.

The checked-in HTML keeps stable, version-query URLs so it remains convenient
to run directly during development.  The production Docker build runs this
script after copying the frontend and shared design files.  It creates
content-addressed copies and rewrites only the copied HTML and declared module import URLs, allowing the edge
to cache those immutable assets for a year while HTML remains ``no-store``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re
from typing import Any


FINGERPRINT_LENGTH = 16


@dataclass(frozen=True)
class Asset:
    source: str
    public_url: str
    dependencies: tuple[str, ...] = ()
    required_html: bool = True


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

EXISTING_FINGERPRINT = re.compile(r"\.[0-9a-f]{12,64}$")


def _fingerprinted_path(source: Path, digest: str) -> Path:
    return source.with_name(f"{source.stem}.{digest[:FINGERPRINT_LENGTH]}{source.suffix}")


def _replace_reference(document: str, old_url: str, new_url: str) -> tuple[str, int]:
    # Replace only a complete quoted URL in HTML or a declared module import.
    # Scene loaders and JavaScript identifiers are never rewritten.
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


def _build_assets(app_root: Path) -> tuple[Asset, ...]:
    manifest_path = app_root / "public_frontend/assets-manifest.json"
    if not manifest_path.is_file():
        return ASSETS
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "project-snow-frontend-build-1":
        raise ValueError("unsupported frontend asset manifest")
    assets = []
    known_urls: set[str] = set()
    for item in manifest.get("assets", []):
        source = str(item["source"])
        public_url = str(item["public_url"])
        candidate = (app_root / source).resolve()
        if not candidate.is_relative_to(app_root) or not candidate.is_file():
            raise ValueError(f"invalid frontend asset source: {source}")
        if not public_url.startswith("/") or ".." in public_url or public_url in known_urls:
            raise ValueError(f"invalid frontend asset URL: {public_url}")
        dependencies = tuple(item.get("dependencies", []))
        if not set(dependencies).issubset(known_urls):
            raise ValueError("asset dependencies must precede their entry point")
        assets.append(Asset(source, public_url, dependencies, item.get("required_html", True)))
        known_urls.add(public_url)
    if not assets:
        raise ValueError("frontend asset manifest is empty")
    return tuple(assets)


def _scene_manifest_tag(scene_urls: dict[str, str]) -> str:
    encoded = html.escape(json.dumps(scene_urls, sort_keys=True, separators=(",", ":")), quote=True)
    return f'<meta name="snow-scene-assets" content="{encoded}" />'


def fingerprint(app_root: Path) -> dict[str, Any]:
    app_root = app_root.resolve()
    url_map: dict[str, str] = {}
    files: dict[str, str] = {}

    scene_urls, scene_files = _fingerprint_scenes(app_root)
    assets = _build_assets(app_root)
    for asset in assets:
        source = app_root / asset.source
        payload = source.read_bytes()
        if asset.dependencies:
            document = payload.decode("utf-8")
            for dependency in asset.dependencies:
                document, replaced = _replace_reference(document, dependency, url_map[dependency])
                # A second invocation accepts the already fingerprinted import.
                if not replaced and url_map[dependency] not in document:
                    raise ValueError(f"unreferenced entry dependency: {dependency}")
            payload = document.encode("utf-8")
            source.write_bytes(payload)
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
        tag = _scene_manifest_tag(scene_urls)
        pattern = r'<meta\s+name="snow-scene-assets"[^>]*>'
        if re.search(pattern, document):
            document = re.sub(pattern, lambda _: tag, document, count=1)
        elif "</head>" in document:
            document = document.replace("</head>", tag + "\n</head>", 1)
        else:
            document = tag + "\n" + document
        changed = True
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

    missing_references = sorted({asset.public_url for asset in assets if asset.required_html} - referenced_urls)
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
