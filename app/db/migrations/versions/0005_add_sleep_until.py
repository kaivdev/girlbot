"""add sleep_until to chat_state

Revision ID: 0005_add_sleep_until
Revises: 0004_proactive_extended
Create Date: 2025-09-17 00:30:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_add_sleep_until"
down_revision = "0004_proactive_extended"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("sleep_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "sleep_until")
