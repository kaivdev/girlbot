"""add tasks queue

Revision ID: 0012_add_tasks_queue
Revises: 0011_add_pending_input_buffer
Create Date: 2025-09-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0012_add_tasks_queue'
down_revision = '0011_add_pending_input_buffer'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(length=64), nullable=False, index=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('dedup_key', sa.String(length=128), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Composite index
    op.create_index('ix_tasks_status_priority_created', 'tasks', ['status', 'priority', 'created_at'])
    op.create_index('ix_tasks_lease_expires_at', 'tasks', ['lease_expires_at'])
    op.create_check_constraint('ck_tasks_status', 'tasks', "status IN ('pending','processing','done','failed','cancelled')")


def downgrade() -> None:
    op.drop_constraint('ck_tasks_status', 'tasks', type_='check')
    op.drop_index('ix_tasks_status_priority_created', table_name='tasks')
    op.drop_index('ix_tasks_lease_expires_at', table_name='tasks')
    op.drop_table('tasks')
