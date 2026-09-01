"""Versioned public character knowledge shared by Snow product surfaces.

The immutable source corpus under ``Data/`` remains read-only.  This module
loads a small reviewed release artifact used by immersive generation and by
portable public-data exports.  User conversations cannot mutate this data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PUBLIC_KNOWLEDGE_PATH = (
    PACKAGE_ROOT / "config" / "public_knowledge" / "character_relationships.v1.json"
)


class PublicKnowledgeError(ValueError):
    pass


class PublicKnowledge:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DEFAULT_PUBLIC_KNOWLEDGE_PATH).resolve()
        self._payload = self._load()
        self._relationships = {
            str(item["character_id"]): dict(item)
            for item in self._payload.get("formal_relationships") or []
        }

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicKnowledgeError(f"公共角色知识无法读取：{self.path}") from exc
        if payload.get("schema_version") != 1:
            raise PublicKnowledgeError("公共角色知识 schema_version 不受支持。")
        version = str(payload.get("knowledge_version") or "").strip()
        if not version:
            raise PublicKnowledgeError("公共角色知识缺少 knowledge_version。")
        seen: set[str] = set()
        for item in payload.get("formal_relationships") or []:
            character_id = str(item.get("character_id") or "").strip()
            if not character_id or character_id in seen:
                raise PublicKnowledgeError("正式关系名单包含空 ID 或重复角色。")
            if not str(item.get("display_name") or "").strip():
                raise PublicKnowledgeError("正式关系名单缺少角色显示名。")
            if not str(item.get("preferred_address") or "").strip():
                raise PublicKnowledgeError("正式关系名单缺少称呼。")
            seen.add(character_id)
        return payload

    @property
    def version(self) -> str:
        return str(self._payload["knowledge_version"])

    @property
    def schema_version(self) -> int:
        return int(self._payload["schema_version"])

    def relationship(self, character_id: str) -> dict[str, Any] | None:
        item = self._relationships.get(str(character_id or "").strip())
        return dict(item) if item else None

    def formal_relationships(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._relationships.values()]

    def formal_character_ids(self) -> frozenset[str]:
        return frozenset(self._relationships)

    def preferred_addresses(self) -> dict[str, str]:
        return {
            character_id: str(item["preferred_address"])
            for character_id, item in self._relationships.items()
        }

    def formal_roster(self) -> tuple[str, ...]:
        return tuple(str(item["display_name"]) for item in self._relationships.values())

    def public_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "knowledge_version": self.version,
            "release_status": self._payload.get("release_status"),
            "policy": dict(self._payload.get("policy") or {}),
            "formal_relationship_count": len(self._relationships),
        }
