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
    # Проверяем, существуют ли колонки перед добавлением
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('chat_state')]
    
    if 'aggression_level' not in columns:
        op.add_column("chat_state", sa.Column("aggression_level", sa.Integer(), nullable=True))
    if 'first_aggression_at' not in columns:
        op.add_column("chat_state", sa.Column("first_aggression_at", sa.DateTime(timezone=True), nullable=True))
    if 'last_aggression_at' not in columns:
        op.add_column("chat_state", sa.Column("last_aggression_at", sa.DateTime(timezone=True), nullable=True))
    if 'aggression_count' not in columns:
        op.add_column("chat_state", sa.Column("aggression_count", sa.Integer(), nullable=True))
    if 'blocked_until' not in columns:
        op.add_column("chat_state", sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True))
    if 'warnings_given' not in columns:
        op.add_column("chat_state", sa.Column("warnings_given", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Проверяем, существуют ли колонки перед удалением
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('chat_state')]
    
    if 'warnings_given' in columns:
        op.drop_column("chat_state", "warnings_given")
    if 'blocked_until' in columns:
        op.drop_column("chat_state", "blocked_until")
    if 'aggression_count' in columns:
        op.drop_column("chat_state", "aggression_count")
    if 'last_aggression_at' in columns:
        op.drop_column("chat_state", "last_aggression_at")
    if 'first_aggression_at' in columns:
        op.drop_column("chat_state", "first_aggression_at")
    if 'aggression_level' in columns:
        op.drop_column("chat_state", "aggression_level")
