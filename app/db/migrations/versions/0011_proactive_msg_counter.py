"""add proactive_user_msg_count_since_last

Revision ID: 0011_proactive_msg_counter
Revises: 0010_last_proactive_sent_at
Create Date: 2025-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_proactive_msg_counter"
down_revision = "0010_last_proactive_sent_at"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_state", sa.Column("proactive_user_msg_count_since_last", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "proactive_user_msg_count_since_last")
