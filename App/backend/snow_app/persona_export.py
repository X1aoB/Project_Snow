"""Export a portable, privacy-bounded persona bundle.

This module is deliberately independent from Codex, pairing credentials, and
the conversation/user-fact stores.  It reads only reviewed public knowledge
and rebuildable runtime projections, then emits the Snow Persona Bundle v1
contract consumed by external integrations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import Settings
from .mvp_policy import MVP_CHARACTERS, canonical_mvp_character
from .public_knowledge import PublicKnowledge
from .repository import RuntimeRepository


BUNDLE_SCHEMA_VERSION = "snow-persona-bundle.v1"
SOURCE_REPOSITORY = "https://github.com/X1aoB/Project_Snow"
PAYLOAD_NAMES = ("personas.json", "relationships.json", "knowledge.jsonl")
STYLE_FIELDS = (
    "identity_evidence",
    "address_terms",
    "self_reference_terms",
    "sentence_style",
    "analyst_interaction",
    "supported_preferences",
    "supported_dislikes",
    "supported_values",
    "supported_boundaries",
    "narrative_evolution",
    "trait_activation_policy",
)
PRIVATE_KEYS = {
    "api_key",
    "authorization",
    "conversation_history",
    "credential",
    "credential_ref",
    "local_path",
    "private_chat",
    "private_messages",
    "raw_messages",
    "secret",
    "token",
    "token_hash",
    "user_facts",
}
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s\"'=])(?:[a-z]:[\\/])")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|password|secret|token)\s*[:=]\s*\S+"
)
_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DROP = object()


class PersonaExportError(ValueError):
    """Raised when a safe, complete export cannot be produced."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonaExportError(f"Invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise PersonaExportError(f"Expected an object at {path}:{number}.")
        rows.append(value)
    return rows


def _unsafe_string(value: str) -> bool:
    lowered = value.casefold().strip()
    return bool(
        _WINDOWS_PATH.search(value)
        or lowered.startswith("file://")
        or lowered.startswith("/users/")
        or lowered.startswith("/home/")
        or _SECRET_ASSIGNMENT.search(value)
    )


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key and key.casefold() in PRIVATE_KEYS:
        return _DROP
    if isinstance(value, str):
        return _DROP if _unsafe_string(value) else value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            safe = _sanitize(child_value, key=str(child_key))
            if safe is not _DROP:
                result[str(child_key)] = safe
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for child in value:
            safe = _sanitize(child)
            if safe is not _DROP:
                result.append(safe)
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _DROP


def _safe_http_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or _unsafe_string(text):
        return None
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _selected_characters(character_values: Iterable[str] | None) -> list[Any]:
    if not character_values:
        return [character for character in MVP_CHARACTERS if character.selector_enabled]
    result = []
    seen: set[str] = set()
    for value in character_values:
        character = canonical_mvp_character(value)
        if character is None or not character.selector_enabled:
            raise PersonaExportError(f"Unknown or unavailable character: {value}")
        if character.character_id not in seen:
            result.append(character)
            seen.add(character.character_id)
    if not result:
        raise PersonaExportError("At least one character must be selected.")
    return result


def _related_character_ids(document: dict[str, Any]) -> set[str]:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        return set()
    result = {
        str(value).strip()
        for value in metadata.get("related_character_ids") or []
        if str(value).strip()
    }
    direct = str(metadata.get("character_id") or "").strip()
    if direct:
        result.add(direct)
    return result


def _source_commit(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        value = ""
    if len(value) < 7:
        raise PersonaExportError("Unable to resolve the source Git commit; pass --source-commit.")
    return value


def build_bundle(
    settings: Settings,
    *,
    character_values: Iterable[str] | None = None,
    default_character_id: str | None = None,
    source_commit: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Build a sanitized bundle in memory without touching private stores."""

    source_commit = str(source_commit or "").strip()
    if not _GIT_COMMIT.fullmatch(source_commit):
        raise PersonaExportError("source_commit must be a 7-to-64 character hexadecimal Git ID.")
    characters = _selected_characters(character_values)
    selected_ids = {character.character_id for character in characters}
    if default_character_id:
        default = canonical_mvp_character(default_character_id)
        if default is None or default.character_id not in selected_ids:
            raise PersonaExportError("The default character must be included in this bundle.")
        default_character_id = default.character_id
    elif len(characters) == 1:
        default_character_id = characters[0].character_id

    public_knowledge = PublicKnowledge()
    views = {
        str(row.get("character_id")): row
        for row in _read_jsonl(settings.runtime_root / "mvp" / "character_views.jsonl")
        if row.get("character_id")
    }
    profiles = {
        str(row.get("character_id")): row
        for row in _read_jsonl(
            settings.runtime_root / "personas" / "dialogue_style_profiles.jsonl"
        )
        if row.get("character_id")
    }
    missing_views = [
        character.display_name
        for character in characters
        if character.character_id not in views
    ]
    if missing_views:
        raise PersonaExportError(
            "Build the MVP character views before exporting: " + ", ".join(missing_views)
        )

    personas: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    declarations: list[dict[str, str]] = []
    for character in characters:
        view = views[character.character_id]
        profile = profiles.get(character.character_id) or {}
        persona = {
            field: _sanitize(profile.get(field), key=field)
            for field in STYLE_FIELDS
            if profile.get(field) not in (None, [], {})
        }
        persona = {key: value for key, value in persona.items() if value is not _DROP}
        display_name = str(view.get("character_name") or character.display_name).strip()
        if not display_name or _unsafe_string(display_name):
            raise PersonaExportError(f"Unsafe or missing display name for {character.character_id}.")
        personas.append(
            {
                "profile_version": public_knowledge.version,
                "character": {
                    "character_id": character.character_id,
                    "display_name": display_name,
                    "aliases": list(dict.fromkeys(character.aliases)),
                },
                "persona": persona,
                "public_knowledge_scope": {
                    "knowledge_version": public_knowledge.version,
                    "coverage": _sanitize(dict(view.get("coverage") or {})),
                },
            }
        )
        public_relationship = public_knowledge.relationship(character.character_id)
        if public_relationship:
            relationship = {
                "label": str(public_relationship.get("relationship_label") or "公开关系"),
                "summary": "来自 Project Snow 审核后的公开关系发布物。",
                "evidence_state": str(public_relationship.get("evidence_state") or "reviewed"),
            }
            preferred_address = str(public_relationship.get("preferred_address") or "分析员")
        else:
            relationship = {
                "label": "未发布正式关系",
                "summary": "公开关系发布物未声明特殊关系；使用中性称呼。",
                "evidence_state": "neutral_default",
            }
            preferred_address = "分析员"
        relationships.append(
            {
                "character_id": character.character_id,
                "preferred_address": preferred_address,
                "relationship": relationship,
            }
        )
        declarations.append(
            {
                "character_id": character.character_id,
                "display_name": display_name,
                "profile_version": public_knowledge.version,
            }
        )

    knowledge: list[dict[str, Any]] = []
    repository = RuntimeRepository(settings)
    for document in repository.documents():
        related_ids = sorted(_related_character_ids(document) & selected_ids)
        if not related_ids:
            # Global/unscoped records are intentionally excluded.  An export
            # must be an explicit public character projection, not a corpus dump.
            continue
        source_id = str(document.get("document_id") or "").strip()
        title = str(document.get("title") or "").strip()[:1000]
        body = str(document.get("text") or "").strip()[:12000]
        if (
            not source_id
            or not title
            or not body
            or _unsafe_string(source_id)
            or _unsafe_string(title)
            or _unsafe_string(body)
        ):
            continue
        for character_id in related_ids:
            citation: dict[str, Any] = {"label": title, "source_id": source_id}
            canonical_url = _safe_http_url(document.get("canonical_url"))
            if canonical_url:
                citation["canonical_url"] = canonical_url
            for key in ("source_type", "source_license"):
                value = str(document.get(key) or "").strip()
                if value and not _unsafe_string(value):
                    citation[key] = value
            knowledge.append(
                {
                    "document_id": f"{character_id}:{source_id}",
                    "character_id": character_id,
                    "title": title,
                    "text": body,
                    "citation": citation,
                }
            )
    knowledge.sort(key=lambda item: (item["character_id"], item["document_id"]))

    payloads = {
        "personas.json": (
            json.dumps(personas, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "relationships.json": (
            json.dumps(relationships, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "knowledge.jsonl": "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in knowledge
        ).encode("utf-8"),
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {"repository": SOURCE_REPOSITORY, "commit": source_commit},
        "knowledge_version": public_knowledge.version,
        "default_character_id": default_character_id,
        "characters": declarations,
        "files": {
            name: {"sha256": hashlib.sha256(payloads[name]).hexdigest()}
            for name in PAYLOAD_NAMES
        },
    }
    return manifest, payloads


def export_bundle(
    output: str | Path,
    settings: Settings,
    *,
    character_values: Iterable[str] | None = None,
    default_character_id: str | None = None,
    source_commit: str,
) -> dict[str, Any]:
    """Write a new directory or ZIP bundle atomically; existing targets are preserved."""

    target = Path(output).expanduser().resolve()
    if target.exists():
        raise PersonaExportError(f"Refusing to overwrite existing export: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest, payloads = build_bundle(
        settings,
        character_values=character_values,
        default_character_id=default_character_id,
        source_commit=source_commit,
    )
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="snow-persona-export-", dir=target.parent) as temp:
        staging = Path(temp)
        if target.suffix.casefold() == ".zip":
            archive = staging / target.name
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                bundle_zip.writestr("manifest.json", manifest_bytes)
                for name in PAYLOAD_NAMES:
                    bundle_zip.writestr(name, payloads[name])
            os.replace(archive, target)
        else:
            directory = staging / target.name
            directory.mkdir()
            (directory / "manifest.json").write_bytes(manifest_bytes)
            for name in PAYLOAD_NAMES:
                (directory / name).write_bytes(payloads[name])
            os.replace(directory, target)
    return {
        "output": str(target),
        "schema_version": manifest["schema_version"],
        "knowledge_version": manifest["knowledge_version"],
        "character_count": len(manifest["characters"]),
        "document_count": len(payloads["knowledge.jsonl"].splitlines()),
        "source": manifest["source"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New bundle directory or .zip path")
    parser.add_argument(
        "--character-id",
        action="append",
        dest="character_ids",
        help="Character ID or canonical name; repeat to export more than one",
    )
    parser.add_argument("--default-character-id")
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    project_root = Path(__file__).resolve().parents[3]
    commit = str(args.source_commit or "").strip() or _source_commit(project_root)
    result = export_bundle(
        args.output,
        settings,
        character_values=args.character_ids,
        default_character_id=args.default_character_id,
        source_commit=commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
