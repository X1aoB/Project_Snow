"""Allow the feedback mailer to render the explicitly requested details.

Revision ID: 20260926_0007
Revises: 20260925_0006
"""

from alembic import op


revision = "20260926_0007"
down_revision = "20260925_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the worker away from request context and the rest of the request row.
    # It receives only the user-submitted body and encrypted optional QQ; the
    # corresponding decryption key is mounted separately and never stored in
    # PostgreSQL.
    op.execute(
        "GRANT SELECT (body_text, qq_cipher) "
        "ON TABLE public_feedback TO project_snow_feedback_mailer"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (body_text, qq_cipher) "
        "ON TABLE public_feedback FROM project_snow_feedback_mailer"
    )
