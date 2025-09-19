"""add tg_message_id to messages and assistant_messages

Revision ID: 0013_add_tg_message_id
Revises: 0012_add_tasks_queue
Create Date: 2025-09-20
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0013_add_tg_message_id'
down_revision = '0012_add_tasks_queue'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('messages', sa.Column('tg_message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_messages_tg_message_id', 'messages', ['tg_message_id'])
    op.add_column('assistant_messages', sa.Column('tg_message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_assistant_messages_tg_message_id', 'assistant_messages', ['tg_message_id'])


def downgrade() -> None:
    op.drop_index('ix_messages_tg_message_id', table_name='messages')
    op.drop_column('messages', 'tg_message_id')
    op.drop_index('ix_assistant_messages_tg_message_id', table_name='assistant_messages')
    op.drop_column('assistant_messages', 'tg_message_id')
