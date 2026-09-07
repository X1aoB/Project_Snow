"""Create registration-free public API tables.

Revision ID: 20260814_0001
Revises:
"""

from alembic import op

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
    request_id VARCHAR(128) PRIMARY KEY,
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


revision = "20260814_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in (
        "public_feedback_dedupe",
        "public_feedback",
        "public_feedback_attempt",
        "public_verification",
        "public_request_cache",
        "public_rate_limit",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
