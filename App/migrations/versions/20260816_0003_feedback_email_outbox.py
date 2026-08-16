"""Queue feedback notifications without storing QQ in plaintext.

Revision ID: 20260816_0003
Revises: 20260815_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260816_0003"
down_revision = "20260815_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_feedback_email_outbox",
        sa.Column("outbox_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.String(length=36),
            sa.ForeignKey("public_feedback.feedback_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'sending', 'retry', 'sent')", name="public_feedback_email_status_ck"),
        sa.CheckConstraint("attempt_count >= 0", name="public_feedback_email_attempt_ck"),
    )
    op.create_index(
        "public_feedback_email_outbox_due_idx",
        "public_feedback_email_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("public_feedback_email_outbox_due_idx", table_name="public_feedback_email_outbox")
    op.drop_table("public_feedback_email_outbox")
