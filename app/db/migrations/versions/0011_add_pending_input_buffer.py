"""add pending input buffer fields

Revision ID: 0011_pending_buffer
Revises: 0010_legacy_aggr
Create Date: 2025-09-19

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_pending_buffer"
down_revision = "0010_legacy_aggr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('chat_state')]

    if 'pending_input_json' not in columns:
        op.add_column('chat_state', sa.Column('pending_input_json', sa.dialects.postgresql.JSONB(), nullable=True))
    if 'pending_started_at' not in columns:
        op.add_column('chat_state', sa.Column('pending_started_at', sa.DateTime(timezone=True), nullable=True))
    if 'pending_updated_at' not in columns:
        op.add_column('chat_state', sa.Column('pending_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('chat_state')]

    if 'pending_updated_at' in columns:
        op.drop_column('chat_state', 'pending_updated_at')
    if 'pending_started_at' in columns:
        op.drop_column('chat_state', 'pending_started_at')
    if 'pending_input_json' in columns:
        op.drop_column('chat_state', 'pending_input_json')
