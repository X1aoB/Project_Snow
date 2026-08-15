"""PostgreSQL-backed anonymous limits, idempotency and feedback storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
import unicodedata
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool


class PublicStoreUnavailable(RuntimeError):
    pass


class RateLimitExceeded(RuntimeError):
    def __init__(self, scope: str, limit: int):
        super().__init__(scope)
        self.scope = scope
        self.limit = limit


class DuplicateFeedback(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalized_text_for_hash(value: str) -> str:
    """Use the same Unicode equivalence class for feedback de-duplication."""

    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_rate_limit (
    subject_hash VARCHAR(64) NOT NULL,
    scope VARCHAR(40) NOT NULL,
    bucket_start VARCHAR(32) NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, scope, bucket_start)
);
CREATE INDEX IF NOT EXISTS public_rate_limit_expiry_idx ON public_rate_limit (expires_at);

CREATE TABLE IF NOT EXISTS public_request_cache (
    request_id VARCHAR(36) PRIMARY KEY,
    subject_hash VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('processing', 'completed')),
    response_json TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_request_expiry_idx ON public_request_cache (expires_at);

CREATE TABLE IF NOT EXISTS public_verification (
    subject_hash VARCHAR(64) NOT NULL,
    purpose VARCHAR(24) NOT NULL,
    verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, purpose)
);

CREATE TABLE IF NOT EXISTS public_feedback_attempt (
    attempt_id VARCHAR(36) PRIMARY KEY,
    subject_hash VARCHAR(64) NOT NULL,
    ip_fingerprint VARCHAR(64) NOT NULL,
    attempted_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_feedback_attempt_subject_idx
    ON public_feedback_attempt (subject_hash, attempted_at);
CREATE INDEX IF NOT EXISTS public_feedback_attempt_ip_idx
    ON public_feedback_attempt (ip_fingerprint, attempted_at);

CREATE TABLE IF NOT EXISTS public_feedback (
    feedback_id VARCHAR(36) PRIMARY KEY,
    public_code VARCHAR(40) NOT NULL UNIQUE,
    subject_hash VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    body_text VARCHAR(1000) NOT NULL CHECK (length(body_text) BETWEEN 1 AND 1000),
    context_json TEXT NOT NULL,
    qq_cipher TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_feedback_created_idx ON public_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS public_feedback_expiry_idx ON public_feedback (expires_at);
CREATE INDEX IF NOT EXISTS public_feedback_subject_dedupe_idx
    ON public_feedback (subject_hash, content_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS public_feedback_dedupe (
    dedupe_id VARCHAR(64) PRIMARY KEY,
    subject_hash VARCHAR(64) NOT NULL,
    ip_fingerprint VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_feedback_dedupe_subject_idx
    ON public_feedback_dedupe (subject_hash, content_hash, expires_at);
CREATE INDEX IF NOT EXISTS public_feedback_dedupe_ip_idx
    ON public_feedback_dedupe (ip_fingerprint, content_hash, expires_at);
"""


class PublicStore:
    def __init__(self, database_url: str, *, engine: Engine | None = None):
        self.database_url = database_url
        if engine is not None:
            self.engine = engine
        elif not database_url:
            self.engine = None
        elif database_url.startswith("sqlite"):
            # Tests use one in-memory database across request threads. This is
            # not a production path; production always uses PostgreSQL.
            self.engine = create_engine(
                database_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self.engine = create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        if self.engine is None:
            raise PublicStoreUnavailable("PUBLIC_DATABASE_URL is not configured")
        try:
            with self.engine.begin() as connection:
                yield connection
        except (RateLimitExceeded, DuplicateFeedback):
            raise
        except Exception as exc:
            raise PublicStoreUnavailable("public database operation failed") from exc

    def create_schema(self) -> None:
        with self.begin() as connection:
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    connection.execute(text(statement))

    def health(self) -> bool:
        try:
            with self.begin() as connection:
                return connection.execute(text("SELECT 1")).scalar_one() == 1
        except PublicStoreUnavailable:
            return False

    @staticmethod
    def _bucket(period: str, now: datetime) -> tuple[str, datetime]:
        if period == "hour":
            start = now.replace(minute=0, second=0, microsecond=0)
            return start.isoformat(), start + timedelta(hours=2)
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start.isoformat(), start + timedelta(days=2)
        raise ValueError(f"unsupported rate period: {period}")

    def consume_limits(
        self,
        subject_hash: str,
        limits: list[tuple[str, str, int]],
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        now = now or _utcnow()
        counts: dict[str, int] = {}
        with self.begin() as connection:
            for scope, period, limit in limits:
                bucket_start, expires_at = self._bucket(period, now)
                count = connection.execute(
                    text(
                        """
                        INSERT INTO public_rate_limit
                            (subject_hash, scope, bucket_start, count, expires_at)
                        VALUES (:subject_hash, :scope, :bucket_start, 1, :expires_at)
                        ON CONFLICT (subject_hash, scope, bucket_start)
                        DO UPDATE SET count = public_rate_limit.count + 1
                        RETURNING count
                        """
                    ),
                    {
                        "subject_hash": subject_hash,
                        "scope": scope,
                        "bucket_start": bucket_start,
                        "expires_at": expires_at,
                    },
                ).scalar_one()
                if int(count) > limit:
                    # The failed attempt must not consume quota.  Raising from
                    # inside this transaction rolls back every increment made
                    # for this request, including any earlier hour/day bucket.
                    raise RateLimitExceeded(scope, limit)
                counts[scope] = int(count)
        return counts

    def claim_request(self, request_id: str, subject_hash: str, request_hash: str) -> tuple[str, dict | None]:
        now = _utcnow()
        expires_at = now + timedelta(minutes=10)
        with self.begin() as connection:
            connection.execute(
                text("DELETE FROM public_request_cache WHERE expires_at <= :now"), {"now": now}
            )
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO public_request_cache
                        (request_id, subject_hash, request_hash, status, response_json, created_at, expires_at)
                    VALUES (:request_id, :subject_hash, :request_hash, 'processing', NULL, :now, :expires_at)
                    ON CONFLICT (request_id) DO NOTHING
                    RETURNING request_id
                    """
                ),
                {
                    "request_id": request_id,
                    "subject_hash": subject_hash,
                    "request_hash": request_hash,
                    "now": now,
                    "expires_at": expires_at,
                },
            ).first()
            if inserted:
                return "claimed", None
            existing = connection.execute(
                text(
                    """
                    SELECT subject_hash, request_hash, status, response_json
                    FROM public_request_cache WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id},
            ).mappings().one()
            if existing["subject_hash"] != subject_hash or existing["request_hash"] != request_hash:
                return "conflict", None
            if existing["status"] == "completed" and existing["response_json"]:
                return "completed", json.loads(existing["response_json"])
            return "processing", None

    def complete_request(self, request_id: str, response: dict[str, Any]) -> None:
        with self.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE public_request_cache
                    SET status = 'completed', response_json = :response_json
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id, "response_json": _json(response)},
            )

    def release_request(self, request_id: str) -> None:
        with self.begin() as connection:
            connection.execute(
                text("DELETE FROM public_request_cache WHERE request_id = :request_id AND status = 'processing'"),
                {"request_id": request_id},
            )

    def request_result(self, request_id: str, subject_hash: str) -> dict[str, Any] | None:
        """Return a still-live terminal result owned by this anonymous subject."""

        if not request_id:
            return None
        now = _utcnow()
        with self.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT response_json FROM public_request_cache
                    WHERE request_id = :request_id
                      AND subject_hash = :subject_hash
                      AND status = 'completed'
                      AND expires_at > :now
                    """
                ),
                {"request_id": request_id, "subject_hash": subject_hash, "now": now},
            ).mappings().first()
        if not row or not row["response_json"]:
            return None
        try:
            payload = json.loads(row["response_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def verification_required(self, subject_hash: str, purpose: str) -> bool:
        now = _utcnow()
        with self.begin() as connection:
            value = connection.execute(
                text(
                    """
                    SELECT 1 FROM public_verification
                    WHERE subject_hash = :subject_hash AND purpose = :purpose AND expires_at > :now
                    """
                ),
                {"subject_hash": subject_hash, "purpose": purpose, "now": now},
            ).first()
        return value is None

    def mark_verified(self, subject_hash: str, purpose: str, lifetime: timedelta = timedelta(days=30)) -> None:
        now = _utcnow()
        with self.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO public_verification (subject_hash, purpose, verified_at, expires_at)
                    VALUES (:subject_hash, :purpose, :verified_at, :expires_at)
                    ON CONFLICT (subject_hash, purpose)
                    DO UPDATE SET verified_at = excluded.verified_at, expires_at = excluded.expires_at
                    """
                ),
                {
                    "subject_hash": subject_hash,
                    "purpose": purpose,
                    "verified_at": now,
                    "expires_at": now + lifetime,
                },
            )

    def record_feedback_attempt(self, subject_hash: str, ip_fingerprint: str) -> dict[str, int]:
        now = _utcnow()
        subject_cutoff = now - timedelta(minutes=10)
        day_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO public_feedback_attempt
                        (attempt_id, subject_hash, ip_fingerprint, attempted_at)
                    VALUES (:attempt_id, :subject_hash, :ip_fingerprint, :attempted_at)
                    """
                ),
                {
                    "attempt_id": str(secrets.token_hex(16)),
                    "subject_hash": subject_hash,
                    "ip_fingerprint": ip_fingerprint,
                    "attempted_at": now,
                },
            )
            subject_attempts = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*) FROM public_feedback_attempt
                        WHERE subject_hash = :subject_hash AND attempted_at >= :cutoff
                        """
                    ),
                    {"subject_hash": subject_hash, "cutoff": subject_cutoff},
                ).scalar_one()
            )
            identities = int(
                connection.execute(
                    text(
                        """
                        SELECT count(DISTINCT subject_hash) FROM public_feedback_attempt
                        WHERE ip_fingerprint = :ip_fingerprint AND attempted_at >= :cutoff
                        """
                    ),
                    {"ip_fingerprint": ip_fingerprint, "cutoff": day_cutoff},
                ).scalar_one()
            )
        return {"subject_attempts": subject_attempts, "ip_identities": identities}

    def insert_feedback(
        self,
        *,
        subject_hash: str,
        ip_fingerprint: str,
        body_text: str,
        context: dict[str, Any],
        qq_cipher: str | None,
    ) -> str:
        now = _utcnow()
        content_hash = hashlib.sha256(normalized_text_for_hash(body_text).encode()).hexdigest()
        public_code = "snow-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "")[:16]
        feedback_id = secrets.token_hex(16)
        with self.begin() as connection:
            try:
                if connection.dialect.name == "postgresql":
                    # Serialize equivalent feedback checks without retaining
                    # a raw address or relying on a permanent unique index.
                    lock_digest = hashlib.sha256(
                        f"{subject_hash}\x1f{ip_fingerprint}\x1f{content_hash}".encode()
                    ).digest()[:8]
                    lock_key = int.from_bytes(lock_digest, "big", signed=True)
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                duplicate = connection.execute(
                    text(
                        """
                        SELECT 1 FROM public_feedback_dedupe
                        WHERE expires_at > :now
                          AND content_hash = :content_hash
                          AND (subject_hash = :subject_hash OR ip_fingerprint = :ip_fingerprint)
                        LIMIT 1
                        """
                    ),
                    {
                        "now": now,
                        "content_hash": content_hash,
                        "subject_hash": subject_hash,
                        "ip_fingerprint": ip_fingerprint,
                    },
                ).first()
                if duplicate:
                    raise DuplicateFeedback("duplicate feedback")
                connection.execute(
                    text(
                        """
                        INSERT INTO public_feedback
                            (feedback_id, public_code, subject_hash, content_hash,
                             body_text, context_json, qq_cipher, created_at, expires_at)
                        VALUES
                            (:feedback_id, :public_code, :subject_hash, :content_hash,
                             :body_text, :context_json, :qq_cipher, :created_at, :expires_at)
                        """
                    ),
                    {
                        "feedback_id": feedback_id,
                        "public_code": public_code,
                        "subject_hash": subject_hash,
                        "content_hash": content_hash,
                        "body_text": body_text,
                        "context_json": _json(context),
                        "qq_cipher": qq_cipher,
                        "created_at": now,
                        "expires_at": now + timedelta(days=30),
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO public_feedback_dedupe
                            (dedupe_id, subject_hash, ip_fingerprint, content_hash, created_at, expires_at)
                        VALUES
                            (:dedupe_id, :subject_hash, :ip_fingerprint, :content_hash, :created_at, :expires_at)
                        """
                    ),
                    {
                        "dedupe_id": secrets.token_hex(32),
                        "subject_hash": subject_hash,
                        "ip_fingerprint": ip_fingerprint,
                        "content_hash": content_hash,
                        "created_at": now,
                        "expires_at": now + timedelta(hours=24),
                    },
                )
            except DuplicateFeedback:
                raise
            except Exception as exc:
                message = str(exc).casefold()
                if "unique" in message or "duplicate" in message:
                    raise DuplicateFeedback("duplicate feedback") from exc
                raise
        return public_code

    def feedback_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT feedback_id, public_code, body_text, context_json, qq_cipher, created_at, expires_at
                    FROM public_feedback ORDER BY created_at DESC LIMIT :limit
                    """
                ),
                {"limit": max(1, min(limit, 500))},
            ).mappings().all()
        return [
            {
                **dict(row),
                "context": json.loads(row["context_json"]),
                "context_json": None,
                "has_qq": bool(row["qq_cipher"]),
                "qq_cipher": row["qq_cipher"],
            }
            for row in rows
        ]

    def cleanup(self) -> dict[str, int]:
        now = _utcnow()
        deleted: dict[str, int] = {}
        with self.begin() as connection:
            for table, column in (
                ("public_feedback", "expires_at"),
                ("public_request_cache", "expires_at"),
                ("public_rate_limit", "expires_at"),
                ("public_verification", "expires_at"),
                ("public_feedback_dedupe", "expires_at"),
            ):
                result = connection.execute(text(f"DELETE FROM {table} WHERE {column} <= :now"), {"now": now})
                deleted[table] = max(0, int(result.rowcount or 0))
            result = connection.execute(
                text("DELETE FROM public_feedback_attempt WHERE attempted_at <= :cutoff"),
                {"cutoff": now.replace(hour=0, minute=0, second=0, microsecond=0)},
            )
            deleted["public_feedback_attempt"] = max(0, int(result.rowcount or 0))
        return deleted
