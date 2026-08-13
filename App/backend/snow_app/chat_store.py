"""Durable local conversation storage for the Project Snow test client."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Callable, Iterator


_STORE_LOCK = RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class ConversationStore:
    """SQLite-backed history while generation remains in ``MVPService``."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
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
        with _STORE_LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    character_id TEXT NOT NULL,
                    world_session_id TEXT NOT NULL,
                    communication_channel TEXT NOT NULL DEFAULT 'in_person',
                    session_state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversations_character_updated
                    ON conversations(character_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'divider')),
                    mode TEXT NOT NULL,
                    communication_channel TEXT NOT NULL,
                    text TEXT NOT NULL,
                    content_blocks_json TEXT NOT NULL DEFAULT '[]',
                    response_json TEXT,
                    client_message_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_row
                    ON messages(conversation_id, row_id DESC);

                CREATE TABLE IF NOT EXISTS client_requests (
                    client_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS request_claims (
                    client_message_id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS presence_arrivals (
                    arrival_id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    session_id TEXT,
                    world_session_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('noticed', 'unnoticed')),
                    status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'fallback_unnoticed')),
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS presence_arrivals_character_created
                    ON presence_arrivals(character_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS world_states (
                    world_session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def begin_presence_arrival(
        self,
        *,
        arrival_id: str,
        character_id: str,
        session_id: str | None,
        world_session_id: str,
        decision_factory: Callable[[], str],
    ) -> dict[str, Any]:
        """Claim one arrival decision exactly once."""

        now = _utc_now()
        with _STORE_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM presence_arrivals WHERE arrival_id = ?",
                (arrival_id,),
            ).fetchone()
            if row:
                existing = dict(row)
                if str(existing["character_id"]) != character_id:
                    connection.rollback()
                    raise ValueError("arrival_id 已被其他角色使用。")
                connection.commit()
                existing["response"] = _json_load(existing.pop("response_json"), None)
                return existing
            decision = decision_factory()
            if decision not in {"noticed", "unnoticed"}:
                connection.rollback()
                raise ValueError("到场决策必须是 noticed 或 unnoticed。")
            connection.execute(
                """
                INSERT INTO presence_arrivals (
                    arrival_id, character_id, session_id, world_session_id,
                    decision, status, response_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'processing', NULL, ?, ?)
                """,
                (arrival_id, character_id, session_id, world_session_id, decision, now, now),
            )
            connection.commit()
        return {
            "new": True,
            "arrival_id": arrival_id,
            "character_id": character_id,
            "session_id": session_id,
            "world_session_id": world_session_id,
            "decision": decision,
            "status": "processing",
            "response": None,
            "created_at": now,
            "updated_at": now,
        }

    def complete_presence_arrival(
        self,
        arrival_id: str,
        *,
        status: str,
        response: dict[str, Any],
    ) -> None:
        now = _utc_now()
        with _STORE_LOCK, self._connect() as connection:
            connection.execute(
                """
                UPDATE presence_arrivals
                SET status = ?, response_json = ?, updated_at = ?
                WHERE arrival_id = ?
                """,
                (status, _json_dump(response), now, arrival_id),
            )

    def save_assistant_message(
        self,
        *,
        character_id: str,
        session_id: str,
        world_session_id: str,
        response: dict[str, Any],
        session_state: dict[str, Any],
        world_state: dict[str, Any],
    ) -> str:
        """Persist an unsolicited assistant message without a user row."""

        conversation_id = self.conversation_id(session_id, character_id)
        created_at = _utc_now()
        message_id = str(response["message_id"])
        mode = str(response.get("mode") or "immersive")
        channel = str(response.get("communication_channel") or "in_person")
        payload = {**response, "conversation_id": conversation_id, "persisted": True}
        with _STORE_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT character_id FROM conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing and str(existing["character_id"]) != character_id:
                connection.rollback()
                raise ValueError("session_id 已属于其他角色会话。")
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, session_id, character_id, world_session_id,
                    communication_channel, session_state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    world_session_id = excluded.world_session_id,
                    communication_channel = excluded.communication_channel,
                    session_state_json = excluded.session_state_json,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, session_id, character_id, world_session_id, channel,
                 _json_dump(session_state), created_at, created_at),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO messages (
                    message_id, conversation_id, role, mode, communication_channel,
                    text, content_blocks_json, response_json, client_message_id, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, NULL, ?)
                """,
                (message_id, conversation_id, mode, channel,
                 str(response.get("answer") or ""),
                 _json_dump(response.get("content_blocks") or []),
                 _json_dump(payload), created_at),
            )
            connection.execute(
                """
                INSERT INTO world_states (world_session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(world_session_id) DO UPDATE SET
                    state_json = excluded.state_json, updated_at = excluded.updated_at
                """,
                (world_session_id, _json_dump(world_state), created_at),
            )
            connection.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('active_world_session_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (world_session_id,),
            )
            connection.commit()
        return conversation_id

    @staticmethod
    def conversation_id(session_id: str, character_id: str) -> str:
        digest = sha256(f"{session_id}\x1f{character_id}".encode("utf-8")).hexdigest()[:18]
        return f"conversation_{digest}"

    def duplicate_response(self, client_message_id: str | None) -> dict[str, Any] | None:
        if not client_message_id:
            return None
        with _STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT response_json FROM client_requests WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
        return _json_load(row["response_json"], None) if row else None

    def claim_request(self, client_message_id: str, character_id: str) -> bool:
        """Claim an idempotency key, replacing only an abandoned claim."""

        now = datetime.now(timezone.utc)
        with _STORE_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT character_id, claimed_at FROM request_claims WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
            if row:
                try:
                    claimed_at = datetime.fromisoformat(str(row["claimed_at"]))
                except ValueError:
                    claimed_at = now
                if (now - claimed_at).total_seconds() < 300:
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE request_claims SET character_id = ?, claimed_at = ? WHERE client_message_id = ?",
                    (character_id, now.isoformat(), client_message_id),
                )
            else:
                connection.execute(
                    "INSERT INTO request_claims (client_message_id, character_id, claimed_at) VALUES (?, ?, ?)",
                    (client_message_id, character_id, now.isoformat()),
                )
            connection.commit()
        return True

    def release_request(self, client_message_id: str | None) -> None:
        if not client_message_id:
            return
        with _STORE_LOCK, self._connect() as connection:
            connection.execute(
                "DELETE FROM request_claims WHERE client_message_id = ?",
                (client_message_id,),
            )
            connection.commit()

    def session_state(self, session_id: str) -> dict[str, Any] | None:
        with _STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT session_state_json FROM conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _json_load(row["session_state_json"], None) if row else None

    def world_state(self, world_session_id: str) -> dict[str, Any] | None:
        with _STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM world_states WHERE world_session_id = ?",
                (world_session_id,),
            ).fetchone()
        return _json_load(row["state_json"], None) if row else None

    def active_world_session_id(self) -> str | None:
        with _STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = 'active_world_session_id'"
            ).fetchone()
        return str(row["value"]) if row else None

    def save_presence_state(
        self,
        *,
        character_id: str,
        session_id: str | None,
        world_session_id: str,
        communication_channel: str,
        session_state: dict[str, Any] | None,
        world_state: dict[str, Any],
    ) -> bool:
        """Persist a channel/location transition without creating chat messages."""

        updated_at = _utc_now()
        conversation_updated = False
        with _STORE_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if session_id:
                row = connection.execute(
                    "SELECT conversation_id FROM conversations WHERE session_id = ? AND character_id = ?",
                    (session_id, character_id),
                ).fetchone()
                if row:
                    connection.execute(
                        """
                        UPDATE conversations
                        SET world_session_id = ?, communication_channel = ?,
                            session_state_json = ?, updated_at = ?
                        WHERE conversation_id = ?
                        """,
                        (
                            world_session_id,
                            communication_channel,
                            _json_dump(session_state or {}),
                            updated_at,
                            row["conversation_id"],
                        ),
                    )
                    conversation_updated = True
            connection.execute(
                """
                INSERT INTO world_states (world_session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(world_session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (world_session_id, _json_dump(world_state), updated_at),
            )
            connection.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('active_world_session_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (world_session_id,),
            )
            connection.commit()
        return conversation_updated

    def save_exchange(
        self,
        *,
        character_id: str,
        session_id: str,
        world_session_id: str,
        client_message_id: str | None,
        user_text: str,
        response: dict[str, Any],
        session_state: dict[str, Any],
        world_state: dict[str, Any],
        user_content_blocks: list[dict[str, str]] | None = None,
    ) -> str:
        conversation_id = self.conversation_id(session_id, character_id)
        created_at = _utc_now()
        assistant_message_id = str(response["message_id"])
        request_key = str(client_message_id or "").strip() or None
        user_message_id = "mvp_user_" + sha256(
            f"{assistant_message_id}\x1f{user_text}".encode("utf-8")
        ).hexdigest()[:16]
        mode = str(response.get("mode") or "immersive")
        channel = str(response.get("communication_channel") or "in_person")
        user_blocks = list(user_content_blocks or [])
        if not user_blocks:
            user_blocks = [{"type": "message", "text": user_text}]
        response_payload = {**response, "conversation_id": conversation_id, "persisted": True}

        with _STORE_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT character_id FROM conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing and str(existing["character_id"]) != character_id:
                connection.rollback()
                raise ValueError("session_id 已属于其他角色会话。")
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, session_id, character_id, world_session_id,
                    communication_channel, session_state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    world_session_id = excluded.world_session_id,
                    communication_channel = excluded.communication_channel,
                    session_state_json = excluded.session_state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    session_id,
                    character_id,
                    world_session_id,
                    str(session_state.get("communication_channel") or channel),
                    _json_dump(session_state),
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_id, conversation_id, role, mode, communication_channel,
                    text, content_blocks_json, response_json, client_message_id, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    user_message_id,
                    conversation_id,
                    mode,
                    channel,
                    user_text,
                    _json_dump(user_blocks),
                    request_key,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO messages (
                    message_id, conversation_id, role, mode, communication_channel,
                    text, content_blocks_json, response_json, client_message_id, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assistant_message_id,
                    conversation_id,
                    mode,
                    channel,
                    str(response.get("answer") or ""),
                    _json_dump(response.get("content_blocks") or []),
                    _json_dump(response_payload),
                    request_key,
                    created_at,
                ),
            )
            if request_key:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO client_requests (
                        client_message_id, conversation_id, character_id, mode,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        conversation_id,
                        character_id,
                        mode,
                        _json_dump(response_payload),
                        created_at,
                    ),
                )
                connection.execute(
                    "DELETE FROM request_claims WHERE client_message_id = ?",
                    (request_key,),
                )
            connection.execute(
                """
                INSERT INTO world_states (world_session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(world_session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (world_session_id, _json_dump(world_state), created_at),
            )
            connection.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('active_world_session_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (world_session_id,),
            )
            connection.commit()
        return conversation_id

    def latest_conversations(self, mode: str | None = None) -> list[dict[str, Any]]:
        with _STORE_LOCK, self._connect() as connection:
            mode_filter = str(mode or "").strip()
            if mode_filter:
                rows = connection.execute(
                    """
                    WITH ranked AS (
                        SELECT c.*, m.text AS last_message, m.role AS last_role,
                               m.communication_channel AS last_channel,
                               m.created_at AS last_updated_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.character_id ORDER BY m.row_id DESC
                               ) AS rank_number
                        FROM conversations c
                        JOIN messages m ON m.conversation_id = c.conversation_id
                        WHERE m.mode = ?
                    )
                    SELECT * FROM ranked WHERE rank_number = 1
                    ORDER BY last_updated_at DESC
                    """,
                    (mode_filter,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                WITH ranked AS (
                    SELECT c.*, m.text AS last_message, m.role AS last_role,
                           m.communication_channel AS last_channel,
                           m.created_at AS last_updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY c.character_id ORDER BY m.row_id DESC
                           ) AS rank_number
                    FROM conversations c
                    JOIN messages m ON m.conversation_id = c.conversation_id
                )
                SELECT * FROM ranked WHERE rank_number = 1
                ORDER BY last_updated_at DESC
                """
                ).fetchall()
        return [
            {
                "conversation_id": row["conversation_id"],
                "session_id": row["session_id"],
                "character_id": row["character_id"],
                "world_session_id": row["world_session_id"],
                "communication_channel": row["last_channel"] or row["communication_channel"],
                "last_message": row["last_message"] or "",
                "last_role": row["last_role"],
                "updated_at": row["last_updated_at"],
            }
            for row in rows
        ]

    def latest_conversation(self, character_id: str, mode: str | None = None) -> dict[str, Any] | None:
        return next(
            (item for item in self.latest_conversations(mode) if item["character_id"] == character_id),
            None,
        )

    def history(
        self,
        character_id: str,
        *,
        session_id: str | None = None,
        before: int | None = None,
        limit: int = 50,
        mode: str | None = None,
    ) -> dict[str, Any]:
        with _STORE_LOCK, self._connect() as connection:
            if session_id:
                conversation = connection.execute(
                    """
                    SELECT * FROM conversations
                    WHERE character_id = ? AND session_id = ?
                    """,
                    (character_id, session_id),
                ).fetchone()
            elif mode:
                conversation = connection.execute(
                    """
                    SELECT c.* FROM conversations c
                    JOIN messages m ON m.conversation_id = c.conversation_id
                    WHERE c.character_id = ? AND m.mode = ?
                    ORDER BY m.row_id DESC LIMIT 1
                    """,
                    (character_id, mode),
                ).fetchone()
            else:
                conversation = connection.execute(
                    """
                    SELECT * FROM conversations
                    WHERE character_id = ? ORDER BY updated_at DESC LIMIT 1
                    """,
                    (character_id,),
                ).fetchone()
            if not conversation:
                return {"conversation": None, "messages": [], "next_before": None}
            query = "SELECT * FROM messages WHERE conversation_id = ?"
            parameters: list[Any] = [conversation["conversation_id"]]
            if mode:
                query += " AND mode = ?"
                parameters.append(mode)
            if before is not None:
                query += " AND row_id < ?"
                parameters.append(before)
            query += " ORDER BY row_id DESC LIMIT ?"
            parameters.append(max(1, min(limit, 100)))
            rows = connection.execute(query, parameters).fetchall()

        messages = []
        for row in reversed(rows):
            messages.append(
                {
                    "cursor": row["row_id"],
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "mode": row["mode"],
                    "communication_channel": row["communication_channel"],
                    "text": row["text"],
                    "content_blocks": _json_load(row["content_blocks_json"], []),
                    "response": _json_load(row["response_json"], None),
                    "client_message_id": row["client_message_id"],
                    "created_at": row["created_at"],
                }
            )
        return {
            "conversation": {
                "conversation_id": conversation["conversation_id"],
                "session_id": conversation["session_id"],
                "character_id": conversation["character_id"],
                "world_session_id": conversation["world_session_id"],
                "communication_channel": (
                    messages[-1]["communication_channel"]
                    if messages
                    else conversation["communication_channel"]
                ),
                "created_at": conversation["created_at"],
                "updated_at": conversation["updated_at"],
            },
            "messages": messages,
            "next_before": rows[-1]["row_id"] if len(rows) == max(1, min(limit, 100)) else None,
            "mode": mode,
        }

    def clear(self, character_id: str, mode: str | None = None) -> dict[str, Any]:
        with _STORE_LOCK, self._connect() as connection:
            conversation = connection.execute(
                """
                SELECT * FROM conversations
                WHERE character_id = ? ORDER BY updated_at DESC LIMIT 1
                """,
                (character_id,),
            ).fetchone()
            if not conversation:
                return {"cleared": False, "character_id": character_id, "mode": mode}
            session_id = str(conversation["session_id"])
            if mode:
                connection.execute(
                    "DELETE FROM client_requests WHERE conversation_id = ? AND mode = ?",
                    (conversation["conversation_id"], mode),
                )
                connection.execute(
                    "DELETE FROM messages WHERE conversation_id = ? AND mode = ?",
                    (conversation["conversation_id"], mode),
                )
                state = _json_load(conversation["session_state_json"], {})
                mode_turns = state.setdefault("mode_turns", {"immersive": [], "assistant": []})
                mode_turns[mode] = []
                state["cross_mode_turns"] = [
                    item
                    for item in (state.get("cross_mode_turns") or [])
                    if str(item.get("mode") or "") != mode
                ]
                if state.get("mode") == mode:
                    state["turns"] = []
                connection.execute(
                    "UPDATE conversations SET session_state_json = ?, updated_at = ? WHERE conversation_id = ?",
                    (_json_dump(state), _utc_now(), conversation["conversation_id"]),
                )
            else:
                connection.execute(
                    "DELETE FROM conversations WHERE conversation_id = ?",
                    (conversation["conversation_id"],),
                )
            connection.commit()
        return {
            "cleared": True,
            "character_id": character_id,
            "mode": mode,
            "session_id": session_id,
        }
