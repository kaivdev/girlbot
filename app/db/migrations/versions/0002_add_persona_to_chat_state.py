"""add persona_key to chat_state

Revision ID: 0002_add_persona
Revises: 0001_init
Create Date: 2025-09-13 00:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002_add_persona"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("persona_key", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_state", "persona_key")

