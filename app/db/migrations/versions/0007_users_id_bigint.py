"""widen user id to bigint

Revision ID: 0007_users_id_bigint
Revises: 0006_add_goodnight_followup
Create Date: 2025-09-17 00:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007_users_id_bigint"
down_revision = "0006_add_goodnight_followup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # В PostgreSQL изменение типа с int4 на int8 обычно безопасно
    op.alter_column("users", "id", type_=sa.BigInteger())
    op.alter_column("messages", "user_id", type_=sa.BigInteger())
    op.alter_column("events", "user_id", type_=sa.BigInteger())


def downgrade() -> None:
    # Возможная потеря данных, если id > 2^31-1
    op.alter_column("events", "user_id", type_=sa.Integer())
    op.alter_column("messages", "user_id", type_=sa.Integer())
    op.alter_column("users", "id", type_=sa.Integer())
