"""Add durable confirmed response blocks without changing the v1 cache."""

from alembic import op

revision = "20261013_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None

# Frozen DDL: historical migrations must not import the current application.
REQUEST_BLOCKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_request_blocks (
    request_id VARCHAR(128) NOT NULL REFERENCES public_request_cache(request_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 0 AND 7),
    block_id VARCHAR(64) NOT NULL,
    block_type VARCHAR(16) NOT NULL CHECK (block_type IN ('message', 'speech', 'action')),
    body_text VARCHAR(2000) NOT NULL CHECK (length(body_text) BETWEEN 1 AND 2000),
    PRIMARY KEY (request_id, sequence),
    UNIQUE (block_id)
);
"""


def upgrade() -> None:
    for statement in REQUEST_BLOCKS_SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Normal application rollback retains this additive table and its receipts.
    # Administrative downgrade requires stopped workers and expired receipts.
    op.execute("DROP TABLE IF EXISTS public_request_blocks")
