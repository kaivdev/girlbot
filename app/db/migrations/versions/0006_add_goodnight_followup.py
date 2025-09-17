"""add last_goodnight_followup_sent_at

Revision ID: 0006_add_goodnight_followup
Revises: 0005_add_sleep_until
Create Date: 2025-09-17 01:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_add_goodnight_followup"
down_revision = "0005_add_sleep_until"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("last_goodnight_followup_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "last_goodnight_followup_sent_at")
