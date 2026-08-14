"""Read-only persona boundary for external Agent hosts.

The gateway deliberately exposes a small projection instead of a database or
conversation API.  Pairing tokens are stored only as hashes, while immersive
messages, scene state, costumes and Agent traces never enter the projection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import secrets
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .mvp_policy import canonical_mvp_character
from .mvp_service import MVPService


_LOCK = RLock()
PERSONA_TOKEN_CREDENTIAL_REF = "persona-codex-current-token"
PERSONA_PAIRING_ID_CREDENTIAL_REF = "persona-codex-current-pairing-id"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _token_hash(token: str) -> str:
    return sha256(str(token).encode("utf-8")).hexdigest()


class PersonaPairingStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with _LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS persona_pairings (
                    pairing_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_hint TEXT NOT NULL,
                    default_character_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS persona_pairings_status
                    ON persona_pairings(status, created_at);
                """
            )

    def create(self, label: str, default_character_id: str | None = None) -> dict[str, Any]:
        token = "snow_pair_" + secrets.token_urlsafe(32)
        pairing_id = "pairing_" + secrets.token_hex(8)
        created_at = _now()
        with _LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO persona_pairings(
                    pairing_id, label, token_hash, token_hint,
                    default_character_id, status, created_at
                ) VALUES(?,?,?,?,?,'active',?)
                """,
                (
                    pairing_id,
                    str(label or "Codex").strip()[:120] or "Codex",
                    _token_hash(token),
                    token[-6:],
                    default_character_id,
                    created_at,
                ),
            )
        return {
            "pairing_id": pairing_id,
            "pairing_token": token,
            "token_hint": token[-6:],
            "label": str(label or "Codex").strip()[:120] or "Codex",
            "default_character_id": default_character_id,
            "status": "active",
            "created_at": created_at,
            "token_notice": "配对令牌仅在本次响应中显示；请交由 Codex 插件保存。",
        }

    def authenticate(self, token: str) -> dict[str, Any] | None:
        digest = _token_hash(token)
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM persona_pairings WHERE token_hash=? AND status='active'",
                (digest,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE persona_pairings SET last_used_at=? WHERE pairing_id=?",
                (_now(), row["pairing_id"]),
            )
        result = dict(row)
        result.pop("token_hash", None)
        return result

    def revoke(self, pairing_id: str, authenticated_pairing_id: str) -> bool:
        if not pairing_id or pairing_id != authenticated_pairing_id:
            return False
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM persona_pairings WHERE pairing_id=?", (pairing_id,)
            ).fetchone()
            if not row or row["status"] != "active":
                return False
            connection.execute(
                "UPDATE persona_pairings SET status='revoked', revoked_at=? WHERE pairing_id=?",
                (_now(), pairing_id),
            )
        return True

    def summary(self) -> dict[str, Any]:
        with _LOCK, self._connect() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM persona_pairings WHERE status='active'"
            ).fetchone()[0]
        return {"active_pairing_count": int(active), "transport": "loopback_only"}


class PersonaGateway:
    """Produces a stable, read-only persona projection for Agent hosts."""

    FORBIDDEN_DATA_TYPES = (
        "immersive_messages",
        "conversation_summaries",
        "scene_state",
        "analyst_location",
        "character_location",
        "active_costume",
        "assistant_task_history",
        "agent_tool_logs",
        "attachments",
    )

    def __init__(self, service: MVPService, pairing_store: PersonaPairingStore):
        self.service = service
        self.pairing_store = pairing_store

    def resolve_character_id(self, value: str) -> str:
        canonical = canonical_mvp_character(value)
        if canonical is None or not canonical.selector_enabled:
            raise KeyError(value)
        return canonical.character_id

    def snapshot(self, character_value: str) -> dict[str, Any]:
        character_id = self.resolve_character_id(character_value)
        view = self.service._views().get(character_id)
        if not view:
            raise KeyError(character_value)
        profile = self.service._dialogue_profiles().get(character_id) or {}
        relationship = self.service.user_fact_store.relationship(character_id)
        relationship_value = dict((relationship or {}).get("value") or {})
        style = {
            key: profile.get(key)
            for key in (
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
            if profile.get(key) not in (None, [], {})
        }
        return {
            "schema_version": 1,
            "profile_version": self.service.public_knowledge.version,
            "character": {
                "character_id": character_id,
                "display_name": view.get("character_name"),
                "aliases": list(view.get("aliases") or []),
            },
            "relationship": {
                "status": relationship_value.get("relationship_label", "unconfirmed"),
                "preferred_address": relationship_value.get("preferred_address", "分析员"),
                "source": (relationship or {}).get("source", "neutral_default"),
                "source_version": (relationship or {}).get("source_version"),
                "write_back_allowed": False,
            },
            "persona": style,
            "public_knowledge_scope": {
                "knowledge_version": self.service.public_knowledge.version,
                "latest_narrative_state": True,
                "coverage": dict(view.get("coverage") or {}),
                "allowed": [
                    "reviewed character identity and dialogue style",
                    "reviewed relationship and address premise",
                    "public story and world knowledge returned by search",
                ],
            },
            "rendering_rules": {
                "layers": [
                    "preserve tool and source facts exactly",
                    "complete the task in the host Agent",
                    "apply the selected character voice to public summaries and final prose",
                ],
                "never_rewrite": [
                    "numbers",
                    "formulae",
                    "code",
                    "file paths",
                    "citations",
                    "tool results",
                ],
                "hidden_reasoning": "never_return",
                "public_execution_summary": "allowed",
                "one_character_per_task": True,
            },
            "forbidden_data_types": list(self.FORBIDDEN_DATA_TYPES),
        }

    def relationship(self, character_value: str) -> dict[str, Any]:
        snapshot = self.snapshot(character_value)
        return {
            "profile_version": snapshot["profile_version"],
            "character": snapshot["character"],
            "relationship": snapshot["relationship"],
        }

    def knowledge_search(
        self,
        query: str,
        character_value: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        character_id = self.resolve_character_id(character_value)
        result = self.service.retrieve(
            character_id,
            str(query or "").strip(),
            limit=max(1, min(int(limit), 8)),
            mode="assistant",
        )
        hits = []
        for hit in result.get("hits") or []:
            citation = dict(hit.get("citation") or {})
            # Local corpus paths are an implementation detail and reveal the
            # user's filesystem layout; the canonical citation is sufficient.
            citation.pop("local_path", None)
            hits.append(
                {
                    "citation": citation,
                    "text": str(hit.get("text") or "")[:12000],
                    "score": hit.get("score"),
                    "source_scope": (hit.get("metadata") or {}).get(
                        "mvp_document_origin", "unknown"
                    ),
                }
            )
        return {
            "query": str(query or "").strip(),
            "character_id": character_id,
            "profile_version": self.service.public_knowledge.version,
            "results": hits,
            "write_back_allowed": False,
        }
