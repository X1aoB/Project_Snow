"""Build the always-on character identity card from the read-only wiki pages.

Every selectable character has armor pages under ``Data/Source/character_armors``;
only some have a ``character_profiles`` page.  Facts a character must always know
about herself therefore come from the armor info-box, and a field is published
only when all of that character's pages that state it agree.  ``Data/`` is never
written; the reviewed output lives in ``config/public_knowledge``.

    python pipelines/build_character_identity.py --data-root ../Data --check
    python pipelines/build_character_identity.py --data-root ../Data --write
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = APP_ROOT / "backend/snow_app/mvp_character_registry.json"
OUTPUT_PATH = APP_ROOT / "config/public_knowledge/character_identity.v1.json"
KNOWLEDGE_VERSION = "snow-identity-2026-11-08.1"
_TAGS = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<th[^>]*>((?:(?!</?th\b).)*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)


def _text(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", markup))).strip()


def page_facts(page: str) -> dict[str, object]:
    cells: dict[str, str] = {}
    for label, value in _ROW.findall(page):
        cells.setdefault(_text(label), _text(value))
    facts: dict[str, object] = {}
    full_name = cells.get("全名") or cells.get("全名/本名")
    if full_name:
        facts["full_name"] = full_name
    birthday = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", cells.get("生日", ""))
    if birthday:
        facts["birthday"] = (int(birthday.group(1)), int(birthday.group(2)))
    # Kept as written: 芙提雅's "158cm（自称160cm）" is itself something she knows.
    height = cells.get("身高", "").replace("厘米", "cm").replace(" ", "")
    measured = re.match(r"(\d{2,3}(?:\.\d)?cm)(.*)", height)
    if measured:
        facts["height"] = measured.group(1) + (f"（{measured.group(2)}）" if measured.group(2) else "")
    return facts


def character_card(character: dict, armor_root: Path, data_root: Path) -> tuple[dict, list[str]]:
    """Return one card and any disagreement between that character's pages."""
    values: dict[str, dict[object, list[str]]] = {"full_name": {}, "birthday": {}, "height": {}}
    paths = sorted((armor_root / character["source_name"]).glob("*.html"))
    armors = [path.stem.rsplit("__", 1)[0] for path in paths]
    # Some pages fill 全名 with the page title "name·armor", which is not a name.
    page_titles = {f"{character['display_name']}·{armor}" for armor in armors}
    for path in paths:
        relative = path.relative_to(data_root.parent).as_posix()
        for field, value in page_facts(path.read_text(encoding="utf-8")).items():
            if field == "full_name" and value in page_titles:
                continue
            values[field].setdefault(value, []).append(relative)
    problems = [f"{character['display_name']}: {field} disagrees: {sorted(map(str, found))}"
                for field, found in values.items() if len(found) > 1]
    card: dict[str, object] = {"character_id": character["character_id"], "display_name": character["display_name"]}
    evidence: dict[str, list[str]] = {}
    for field, found in values.items():
        if len(found) == 1:
            value, paths = next(iter(found.items()))
            card[field] = {"month": value[0], "day": value[1]} if field == "birthday" else value
            evidence[field] = paths
    card["armors"] = armors
    card["evidence"] = evidence
    return card, problems


def build(data_root: Path) -> tuple[dict, list[str]]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["characters"]
    armor_root = data_root / "Source/character_armors"
    cards, problems = [], []
    for character in registry:
        card, found = character_card(character, armor_root, data_root)
        cards.append(card)
        problems.extend(found)
        problems.extend(f"{character['display_name']}: missing {field}"
                        for field in ("birthday",) if field not in card)
    payload = {
        "schema_version": 1,
        "knowledge_version": KNOWLEDGE_VERSION,
        "release_status": "active",
        "policy": {
            "source": "read_only_wiki_armor_infobox",
            "scope": "facts_a_character_always_knows_about_herself",
            "birthday_timezone": "Asia/Shanghai",
            "user_chat_can_modify": False,
        },
        "characters": cards,
    }
    return payload, problems


def render(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true", help="fail when the committed card differs from the sources")
    args = parser.parse_args()
    payload, problems = build(args.data_root.resolve())
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    text = render(payload)
    if args.write:
        OUTPUT_PATH.write_text(text, encoding="utf-8", newline="\n")
    if args.check and OUTPUT_PATH.read_text(encoding="utf-8") != text:
        print("character_identity.v1.json differs from the sources", file=sys.stderr)
        return 1
    if not args.write and not args.check:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
