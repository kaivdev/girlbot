"""add memory_rev to chat_state

Revision ID: 0003_add_memory_rev
Revises: 0002_add_persona
Create Date: 2025-09-13 00:10:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_add_memory_rev"
down_revision = "0002_add_persona"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("memory_rev", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("chat_state", "memory_rev")

