"""Local structured user facts, isolated from immersive message history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator

from .public_knowledge import PublicKnowledge


_LOCK = RLock()
LOCAL_SUBJECT_ID = "local-default"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class UserFactStore:
    """Stores structured facts only; no raw prompts, replies or Agent traces."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
                CREATE TABLE IF NOT EXISTS user_facts (
                    fact_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    character_id TEXT,
                    fact_type TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS user_facts_subject_character
                    ON user_facts(subject_id, character_id, fact_type, status);
                CREATE TABLE IF NOT EXISTS user_fact_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(fact_id) REFERENCES user_facts(fact_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS user_fact_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def seed_public_relationships(
        self,
        knowledge: PublicKnowledge,
        subject_id: str = LOCAL_SUBJECT_ID,
    ) -> int:
        """Instantiate reviewed public defaults without copying any chat data."""

        now = _now()
        added = 0
        with _LOCK, self._connect() as connection:
            for relationship in knowledge.formal_relationships():
                character_id = str(relationship["character_id"])
                fact_id = f"relationship:{subject_id}:{character_id}"
                value = {
                    "relationship_label": relationship["relationship_label"],
                    "preferred_address": relationship["preferred_address"],
                    "evidence_state": relationship.get("evidence_state"),
                }
                exists = connection.execute(
                    "SELECT 1 FROM user_facts WHERE fact_id = ?", (fact_id,)
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO user_facts(
                        fact_id, subject_id, character_id, fact_type, value_json,
                        scope, source, source_version, status, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fact_id,
                        subject_id,
                        character_id,
                        "relationship",
                        json.dumps(value, ensure_ascii=False, sort_keys=True),
                        "snow_modules",
                        "public_default",
                        knowledge.version,
                        "active",
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO user_fact_events(fact_id, action, detail_json, created_at) VALUES(?,?,?,?)",
                    (
                        fact_id,
                        "seeded",
                        json.dumps({"knowledge_version": knowledge.version}, sort_keys=True),
                        now,
                    ),
                )
                added += 1
            connection.execute(
                "INSERT INTO user_fact_meta(key,value) VALUES('public_knowledge_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (knowledge.version,),
            )
        return added

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["value"] = json.loads(str(result.pop("value_json")))
        except (json.JSONDecodeError, TypeError):
            result["value"] = {}
        return result

    def active_facts(
        self,
        subject_id: str = LOCAL_SUBJECT_ID,
        *,
        character_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM user_facts WHERE subject_id = ? AND status = 'active'"
        parameters: list[Any] = [subject_id]
        if character_id:
            query += " AND character_id = ?"
            parameters.append(character_id)
        query += " ORDER BY fact_type, character_id, created_at"
        with _LOCK, self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row(row) for row in rows]

    def relationship(
        self,
        character_id: str,
        subject_id: str = LOCAL_SUBJECT_ID,
    ) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_facts
                WHERE subject_id = ? AND character_id = ?
                  AND fact_type = 'relationship' AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (subject_id, character_id),
            ).fetchone()
        return self._row(row) if row else None

    def revoke(self, fact_id: str, reason: str = "user_revoked") -> bool:
        now = _now()
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM user_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if not row or str(row["status"]) != "active":
                return False
            connection.execute(
                "UPDATE user_facts SET status='revoked', revoked_at=?, updated_at=? WHERE fact_id=?",
                (now, now, fact_id),
            )
            connection.execute(
                "INSERT INTO user_fact_events(fact_id, action, detail_json, created_at) VALUES(?,?,?,?)",
                (fact_id, "revoked", json.dumps({"reason": reason}), now),
            )
        return True
