"""add proactive extended fields

Revision ID: 0004_proactive_extended
Revises: 0003_add_memory_rev
Create Date: 2025-09-17 00:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004_proactive_extended"
down_revision = "0003_add_memory_rev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("last_morning_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("last_goodnight_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("last_reengage_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("timezone_offset_minutes", sa.Integer(), nullable=True))



def downgrade() -> None:
    op.drop_column("chat_state", "timezone_offset_minutes")
    op.drop_column("chat_state", "last_reengage_sent_at")
    op.drop_column("chat_state", "last_goodnight_sent_at")
    op.drop_column("chat_state", "last_morning_sent_at")
