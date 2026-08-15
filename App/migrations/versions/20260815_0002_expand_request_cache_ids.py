"""Allow namespaced public idempotency keys.

Revision ID: 20260815_0002
Revises: 20260814_0001
"""

import sqlalchemy as sa
from alembic import op


revision = "20260815_0002"
down_revision = "20260814_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "public_request_cache",
        "request_id",
        existing_type=sa.String(length=36),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("DELETE FROM public_request_cache WHERE length(request_id) > 36")
    op.alter_column(
        "public_request_cache",
        "request_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=36),
        existing_nullable=False,
    )
