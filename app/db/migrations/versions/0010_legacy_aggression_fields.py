"""add legacy aggression fields for compatibility

Revision ID: 0010_legacy_aggr
Revises: 0009_long_pause_delay
Create Date: 2025-09-18

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_legacy_aggr"
down_revision = "0009_long_pause_delay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("aggression_level", sa.Integer(), nullable=True))
    op.add_column("chat_state", sa.Column("first_aggression_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("last_aggression_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("aggression_count", sa.Integer(), nullable=True))
    op.add_column("chat_state", sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_state", sa.Column("warnings_given", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "warnings_given")
    op.drop_column("chat_state", "blocked_until")
    op.drop_column("chat_state", "aggression_count")
    op.drop_column("chat_state", "last_aggression_at")
    op.drop_column("chat_state", "first_aggression_at")
    op.drop_column("chat_state", "aggression_level")
