"""proactive outbox and chat_state flag

Revision ID: 0008_proactive_outbox
Revises: 0007_users_id_bigint
Create Date: 2025-09-17 00:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0008_proactive_outbox"
down_revision = "0007_users_id_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_state", sa.Column("proactive_via_userbot", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    op.create_table(
        "proactive_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        # explicit index created below, so no index=True here to avoid duplicate op
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_proactive_outbox_chat_id", "proactive_outbox", ["chat_id"])    


def downgrade() -> None:
    op.drop_index("ix_proactive_outbox_chat_id", table_name="proactive_outbox")
    op.drop_table("proactive_outbox")
    op.drop_column("chat_state", "proactive_via_userbot")
