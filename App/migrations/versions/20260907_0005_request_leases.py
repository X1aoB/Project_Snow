"""Add request ownership without changing the v1 request cache contract."""

from alembic import op

revision = "20260907_0005"
down_revision = "20260819_0004"
branch_labels = None
depends_on = None

LEASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_request_leases (
    request_id VARCHAR(128) PRIMARY KEY REFERENCES public_request_cache(request_id) ON DELETE CASCADE,
    owner_token VARCHAR(64) NOT NULL,
    lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_request_lease_expiry_idx ON public_request_leases (lease_expires_at);
"""


def upgrade() -> None:
    for statement in LEASE_SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Application rollback keeps this additive table. Explicit administrative
    # downgrade is safe only after all generation workers have been stopped.
    op.execute("DROP TABLE IF EXISTS public_request_leases")
