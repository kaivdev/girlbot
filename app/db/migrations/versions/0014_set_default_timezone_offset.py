"""Set default timezone offset to 180 minutes (Moscow)

Revision ID: 0014_set_default_timezone_offset
Revises: 0013_add_tg_message_id
Create Date: 2025-09-20
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0014_set_default_timezone_offset"
down_revision = "0013_add_tg_message_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Update existing NULLs to 180
    op.execute("UPDATE chat_state SET timezone_offset_minutes = 180 WHERE timezone_offset_minutes IS NULL")
    # Set server default to 180 (only if column exists)
    with op.batch_alter_table("chat_state") as batch_op:
        batch_op.alter_column(
            "timezone_offset_minutes",
            existing_type=sa.Integer(),
            nullable=True,
            server_default="180",
        )


def downgrade() -> None:
    # Remove server default (revert to NULL default)
    with op.batch_alter_table("chat_state") as batch_op:
        batch_op.alter_column(
            "timezone_offset_minutes",
            existing_type=sa.Integer(),
            nullable=True,
            server_default=None,
        )
