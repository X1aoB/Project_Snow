"""Add the anonymous voice-preference survey table (expand only)."""

from alembic import op


revision = "20261108_0011"
down_revision = "20261020_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public_voice_survey_response (
            subject_hash VARCHAR(64) PRIMARY KEY,
            survey_version VARCHAR(32) NOT NULL,
            answers_json TEXT NOT NULL,
            comment VARCHAR(400) NOT NULL DEFAULT '',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
    )
    op.execute(
        """CREATE INDEX IF NOT EXISTS public_voice_survey_response_expiry_idx
           ON public_voice_survey_response (expires_at)"""
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS public_voice_survey_response")
