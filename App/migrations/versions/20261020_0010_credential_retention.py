"""Add content-free BYOK revocation cutoffs for retention credentials."""

from alembic import op


revision = "20261020_0010"
down_revision = "20261019_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public_credential_revocation (
            subject_hash VARCHAR(64) PRIMARY KEY,
            revoked_before TIMESTAMP WITH TIME ZONE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
    )
    op.execute(
        """CREATE INDEX IF NOT EXISTS public_credential_revocation_expiry_idx
           ON public_credential_revocation (expires_at)"""
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS public_credential_revocation")
