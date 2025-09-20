"""add last_proactive_sent_at

Revision ID: 0010_last_proactive_sent_at
Revises: 0009_long_pause_delay
Create Date: 2025-09-20
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0010_last_proactive_sent_at"
down_revision = "0009_long_pause_delay"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_state", sa.Column("last_proactive_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "last_proactive_sent_at")
