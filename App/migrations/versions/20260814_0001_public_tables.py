"""Create registration-free public API tables.

Revision ID: 20260814_0001
Revises:
"""
from alembic import op

from backend.snow_app.public_store import SCHEMA_SQL


revision = "20260814_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in (
        "public_feedback_dedupe",
        "public_feedback",
        "public_feedback_attempt",
        "public_verification",
        "public_request_cache",
        "public_rate_limit",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
