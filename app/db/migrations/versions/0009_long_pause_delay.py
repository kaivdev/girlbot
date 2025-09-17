"""add last_long_pause_reply_at

Revision ID: 0009_long_pause_delay
Revises: 0008_proactive_outbox
Create Date: 2025-09-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_long_pause_delay"
down_revision = "0008_proactive_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_state",
        sa.Column("last_long_pause_reply_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_state", "last_long_pause_reply_at")
