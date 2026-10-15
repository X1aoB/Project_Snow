"""Build the self-hosted LXGW WenKai Screen subsets for the snow-adv UI.

Input is the pinned npm package ``lxgw-wenkai-screen-webfont@1.7.0`` (package
MIT, font files SIL OFL 1.1).  The script keeps the package's own
unicode-range subsets but:

* removes emoji and pictographic code points from every ``unicode-range`` so
  emoji keep using the system colour emoji font, and drops subsets that held
  nothing else;
* copies each kept ``.woff2`` under a ``<name>.<sha256[:16]>.woff2`` name, so
  the edge serves it with the existing one-year immutable cache rule;
* writes ``public_frontend/snow-adv-fonts.css`` with absolute URLs.

The font files themselves are copied byte for byte (no glyph modification).

    python App/scripts/build_snow_adv_fonts.py --package <node_modules/lxgw-wenkai-screen-webfont>
    python App/scripts/build_snow_adv_fonts.py --package <...> --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

PACKAGE_NAME = "lxgw-wenkai-screen-webfont"
PACKAGE_VERSION = "1.7.0"
SOURCE_STYLESHEET = "lxgwwenkaigbscreen.css"
FAMILY = "LXGW WenKai Screen"
FINGERPRINT_LENGTH = 16
APP_ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = APP_ROOT / "public_frontend" / "assets" / "fonts" / "lxgw-wenkai-screen"
FONT_URL = "/assets/fonts/lxgw-wenkai-screen"
STYLESHEET = APP_ROOT / "public_frontend" / "snow-adv-fonts.css"

# Code points that must fall back to the system emoji font.
EXCLUDED_RANGES = (
    (0x200D, 0x200D),  # zero width joiner
    (0x2600, 0x27BF),  # miscellaneous symbols, dingbats
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows (stars, squares)
    (0xFE00, 0xFE0F),  # variation selectors
    (0x1F000, 0x1FAFF),  # mahjong ... symbols and pictographs extended-A
    (0xE0000, 0xE007F),  # tag characters
)

FACE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}")
SOURCE_URL = re.compile(r"url\('\./files/(?P<name>[A-Za-z0-9_-]+\.woff2)'\)")
RANGE = re.compile(r"unicode-range:\s*(?P<ranges>[^;}]+)")


def parse_ranges(value: str) -> list[tuple[int, int]]:
    ranges = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if not item.upper().startswith("U+") or "?" in item:
            raise ValueError(f"unsupported unicode-range item: {item}")
        low, _, high = item[2:].partition("-")
        ranges.append((int(low, 16), int(high or low, 16)))
    return ranges


def subtract_excluded(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    kept = []
    for low, high in ranges:
        pieces = [(low, high)]
        for ex_low, ex_high in EXCLUDED_RANGES:
            next_pieces = []
            for start, end in pieces:
                if end < ex_low or start > ex_high:
                    next_pieces.append((start, end))
                    continue
                if start < ex_low:
                    next_pieces.append((start, ex_low - 1))
                if end > ex_high:
                    next_pieces.append((ex_high + 1, end))
            pieces = next_pieces
        kept.extend(pieces)
    return kept


def format_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(f"U+{low:x}" if low == high else f"U+{low:x}-{high:x}" for low, high in ranges)


def build(package: Path) -> tuple[dict[str, bytes], str, dict]:
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if metadata.get("name") != PACKAGE_NAME or metadata.get("version") != PACKAGE_VERSION:
        raise ValueError(f"expected {PACKAGE_NAME}@{PACKAGE_VERSION}")
    source_css = (package / SOURCE_STYLESHEET).read_text(encoding="utf-8")
    files: dict[str, bytes] = {}
    faces: list[str] = []
    dropped: list[str] = []
    for match in FACE.finditer(source_css):
        body = match.group("body")
        if f"font-family: '{FAMILY}'" not in body or "font-weight: 400" not in body or "font-style: normal" not in body:
            raise ValueError("unexpected @font-face descriptor in the source package")
        source_name = SOURCE_URL.search(body).group("name")
        ranges = subtract_excluded(parse_ranges(RANGE.search(body).group("ranges")))
        if not ranges:
            dropped.append(source_name)
            continue
        payload = (package / "files" / source_name).read_bytes()
        if payload[:4] != b"wOF2":
            raise ValueError(f"not a WOFF2 file: {source_name}")
        digest = hashlib.sha256(payload).hexdigest()[:FINGERPRINT_LENGTH]
        target_name = source_name.replace("lxgwwenkaigbscreen-subset-", "wenkai-").removesuffix(".woff2") + f".{digest}.woff2"
        files[target_name] = payload
        faces.append(
            "@font-face{font-family:'LXGW WenKai Screen';font-style:normal;font-weight:400;font-display:swap;"
            f"src:url('{FONT_URL}/{target_name}') format('woff2');unicode-range:{format_ranges(ranges)}}}"
        )
    if not faces:
        raise ValueError("no font subsets found")
    stylesheet = (
        f"/* Generated by App/scripts/build_snow_adv_fonts.py from {PACKAGE_NAME}@{PACKAGE_VERSION}.\n"
        " * LXGW WenKai Screen (c) LXGW, SIL Open Font License 1.1 -- see assets/fonts/lxgw-wenkai-screen/OFL.txt.\n"
        " * Loaded only when the ui_snow_adv feature is enabled. Emoji code points are excluded on purpose. */\n"
        + "\n".join(faces)
        + "\n"
    )
    source = {
        "schema_version": "project-snow-font-source-1",
        "package": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "family": FAMILY,
        "font_license": "SIL Open Font License 1.1",
        "package_license": metadata.get("license"),
        "source_stylesheet": SOURCE_STYLESHEET,
        "excluded_ranges": [format_ranges([item]) for item in EXCLUDED_RANGES],
        "dropped_subsets": sorted(dropped),
        "files": {name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(files.items())},
    }
    files["OFL.txt"] = (package / "OFL.txt").read_bytes()
    files["SOURCE.json"] = (json.dumps(source, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return files, stylesheet, source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True, help=f"path to node_modules/{PACKAGE_NAME}")
    parser.add_argument("--check", action="store_true", help="verify the checked-in output instead of writing it")
    args = parser.parse_args()
    files, stylesheet, source = build(args.package)
    if args.check:
        existing = {path.name for path in FONT_DIR.iterdir()} if FONT_DIR.is_dir() else set()
        stale = existing ^ set(files)
        changed = [name for name, payload in files.items() if (FONT_DIR / name).is_file() and (FONT_DIR / name).read_bytes() != payload]
        if stale or changed or not STYLESHEET.is_file() or STYLESHEET.read_text(encoding="utf-8") != stylesheet:
            print("snow-adv fonts are out of date", sorted(stale), changed, file=sys.stderr)
            return 1
        return 0
    if FONT_DIR.exists():
        shutil.rmtree(FONT_DIR)
    FONT_DIR.mkdir(parents=True)
    for name, payload in files.items():
        (FONT_DIR / name).write_bytes(payload)
    STYLESHEET.write_text(stylesheet, encoding="utf-8", newline="\n")
    print(json.dumps({"subsets": len(source["files"]), "dropped": source["dropped_subsets"],
                      "bytes": sum(len(payload) for name, payload in files.items() if name.endswith(".woff2"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
