"""Shared, source-safe helpers for application pipelines.

The crawler owns Data/. These helpers only read manifest-listed source files and
write generated artifacts below App/runtime/.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import unicodedata
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - dependency check is exercised by installation
    BeautifulSoup = None


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent
DATA_ROOT = Path(os.getenv("DATA_ROOT", PROJECT_ROOT / "Data")).resolve()
RUNTIME_ROOT = Path(os.getenv("APP_RUNTIME", APP_ROOT / "runtime")).resolve()
MANIFEST_ROOT = DATA_ROOT / "Manifest"

INDEX_SOURCE_TYPES = {
    "affinity_stories_index.jsonl": "affinity_story",
    "character_armors_index.jsonl": "character_armor",
    "character_costumes_index.jsonl": "character_costume",
    "character_profiles_index.jsonl": "character_profile",
    "character_stories_index.jsonl": "character_story",
    "character_voices_index.jsonl": "character_voice",
    "enemies_index.jsonl": "enemy_lore",
    "events_index.jsonl": "event_lore",
    "exploration_notes_index.jsonl": "exploration_note",
    "furniture_index.jsonl": "furniture_lore",
    "items_index.jsonl": "item_lore",
    "logistics_index.jsonl": "logistics_lore",
    "main_story_index.jsonl": "main_story",
    "random_events_index.jsonl": "random_event",
    "special_mail_index.jsonl": "special_mail",
    "weapons_index.jsonl": "weapon_lore",
    "weapon_attachments_index.jsonl": "weapon_attachment",
}

SOURCE_PRIORITY = {
    "main_story": 1.00,
    "character_story": 1.00,
    "affinity_story": 0.98,
    "special_mail": 0.95,
    "character_profile": 0.93,
    "random_event": 0.90,
    "character_voice": 0.88,
    "exploration_note": 0.85,
    "event_lore": 0.80,
    "character_costume": 0.75,
    "furniture_lore": 0.68,
    "item_lore": 0.62,
    "weapon_lore": 0.60,
    "logistics_lore": 0.56,
    "enemy_lore": 0.55,
    "weapon_attachment": 0.35,
    "character_armor": 0.35,
}

# Only these catalogues explicitly identify a character with an ID/name pair.
# Item manifests use character_ids for some mail-page references, so they must
# never be treated as an authority for the character catalogue.
CHARACTER_AUTHORITY_SOURCE_TYPES = {
    "character_profile",
    "character_armor",
    "character_costume",
    "character_story",
    "affinity_story",
    "character_voice",
    "random_event",
}

# The crawler records some operators once by their given name and again by a
# full name. Those records are evidence for one character, not separate chat
# identities. Keep this policy in the application layer so Data/ remains a
# faithful crawler snapshot.
CHARACTER_ID_ALIASES = {
    "354212435bcc": "1b0a6b35719a",  # 芬妮 -> 芬妮·戈尔登
    "2056f7696dea": "673ba6851b05",  # 苔丝 -> 苔丝·科特金
    "b54b34432e2b": "daab0f4cceb4",  # 茉莉安 -> 茉莉安·安德烈奥蒂
    "1fb06e33b46b": "98322bd505f4",  # 姬辰星 -> 辰星
    "d6123fd71749": "921f9ef0cc4e",  # 鸣濑晴 -> 晴
}

CHARACTER_DISPLAY_NAMES = {
    "1b0a6b35719a": "芬妮",
    "673ba6851b05": "苔丝",
    "daab0f4cceb4": "茉莉安",
    "98322bd505f4": "辰星",
    "921f9ef0cc4e": "晴",
}

CHARACTER_NAME_ALIASES = {
    "芬妮": ("1b0a6b35719a", "芬妮"),
    "芬妮·戈尔登": ("1b0a6b35719a", "芬妮"),
    "苔丝": ("673ba6851b05", "苔丝"),
    "苔丝·科特金": ("673ba6851b05", "苔丝"),
    "茉莉安": ("daab0f4cceb4", "茉莉安"),
    "茉莉安·安德烈奥蒂": ("daab0f4cceb4", "茉莉安"),
    "辰星": ("98322bd505f4", "辰星"),
    "姬辰星": ("98322bd505f4", "辰星"),
    "晴": ("921f9ef0cc4e", "晴"),
    "鸣濑晴": ("921f9ef0cc4e", "晴"),
}

# NPC/world-lore entries remain in the corpus and graph but are never exposed
# as a companion persona or as a selectable dialogue character.
NON_DIALOGUE_CHARACTER_IDS = frozenset({"011ead465049"})  # 米拉·吉诺拉

TEXT_FIELDS = {
    "affinity_story": ("story_title", "story_subtitle", "story_summary", "speaker_names"),
    "character_armor": ("character_name", "armor_name"),
    "character_costume": ("character_name", "armor_name", "costume_name", "costume_description"),
    "character_profile": ("character_name", "description", "section_headings"),
    "character_story": ("story_title", "story_subtitle", "story_summary", "speaker_names"),
    "character_voice": ("character_name", "armor_name", "voice_excerpt", "voice_lines"),
    "enemy_lore": ("enemy_name", "enemy_description", "enemy_behavior", "enemy_skill_text"),
    "event_lore": ("event_name", "description", "participant_names"),
    "exploration_note": ("glossary_title", "summary", "speaker_names"),
    "furniture_lore": ("furniture_name", "description", "gift_dialogue", "owner_characters"),
    "item_lore": ("item_name", "item_description", "item_use", "character_names"),
    "logistics_lore": (
        "squad_name",
        "member_names",
        "story_text",
        "recommended_character_names",
        "logistics_tag",
    ),
    "main_story": ("story_display_title", "story_subtitle", "canonical_story_label"),
    "random_event": ("event_name", "character_name", "trigger_conditions", "speaker_names"),
    "special_mail": ("sender_name", "mail_subject", "mail_body", "mail_excerpt"),
    "weapon_lore": ("weapon_name", "weapon_description", "recommended_character_names"),
    "weapon_attachment": ("attachment_name", "weapon_name", "description"),
}

SKIP_RECORD_FIELDS = {
    "local_path",
    "canonical_url",
    "catalog_url",
    "media_urls",
    "attached_media_urls",
    "source_image_url",
    "file_page_url",
    "mechanics",
    "mechanics_sections",
    "occurrences",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_id(*parts: Any, prefix: str = "") -> str:
    payload = "\x1f".join(str(part) for part in parts if part is not None)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}" if prefix else digest


def ensure_runtime(*parts: str) -> Path:
    path = RUNTIME_ROOT.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {path}:{line_number}")
            yield value


def _atomic_replace(temporary_path: Path, path: Path, attempts: int = 50, delay_seconds: float = 0.05) -> None:
    """Replace a runtime artifact, tolerating short Windows reader locks."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(temporary_path, path)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay_seconds)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    count = 0
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                count += 1
        _atomic_replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return count


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _atomic_replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def manifest_status_by_url() -> dict[str, dict[str, Any]]:
    path = MANIFEST_ROOT / "page_manifest.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Required source manifest is missing: {path}")
    return {row["canonical_url"]: row for row in read_jsonl(path) if row.get("canonical_url")}


def iter_index_records() -> Iterator[tuple[str, Path, dict[str, Any]]]:
    for name, source_type in INDEX_SOURCE_TYPES.items():
        path = MANIFEST_ROOT / name
        if not path.exists():
            continue
        for row in read_jsonl(path):
            yield source_type, path, row


def canonical_character_identity(identifier: Any, name: Any) -> tuple[str | None, str | None]:
    """Return the project identity and display name for a crawler identity."""
    raw_id = str(identifier).strip() if identifier else None
    raw_name = str(name).strip() if name else None
    canonical_id = CHARACTER_ID_ALIASES.get(raw_id, raw_id)
    alias_identity = CHARACTER_NAME_ALIASES.get(raw_name or "")
    if alias_identity:
        canonical_id = alias_identity[0]
    canonical_name = CHARACTER_DISPLAY_NAMES.get(canonical_id)
    if canonical_name is None and alias_identity and alias_identity[0] == canonical_id:
        canonical_name = alias_identity[1]
    return canonical_id, canonical_name or raw_name


def _canonical_character_pairs(ids: Any, names: Any) -> list[tuple[str, str]]:
    values = ids if isinstance(ids, list) else ([] if ids is None else [ids])
    labels = names if isinstance(names, list) else ([] if names is None else [names])
    pairs: list[tuple[str, str]] = []
    for index, identifier in enumerate(values):
        canonical_id, canonical_name = canonical_character_identity(
            identifier, labels[index] if index < len(labels) else None
        )
        if canonical_id:
            pairs.append((canonical_id, canonical_name or canonical_id))
    return list(dict.fromkeys(pairs))


def normalize_character_references(record: dict[str, Any]) -> dict[str, Any]:
    """Copy a manifest record with all explicit character links canonicalized."""
    normalized = dict(record)
    character_id, character_name = canonical_character_identity(
        normalized.get("character_id"), normalized.get("character_name")
    )
    if character_id:
        normalized["character_id"] = character_id
        normalized["character_name"] = character_name or character_id

    for id_field, name_field in (
        ("related_character_ids", "related_character_names"),
        ("owner_character_ids", "owner_characters"),
        ("linked_character_ids", "linked_character_names"),
        ("recommended_character_ids", "recommended_character_names"),
        ("participant_ids", "participant_names"),
        ("member_ids", "member_names"),
    ):
        if id_field not in normalized:
            continue
        pairs = _canonical_character_pairs(normalized.get(id_field), normalized.get(name_field))
        normalized[id_field] = [identifier for identifier, _ in pairs]
        normalized[name_field] = [name for _, name in pairs]
    return normalized


@lru_cache(maxsize=1)
def known_characters() -> dict[str, str]:
    characters: dict[str, str] = {}
    for source_type, _, record in iter_index_records():
        if source_type not in CHARACTER_AUTHORITY_SOURCE_TYPES:
            continue
        identifier, name = canonical_character_identity(record.get("character_id"), record.get("character_name"))
        if identifier and name:
            characters[identifier] = name
    return characters


@lru_cache(maxsize=1)
def known_armors() -> dict[str, dict[str, str]]:
    """Return armor labels mapped to their canonical character identity.

    Logistics pages expose recommendations as ``character·armor`` links, but
    older manifests did not persist those links.  Keeping this lookup in the
    application pipeline lets us recover the relationship from the immutable
    character-armour catalogue without treating logistics members as chat
    characters.
    """

    armors: dict[str, dict[str, str]] = {}
    for source_type, _, record in iter_index_records():
        if source_type != "character_armor":
            continue
        armor_id = str(record.get("armor_id") or "").strip()
        armor_name = str(record.get("armor_name") or "").strip()
        if not armor_id or not armor_name:
            continue
        character_id, character_name = canonical_character_identity(
            record.get("character_id"), record.get("character_name")
        )
        if not character_id:
            continue
        row = {
            "armor_id": armor_id,
            "armor_name": armor_name,
            "character_id": character_id,
            "character_name": character_name or str(record.get("character_name") or "").strip(),
        }
        labels = {
            armor_name,
            f"{row['character_name']}{armor_name}",
            f"{row['character_name']}·{armor_name}",
            str(record.get("canonical_armor_label") or "").replace("角色装甲 / ", ""),
        }
        for label in labels:
            compacted = _compact_reference(label)
            if compacted:
                armors[compacted] = row
    return armors


@lru_cache(maxsize=1)
def dialogue_characters() -> dict[str, str]:
    return {
        identifier: name
        for identifier, name in known_characters().items()
        if identifier not in NON_DIALOGUE_CHARACTER_IDS
    }


@lru_cache(maxsize=1)
def mail_character_context() -> dict[str, tuple[list[str], list[str]]]:
    known = known_characters()
    contexts: dict[str, tuple[list[str], list[str]]] = {}
    for source_type, _, record in iter_index_records():
        if source_type != "special_mail" or not record.get("mail_id"):
            continue
        record = normalize_character_references(record)
        ids = record.get("related_character_ids", []) or []
        names = record.get("related_character_names", []) or []
        pairs = [
            (identifier, names[index] if index < len(names) else known.get(identifier))
            for index, identifier in enumerate(ids)
            if identifier in known
        ]
        contexts[record["mail_id"]] = ([identifier for identifier, _ in pairs], [name for _, name in pairs])
    return contexts


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "、".join(filter(None, (as_text(item) for item in value)))
    if isinstance(value, dict):
        return "；".join(f"{key}: {as_text(item)}" for key, item in value.items() if as_text(item))
    return str(value).strip()


def resolve_source_path(local_path: str | None) -> Path | None:
    if not local_path:
        return None
    candidate = (PROJECT_ROOT / local_path).resolve()
    source_root = (DATA_ROOT / "Source").resolve()
    try:
        candidate.relative_to(source_root)
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def _compact_reference(value: Any) -> str:
    """Normalize a displayed character/armor label for exact matching."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(
        r"[\s\-—_·•⋅・,，。！？!?、:：;；'\"“”‘’()（）\[\]【】<>《》]+",
        "",
        normalized,
    )


def _split_logistics_style_label(value: str) -> tuple[str, str | None]:
    """Split a recommendation label into character and optional armor."""

    cleaned = re.sub(r"^文件\s*[:：]", "", str(value or "")).strip()
    cleaned = re.sub(r"(?:头像|头象)$", "", cleaned).strip()
    cleaned = re.sub(r"(?:专属后勤|专属|专用后勤|专用)$", "", cleaned).strip()
    parts = re.split(r"\s*[·•⋅・\-—]\s*", cleaned, maxsplit=1)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    return cleaned, None


def _resolve_logistics_style_label(
    label: Any,
    characters: dict[str, str],
    armors: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """Resolve a rendered recommendation/tag label to known IDs only."""

    raw = str(label or "").strip()
    if not raw:
        return None
    compacted = _compact_reference(raw)
    if not compacted:
        return None

    # Full armor labels are authoritative, including labels such as
    # ``奈莉德·冥河代理人`` and a bare armor name such as ``冥河代理人``.
    if compacted in armors:
        return dict(armors[compacted])

    character_label, armor_label = _split_logistics_style_label(raw)
    bare_armor = armors.get(_compact_reference(character_label))
    if bare_armor:
        return dict(bare_armor)
    character_id: str | None = None
    character_name: str | None = None
    character_compacted = _compact_reference(character_label)
    for known_id, known_name in characters.items():
        if _compact_reference(known_name) == character_compacted:
            character_id = str(known_id)
            character_name = known_name
            break
    if armor_label:
        armor = armors.get(_compact_reference(armor_label))
        if armor:
            # Prefer the catalogue's identity when the label contains a
            # known character; this also resolves aliases such as 凯西娅/凯茜娅.
            if character_id and character_id != armor.get("character_id"):
                return None
            return dict(armor)
    if character_id:
        return {
            "character_id": character_id,
            "character_name": character_name or character_label,
            "armor_id": "",
            "armor_name": "",
        }
    return None


@lru_cache(maxsize=512)
def _logistics_relationships_for_path(local_path: str) -> tuple[dict[str, str], ...]:
    """Extract role/armor links from an already-crawled logistics page.

    BWiki renders the visible recommendation cards below the ``角色推荐``
    heading and stores exclusive recommendations in a ``.tag`` element.  The
    parser is intentionally bounded to those sections so member names and
    sponsor names never become character relations.
    """

    path = resolve_source_path(local_path)
    if path is None or BeautifulSoup is None:
        return ()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.select_one("#mw-content-text") or soup.select_one("#bodyContent") or soup.select_one("main") or soup
    characters = known_characters()
    armors = known_armors()
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(label: Any, relation_source: str) -> None:
        relation = _resolve_logistics_style_label(label, characters, armors)
        if not relation:
            return
        key = (str(relation.get("character_id") or ""), str(relation.get("armor_id") or ""))
        if not key[0] or key in seen:
            return
        seen.add(key)
        relation = dict(relation)
        relation["relation_source"] = relation_source
        results.append(relation)

    # Exclusive tags are present even when the recommendation widget is empty.
    for tag in root.select(".tag"):
        text = tag.get_text(" ", strip=True)
        if "专属" in text or "专用" in text:
            text = re.sub(r"^TAG\s*[:：]?\s*", "", text).strip()
            add(text, "exclusive_tag")

    for heading in root.find_all(["h2", "h3", "h4"]):
        if _compact_reference(heading.get_text(" ", strip=True)) != _compact_reference("角色推荐"):
            continue
        try:
            level = int(str(heading.name)[1:])
        except (TypeError, ValueError):
            level = 2
        node = heading.next_sibling
        while node is not None:
            if getattr(node, "name", None) in {"h1", "h2", "h3", "h4"}:
                try:
                    next_level = int(str(node.name)[1:])
                except (TypeError, ValueError):
                    next_level = level
                if next_level <= level:
                    break
            if getattr(node, "select", None):
                anchors = node.select("a[title], a[href]")
                for anchor in anchors:
                    label = anchor.get("title") or anchor.get_text(" ", strip=True)
                    add(label, "role_recommendation")
            node = node.next_sibling

    # Older squad pages omit the final ``角色推荐`` heading entirely but keep
    # the recommended armor as a link inside the three-piece effect table.
    # Matching only against the known armor catalogue avoids turning ordinary
    # member, sponsor, or navigation links into role relationships.
    for anchor in root.select("a[title]"):
        add(anchor.get("title"), "named_armor_reference")

    return tuple(results)


def _logistics_relationships(record: dict[str, Any]) -> list[dict[str, str]]:
    """Combine persisted relations with the HTML fallback for old manifests."""

    if str(record.get("logistics_kind") or "squad") not in {"squad", ""}:
        return []
    relations: list[dict[str, str]] = []
    raw_recommendations = record.get("recommended_characters") or record.get("logistics_recommendations")
    if isinstance(raw_recommendations, list):
        for item in raw_recommendations:
            if isinstance(item, dict):
                relation = {
                    "character_id": str(item.get("character_id") or ""),
                    "character_name": str(item.get("character_name") or item.get("name") or ""),
                    "armor_id": str(item.get("armor_id") or ""),
                    "armor_name": str(item.get("armor_name") or ""),
                    "relation_source": "manifest",
                }
                if relation["character_id"]:
                    relations.append(relation)
    # A future manifest may persist parallel ID/name arrays without the full
    # recommendation objects.  Recover those rows before falling back to HTML.
    persisted_ids = record.get("recommended_character_ids") or record.get("logistics_recommended_character_ids") or []
    persisted_names = record.get("recommended_character_names") or record.get("logistics_recommended_character_names") or []
    persisted_armors = record.get("recommended_armor_ids") or record.get("logistics_recommended_armor_ids") or []
    if isinstance(persisted_ids, list):
        for index, identifier in enumerate(persisted_ids):
            if not identifier:
                continue
            relations.append(
                {
                    "character_id": str(identifier),
                    "character_name": str(persisted_names[index] if index < len(persisted_names) else ""),
                    "armor_id": str(persisted_armors[index] if index < len(persisted_armors) else ""),
                    "armor_name": "",
                    "relation_source": "manifest_arrays",
                }
            )
    local_path = str(record.get("local_path") or "").strip()
    if local_path:
        relations.extend(_logistics_relationships_for_path(local_path))
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for relation in relations:
        key = (str(relation.get("character_id") or ""), str(relation.get("armor_id") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        unique.append(relation)
    return unique


def _remove_nested_templates(text: str) -> str:
    output: list[str] = []
    index = 0
    depth = 0
    while index < len(text):
        token = text[index : index + 2]
        if token == "{{":
            depth += 1
            index += 2
        elif token == "}}" and depth:
            depth -= 1
            index += 2
        elif depth == 0:
            output.append(text[index])
            index += 1
        else:
            index += 1
    return "".join(output)


def clean_wikitext(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^/]*/>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Dialogue-heavy pages store their actual lines inside templates. Removing
    # templates wholesale would silently discard the very material needed for
    # persona reconstruction, so retain parameter values as ordinary text.
    text = text.replace("{{", "\n").replace("}}", "\n").replace("|", "\n")
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^\s*[=*#;:]+\s*", "", text, flags=re.MULTILINE)
    return normalize_text(html.unescape(text))


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t \u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_source_text(local_path: str | None) -> str:
    path = resolve_source_path(local_path)
    if path is None:
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        if BeautifulSoup is None:
            return normalize_text(re.sub(r"<[^>]+>", " ", raw))
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return normalize_text(soup.get_text("\n"))
    return clean_wikitext(raw)


def record_preamble(source_type: str, record: dict[str, Any]) -> str:
    labels: list[str] = [f"资料类型：{source_type}"]
    for field in TEXT_FIELDS.get(source_type, ()):
        value = as_text(record.get(field))
        if value:
            labels.append(f"{field}：{value}")
    return "\n".join(labels)


def contextual_metadata(source_type: str, record: dict[str, Any], manifest: dict[str, Any] | None) -> dict[str, Any]:
    record = normalize_character_references(record)
    known = known_characters()
    character_id = record.get("character_id")
    character_name = record.get("character_name")
    if character_id not in known and source_type in CHARACTER_AUTHORITY_SOURCE_TYPES:
        character_id = None
        character_name = None
    if source_type == "item_lore":
        related_ids = record.get("linked_character_ids", []) or []
        related_names = record.get("linked_character_names", []) or []
    else:
        related_ids = record.get("related_character_ids", record.get("owner_character_ids", [])) or []
        related_names = record.get("related_character_names", record.get("owner_characters", [])) or []
    pairs = [
        (identifier, related_names[index] if index < len(related_names) else known.get(identifier))
        for index, identifier in enumerate(related_ids)
        if identifier in known
    ]
    logistics_relations = _logistics_relationships(record) if source_type == "logistics_lore" else []
    logistics_pairs = [
        (str(item.get("character_id") or ""), str(item.get("character_name") or "").strip())
        for item in logistics_relations
        if str(item.get("character_id") or "") in known
    ]
    pairs = list(dict.fromkeys([*pairs, *logistics_pairs]))
    if not character_id and len(pairs) == 1:
        character_id, character_name = pairs[0]
    armor_pairs = list(
        dict.fromkeys(
            (
                str(item.get("armor_id") or ""),
                str(item.get("armor_name") or "").strip(),
            )
            for item in logistics_relations
            if str(item.get("armor_id") or "") and str(item.get("armor_name") or "").strip()
        )
    )
    armor_id = record.get("armor_id")
    armor_name = record.get("armor_name")
    if source_type == "logistics_lore" and len(armor_pairs) == 1:
        armor_id, armor_name = armor_pairs[0]
    return {
        "character_id": character_id,
        "character_name": character_name,
        "armor_id": armor_id,
        "armor_name": armor_name,
        "costume_id": record.get("costume_id"),
        "costume_name": record.get("costume_name"),
        "related_character_ids": [identifier for identifier, _ in pairs],
        "related_character_names": [name for _, name in pairs],
        "related_armor_ids": [identifier for identifier, _ in armor_pairs],
        "related_armor_names": [name for _, name in armor_pairs],
        "logistics_relationships": logistics_relations,
        "source_priority": SOURCE_PRIORITY.get(source_type, 0.5),
        "requires_costume_context": source_type == "character_costume",
        "narrative_relevance": record.get("narrative_relevance") or (manifest or {}).get("narrative_relevance"),
        "section_hints": record.get("section_hints", []),
    }


def chunk_text(text: str, max_chars: int = 900, overlap_chars: int = 120) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            sentences = re.split(r"(?<=[。！？!?])", paragraph)
        else:
            sentences = [paragraph]
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            proposed = f"{current}\n\n{sentence}".strip() if current else sentence
            if len(proposed) <= max_chars:
                current = proposed
                continue
            if current:
                chunks.append(current)
                tail = current[-overlap_chars:].strip()
                current = f"{tail}\n\n{sentence}" if tail else sentence
            else:
                chunks.append(sentence[:max_chars])
                current = sentence[max_chars - overlap_chars :]
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if len(chunk) >= 24]


def source_title(record: dict[str, Any]) -> str:
    for field in (
        "canonical_story_label",
        "canonical_profile_label",
        "canonical_voice_label",
        "canonical_mail_label",
        "story_title",
        "story_name",
        "mail_subject",
        "event_name",
        "squad_name",
        "item_name",
        "weapon_name",
        "character_name",
        "source_page_title",
    ):
        value = as_text(record.get(field))
        if value:
            return value
    return as_text(record.get("page_id")) or "未命名资料"


def iter_corpus_documents() -> Iterator[dict[str, Any]]:
    status_by_url = manifest_status_by_url()
    mail_context = mail_character_context()
    seen: set[tuple[str, str, str]] = set()
    for source_type, index_path, record in iter_index_records():
        record = normalize_character_references(record)
        if source_type == "item_lore":
            record = dict(record)
            contexts = [mail_context[mail_id] for mail_id in record.get("related_mail_ids", []) if mail_id in mail_context]
            record["linked_character_ids"] = list(dict.fromkeys(identifier for ids, _ in contexts for identifier in ids))
            record["linked_character_names"] = list(dict.fromkeys(name for _, names in contexts for name in names))
        canonical_url = record.get("canonical_url")
        manifest = status_by_url.get(canonical_url, {})
        status = manifest.get("status", "active")
        if status != "active":
            continue
        raw_text = read_source_text(record.get("local_path"))
        preamble = record_preamble(source_type, record)
        text = f"{preamble}\n\n{raw_text}".strip() if raw_text else preamble
        chunks = chunk_text(text)
        page_id = record.get("page_id") or manifest.get("page_id") or stable_id(canonical_url, prefix="page_")
        metadata = contextual_metadata(source_type, record, manifest)
        for ordinal, chunk in enumerate(chunks):
            fingerprint = hashlib.sha256(normalize_text(chunk).encode("utf-8")).hexdigest()
            key = (source_type, str(page_id), fingerprint)
            if key in seen:
                continue
            seen.add(key)
            document_id = stable_id(source_type, page_id, ordinal, fingerprint, prefix="doc_")
            yield {
                "document_id": document_id,
                "page_id": page_id,
                "source_type": source_type,
                "source_manifest": index_path.name,
                "title": source_title(record),
                "chunk_ordinal": ordinal,
                "text": chunk,
                "canonical_url": canonical_url,
                "local_path": record.get("local_path"),
                "source_license": record.get("source_license") or manifest.get("source_license"),
                "attribution": record.get("attribution") or manifest.get("attribution"),
                "source_content_hash": manifest.get("narrative_content_hash") or manifest.get("normalized_content_hash"),
                "created_at": utc_now(),
                "metadata": metadata,
            }


def load_runtime_jsonl(name: str) -> list[dict[str, Any]]:
    path = RUNTIME_ROOT / "lakehouse" / name
    return list(read_jsonl(path)) if path.exists() else []
