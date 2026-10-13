"""Add append-only resolution markers for public feedback.

Revision ID: 20260925_0006
Revises: 20260907_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None


_STATUSES = (
    "'pending_triage', 'planned', 'resolved', 'ignored', "
    "'open', 'needs_verification', 'fixed_verified', "
    "'not_reproduced', 'duplicate', 'superseded_by_architecture'"
)


def upgrade() -> None:
    op.create_table(
        "public_feedback_triage",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.String(length=36),
            sa.ForeignKey("public_feedback.feedback_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_STATUSES})",
            name="public_feedback_triage_status_ck",
        ),
    )
    op.create_index(
        "public_feedback_triage_feedback_idx",
        "public_feedback_triage",
        ["feedback_id", "created_at", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("public_feedback_triage_feedback_idx", table_name="public_feedback_triage")
    op.drop_table("public_feedback_triage")
