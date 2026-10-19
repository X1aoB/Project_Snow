"""Add isolated rendezvous authority and daily paid-attempt ledger.

Application rollback retains these content-free rows. Contract only after all
new workers are stopped and their retention has expired.
"""
from alembic import op

revision = "20261019_0009"
down_revision = "20261013_0008"
branch_labels = None
depends_on = None

PROACTIVE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_rendezvous_authority (
    subject_hash VARCHAR(64) NOT NULL,
    rendezvous_id VARCHAR(160) NOT NULL,
    character_id VARCHAR(32) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    world_revision INTEGER NOT NULL CHECK (world_revision >= 0),
    closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0,1)),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, rendezvous_id)
);
CREATE INDEX IF NOT EXISTS public_rendezvous_authority_expiry_idx
    ON public_rendezvous_authority (expires_at);
CREATE TABLE IF NOT EXISTS public_proactive_day (
    subject_hash VARCHAR(64) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 3),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, schedule_date)
);
CREATE TABLE IF NOT EXISTS public_proactive_attempt (
    request_id VARCHAR(128) PRIMARY KEY,
    subject_hash VARCHAR(64) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    rendezvous_id VARCHAR(160) NOT NULL,
    character_id VARCHAR(32) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('processing','completed','failed','unknown')),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE (subject_hash, rendezvous_id)
);
CREATE INDEX IF NOT EXISTS public_proactive_attempt_day_idx
    ON public_proactive_attempt (subject_hash, schedule_date);
CREATE INDEX IF NOT EXISTS public_proactive_attempt_expiry_idx
    ON public_proactive_attempt (expires_at);
"""


def upgrade():
    for statement in PROACTIVE_SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    op.execute("DROP TABLE IF EXISTS public_proactive_attempt")
    op.execute("DROP TABLE IF EXISTS public_proactive_day")
    op.execute("DROP TABLE IF EXISTS public_rendezvous_authority")
