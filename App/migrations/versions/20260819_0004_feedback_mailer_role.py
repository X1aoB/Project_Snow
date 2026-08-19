"""Restrict the SMTP feedback worker to receipt and outbox fields.

Revision ID: 20260819_0004
Revises: 20260816_0003
"""

from alembic import op


revision = "20260819_0004"
down_revision = "20260816_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'project_snow_feedback_mailer') THEN
            CREATE ROLE project_snow_feedback_mailer NOLOGIN;
          END IF;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON TABLE public_feedback FROM project_snow_feedback_mailer")
    op.execute("REVOKE ALL ON TABLE public_feedback_email_outbox FROM project_snow_feedback_mailer")
    op.execute("GRANT USAGE ON SCHEMA public TO project_snow_feedback_mailer")
    op.execute(
        "GRANT SELECT (feedback_id, public_code, created_at, expires_at) "
        "ON TABLE public_feedback TO project_snow_feedback_mailer"
    )
    op.execute(
        "GRANT SELECT (outbox_id, feedback_id, status, attempt_count, next_attempt_at, locked_until) "
        "ON TABLE public_feedback_email_outbox TO project_snow_feedback_mailer"
    )
    op.execute(
        "GRANT UPDATE (status, attempt_count, next_attempt_at, locked_until, last_error_code, sent_at) "
        "ON TABLE public_feedback_email_outbox TO project_snow_feedback_mailer"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public_feedback_email_outbox FROM project_snow_feedback_mailer")
    op.execute("REVOKE ALL ON TABLE public_feedback FROM project_snow_feedback_mailer")
    op.execute("REVOKE USAGE ON SCHEMA public FROM project_snow_feedback_mailer")
