"""Durable storage for the multimodal assistant and local Agent runtime.

The existing conversation store remains the compatibility source for ordinary
chat history.  This store owns only new capability metadata and can be removed
or migrated independently without touching the read-only Wiki corpus.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4


_LOCK = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class AgentStore:
    """Small versioned SQLite store for capability and Agent state."""

    SCHEMA_VERSION = 4

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
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
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS providers (
                    provider_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    credential_ref TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    trusted_data_types_json TEXT NOT NULL DEFAULT '[]',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models (
                    model_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    probe_status TEXT NOT NULL DEFAULT 'unverified',
                    probe_json TEXT NOT NULL DEFAULT '{}',
                    quality_score REAL NOT NULL DEFAULT 0,
                    context_window INTEGER,
                    max_output_tokens INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider_id, model_name),
                    FOREIGN KEY(provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS models_provider ON models(provider_id);
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    parse_status TEXT NOT NULL DEFAULT 'pending',
                    extracted_text TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS attachments_sha ON attachments(sha256);
                CREATE TABLE IF NOT EXISTS message_attachments (
                    message_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, attachment_id),
                    FOREIGN KEY(attachment_id) REFERENCES attachments(attachment_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS message_attachments_attachment
                    ON message_attachments(attachment_id);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    client_run_id TEXT,
                    character_id TEXT NOT NULL,
                    session_id TEXT,
                    mode TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_override_json TEXT NOT NULL DEFAULT '{}',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_runs_updated ON agent_runs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS agent_steps (
                    step_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    tool_name TEXT,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    risk_level TEXT NOT NULL DEFAULT 'read',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS agent_steps_run ON agent_steps(run_id, step_index);
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY(step_id) REFERENCES agent_steps(step_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    attachment_id TEXT,
                    file_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE SET NULL,
                    FOREIGN KEY(attachment_id) REFERENCES attachments(attachment_id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS connectors (
                    connector_id TEXT PRIMARY KEY,
                    connector_type TEXT NOT NULL,
                    account_label TEXT NOT NULL,
                    credential_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'disconnected',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # SQLite's ``CREATE TABLE IF NOT EXISTS`` does not update an
            # existing preview database.  Keep migrations deliberately small
            # and additive so a user's local conversations survive upgrades.
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "client_run_id" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN client_run_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_client_id "
                "ON agent_runs(client_run_id) WHERE client_run_id IS NOT NULL"
            )
            artifact_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "metadata_json" not in artifact_columns:
                connection.execute("ALTER TABLE artifacts ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute(
                "INSERT INTO store_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    def set_meta(self, key: str, value: Any) -> None:
        with _LOCK, self._connect() as connection:
            connection.execute(
                "INSERT INTO store_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), _dump(value)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with _LOCK, self._connect() as connection:
            row = connection.execute("SELECT value FROM store_meta WHERE key=?", (str(key),)).fetchone()
        return _load(row["value"], default) if row else default

    def upsert_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        provider_id = str(provider.get("provider_id") or self.new_id("provider"))
        record = {
            "provider_id": provider_id,
            "display_name": str(provider.get("display_name") or provider_id),
            "kind": str(provider.get("kind") or "openai-compatible"),
            "base_url": str(provider.get("base_url") or "").rstrip("/"),
            "credential_ref": str(provider.get("credential_ref") or ""),
            "enabled": 1 if provider.get("enabled", True) else 0,
            "trusted_data_types": list(provider.get("trusted_data_types") or []),
            "config": dict(provider.get("config") or {}),
            "created_at": str(provider.get("created_at") or now),
            "updated_at": now,
        }
        with _LOCK, self._connect() as connection:
            connection.execute(
                """INSERT INTO providers(
                    provider_id, display_name, kind, base_url, credential_ref, enabled,
                    trusted_data_types_json, config_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    display_name=excluded.display_name, kind=excluded.kind,
                    base_url=excluded.base_url, credential_ref=excluded.credential_ref,
                    enabled=excluded.enabled, trusted_data_types_json=excluded.trusted_data_types_json,
                    config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (
                    record["provider_id"], record["display_name"], record["kind"],
                    record["base_url"], record["credential_ref"], record["enabled"],
                    _dump(record["trusted_data_types"]), _dump(record["config"]),
                    record["created_at"], record["updated_at"],
                ),
            )
        return record

    @staticmethod
    def _provider_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "provider_id": row["provider_id"],
            "display_name": row["display_name"],
            "kind": row["kind"],
            "base_url": row["base_url"],
            "credential_ref": row["credential_ref"],
            "enabled": bool(row["enabled"]),
            "trusted_data_types": _load(row["trusted_data_types_json"], []),
            "config": _load(row["config_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_providers(self) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM providers ORDER BY display_name").fetchall()
        return [self._provider_row(row) for row in rows]

    def upsert_model(self, model: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        record = {
            "model_id": str(model.get("model_id") or self.new_id("model")),
            "provider_id": str(model.get("provider_id") or ""),
            "model_name": str(model.get("model_name") or ""),
            "capabilities": dict(model.get("capabilities") or {}),
            "probe_status": str(model.get("probe_status") or "unverified"),
            "probe": dict(model.get("probe") or {}),
            "quality_score": float(model.get("quality_score") or 0),
            "context_window": model.get("context_window"),
            "max_output_tokens": model.get("max_output_tokens"),
            "created_at": str(model.get("created_at") or now),
            "updated_at": now,
        }
        if not record["provider_id"] or not record["model_name"]:
            raise ValueError("模型必须包含 provider_id 和 model_name。")
        with _LOCK, self._connect() as connection:
            connection.execute(
                """INSERT INTO models(
                    model_id, provider_id, model_name, capabilities_json, probe_status,
                    probe_json, quality_score, context_window, max_output_tokens,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id, model_name) DO UPDATE SET
                    capabilities_json=excluded.capabilities_json, probe_status=excluded.probe_status,
                    probe_json=excluded.probe_json, quality_score=excluded.quality_score,
                    context_window=excluded.context_window, max_output_tokens=excluded.max_output_tokens,
                    updated_at=excluded.updated_at""",
                (
                    record["model_id"], record["provider_id"], record["model_name"],
                    _dump(record["capabilities"]), record["probe_status"], _dump(record["probe"]),
                    record["quality_score"], record["context_window"], record["max_output_tokens"],
                    record["created_at"], record["updated_at"],
                ),
            )
        stored = next(
            (
                item
                for item in self.list_models(record["provider_id"])
                if item.get("model_name") == record["model_name"]
            ),
            None,
        )
        return stored or record

    def list_models(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT m.*, p.display_name, p.kind, p.enabled AS provider_enabled FROM models m JOIN providers p ON p.provider_id=m.provider_id"
        values: tuple[Any, ...] = ()
        if provider_id:
            query += " WHERE m.provider_id = ?"
            values = (provider_id,)
        query += " ORDER BY m.quality_score DESC, m.model_name"
        with _LOCK, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [
            {
                "model_id": row["model_id"], "provider_id": row["provider_id"],
                "provider_name": row["display_name"], "provider_kind": row["kind"],
                "provider_enabled": bool(row["provider_enabled"]), "model_name": row["model_name"],
                "capabilities": _load(row["capabilities_json"], {}),
                "probe_status": row["probe_status"], "probe": _load(row["probe_json"], {}),
                "quality_score": row["quality_score"], "context_window": row["context_window"],
                "max_output_tokens": row["max_output_tokens"], "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def create_attachment(self, record: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        values = {
            "attachment_id": str(record.get("attachment_id") or self.new_id("attachment")),
            "sha256": str(record.get("sha256") or ""), "original_name": str(record.get("original_name") or "attachment"),
            "mime_type": str(record.get("mime_type") or "application/octet-stream"),
            "size_bytes": int(record.get("size_bytes") or 0), "storage_path": str(record.get("storage_path") or ""),
            "parse_status": str(record.get("parse_status") or "pending"),
            "extracted_text": str(record.get("extracted_text") or ""), "metadata": dict(record.get("metadata") or {}),
            "created_at": str(record.get("created_at") or now), "updated_at": now, "expires_at": record.get("expires_at"),
        }
        if not values["sha256"] or not values["storage_path"]:
            raise ValueError("附件必须包含 sha256 和 storage_path。")
        with _LOCK, self._connect() as connection:
            existing = connection.execute("SELECT * FROM attachments WHERE sha256 = ?", (values["sha256"],)).fetchone()
            if existing:
                return self.attachment_row(existing)
            connection.execute(
                """INSERT INTO attachments(attachment_id,sha256,original_name,mime_type,size_bytes,storage_path,
                    parse_status,extracted_text,metadata_json,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (values["attachment_id"], values["sha256"], values["original_name"], values["mime_type"], values["size_bytes"], values["storage_path"], values["parse_status"], values["extracted_text"], _dump(values["metadata"]), values["created_at"], values["updated_at"], values["expires_at"]),
            )
        return values

    @staticmethod
    def attachment_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"attachment_id": row["attachment_id"], "sha256": row["sha256"], "original_name": row["original_name"], "mime_type": row["mime_type"], "size_bytes": row["size_bytes"], "storage_path": row["storage_path"], "parse_status": row["parse_status"], "extracted_text": row["extracted_text"], "metadata": _load(row["metadata_json"], {}), "created_at": row["created_at"], "updated_at": row["updated_at"], "expires_at": row["expires_at"]}

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            row = connection.execute("SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,)).fetchone()
        return self.attachment_row(row) if row else None

    def list_attachments(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attachments ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (min(max(int(limit), 1), 500), max(int(offset), 0)),
            ).fetchall()
        return [self.attachment_row(row) for row in rows]

    def link_attachment(self, message_id: str, attachment_id: str) -> None:
        with _LOCK, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO message_attachments(message_id, attachment_id, created_at) VALUES(?,?,?)",
                (str(message_id), str(attachment_id), _now()),
            )

    def attachments_for_message(self, message_id: str) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT a.* FROM attachments a JOIN message_attachments ma ON ma.attachment_id=a.attachment_id WHERE ma.message_id=? ORDER BY ma.created_at",
                (str(message_id),),
            ).fetchall()
        return [self.attachment_row(row) for row in rows]

    def update_attachment_parse(self, attachment_id: str, status: str, text: str = "", metadata: dict[str, Any] | None = None) -> None:
        with _LOCK, self._connect() as connection:
            connection.execute("UPDATE attachments SET parse_status=?, extracted_text=?, metadata_json=?, updated_at=? WHERE attachment_id=?", (status, text[:1000000], _dump(metadata or {}), _now(), attachment_id))

    def update_attachment_expiry(self, attachment_id: str, expires_at: str | None) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            connection.execute(
                "UPDATE attachments SET expires_at=?, updated_at=? WHERE attachment_id=?",
                (expires_at, _now(), attachment_id),
            )
        return self.get_attachment(attachment_id)

    def delete_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        record = self.get_attachment(attachment_id)
        if not record:
            return None
        with _LOCK, self._connect() as connection:
            connection.execute("DELETE FROM attachments WHERE attachment_id = ?", (attachment_id,))
        return record

    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        result = {"run_id": str(record.get("run_id") or self.new_id("run")), "client_run_id": record.get("client_run_id"), "character_id": str(record.get("character_id") or ""), "session_id": record.get("session_id"), "mode": str(record.get("mode") or "assistant"), "task": str(record.get("task") or ""), "status": str(record.get("status") or "queued"), "model_override": dict(record.get("model_override") or {}), "state": dict(record.get("state") or {}), "created_at": now, "updated_at": now}
        with _LOCK, self._connect() as connection:
            connection.execute("INSERT INTO agent_runs(run_id,client_run_id,character_id,session_id,mode,task,status,model_override_json,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (result["run_id"], result["client_run_id"], result["character_id"], result["session_id"], result["mode"], result["task"], result["status"], _dump(result["model_override"]), _dump(result["state"]), now, now))
        return result

    @staticmethod
    def run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"run_id": row["run_id"], "client_run_id": row["client_run_id"], "character_id": row["character_id"], "session_id": row["session_id"], "mode": row["mode"], "task": row["task"], "status": row["status"], "model_override": _load(row["model_override_json"], {}), "state": _load(row["state_json"], {}), "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self.run_row(row) if row else None

    def get_run_by_client_id(self, client_run_id: str) -> dict[str, Any] | None:
        if not client_run_id:
            return None
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE client_run_id = ?",
                (str(client_run_id),),
            ).fetchone()
        return self.run_row(row) if row else None

    def update_run(self, run_id: str, *, status: str | None = None, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        existing = self.get_run(run_id)
        if not existing:
            return None
        next_status = status or existing["status"]
        next_state = state if state is not None else existing["state"]
        with _LOCK, self._connect() as connection:
            connection.execute("UPDATE agent_runs SET status=?, state_json=?, updated_at=? WHERE run_id=?", (next_status, _dump(next_state), _now(), run_id))
        return self.get_run(run_id)

    def append_step(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        result = {"step_id": str(record.get("step_id") or self.new_id("step")), "run_id": run_id, "step_index": int(record.get("step_index") or 0), "kind": str(record.get("kind") or "tool"), "tool_name": record.get("tool_name"), "status": str(record.get("status") or "running"), "input": dict(record.get("input") or {}), "output": dict(record.get("output") or {}), "risk_level": str(record.get("risk_level") or "read"), "created_at": now, "updated_at": now}
        with _LOCK, self._connect() as connection:
            connection.execute("INSERT INTO agent_steps(step_id,run_id,step_index,kind,tool_name,status,input_json,output_json,risk_level,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (result["step_id"], run_id, result["step_index"], result["kind"], result["tool_name"], result["status"], _dump(result["input"]), _dump(result["output"]), result["risk_level"], now, now))
        return result

    def update_step(self, step_id: str, *, status: str, output: dict[str, Any] | None = None) -> None:
        with _LOCK, self._connect() as connection:
            connection.execute("UPDATE agent_steps SET status=?, output_json=?, updated_at=? WHERE step_id=?", (status, _dump(output or {}), _now(), step_id))

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM agent_steps WHERE run_id=? ORDER BY step_index", (run_id,)).fetchall()
        return [{"step_id": row["step_id"], "run_id": row["run_id"], "step_index": row["step_index"], "kind": row["kind"], "tool_name": row["tool_name"], "status": row["status"], "input": _load(row["input_json"], {}), "output": _load(row["output_json"], {}), "risk_level": row["risk_level"], "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]

    def list_runs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_runs"
        values: list[Any] = []
        if status:
            query += " WHERE status = ?"
            values.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(min(max(int(limit), 1), 200))
        with _LOCK, self._connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [self.run_row(row) for row in rows]

    def create_approval(self, run_id: str, step_id: str, risk_level: str, summary: str) -> dict[str, Any]:
        now = _now()
        result = {"approval_id": self.new_id("approval"), "run_id": run_id, "step_id": step_id, "risk_level": risk_level, "summary": summary[:2000], "status": "pending", "note": "", "created_at": now, "updated_at": now}
        with _LOCK, self._connect() as connection:
            connection.execute("INSERT INTO approvals(approval_id,run_id,step_id,risk_level,summary,status,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", tuple(result.values()))
        return result

    def update_approval(self, approval_id: str, status: str, note: str = "") -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            connection.execute("UPDATE approvals SET status=?, note=?, updated_at=? WHERE approval_id=?", (status, note[:2000], _now(), approval_id))
            row = connection.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_artifact(self, record: dict[str, Any]) -> dict[str, Any]:
        result = {"artifact_id": str(record.get("artifact_id") or self.new_id("artifact")), "run_id": record.get("run_id"), "attachment_id": record.get("attachment_id"), "file_name": str(record.get("file_name") or "artifact"), "mime_type": str(record.get("mime_type") or "application/octet-stream"), "storage_path": str(record.get("storage_path") or ""), "sha256": str(record.get("sha256") or ""), "size_bytes": int(record.get("size_bytes") or 0), "metadata": dict(record.get("metadata") or {}), "created_at": _now()}
        with _LOCK, self._connect() as connection:
            connection.execute("INSERT INTO artifacts(artifact_id,run_id,attachment_id,file_name,mime_type,storage_path,sha256,size_bytes,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (result["artifact_id"], result["run_id"], result["attachment_id"], result["file_name"], result["mime_type"], result["storage_path"], result["sha256"], result["size_bytes"], _dump(result["metadata"]), result["created_at"]))
        return result

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [{**dict(row), "metadata": _load(row["metadata_json"], {})} for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return {**dict(row), "metadata": _load(row["metadata_json"], {})} if row else None

    def upsert_connector(self, record: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        result = {"connector_id": str(record.get("connector_id") or self.new_id("connector")), "connector_type": str(record.get("connector_type") or ""), "account_label": str(record.get("account_label") or ""), "credential_ref": str(record.get("credential_ref") or ""), "status": str(record.get("status") or "disconnected"), "config": dict(record.get("config") or {}), "created_at": str(record.get("created_at") or now), "updated_at": now}
        with _LOCK, self._connect() as connection:
            connection.execute("INSERT INTO connectors(connector_id,connector_type,account_label,credential_ref,status,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(connector_id) DO UPDATE SET account_label=excluded.account_label,credential_ref=excluded.credential_ref,status=excluded.status,config_json=excluded.config_json,updated_at=excluded.updated_at", (result["connector_id"], result["connector_type"], result["account_label"], result["credential_ref"], result["status"], _dump(result["config"]), result["created_at"], result["updated_at"]))
        return result

    def list_connectors(self) -> list[dict[str, Any]]:
        with _LOCK, self._connect() as connection:
            rows = connection.execute("SELECT * FROM connectors ORDER BY account_label").fetchall()
        return [{"connector_id": row["connector_id"], "connector_type": row["connector_type"], "account_label": row["account_label"], "credential_ref": row["credential_ref"], "status": row["status"], "config": _load(row["config_json"], {}), "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        with _LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connectors WHERE connector_id = ?",
                (str(connector_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "connector_id": row["connector_id"],
            "connector_type": row["connector_type"],
            "account_label": row["account_label"],
            "credential_ref": row["credential_ref"],
            "status": row["status"],
            "config": _load(row["config_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
