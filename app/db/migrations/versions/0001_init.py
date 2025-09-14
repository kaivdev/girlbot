"""initial schema

Revision ID: 0001_init
Revises: 
Create Date: 2025-09-12 00:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("lang", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_assistant_messages_chat_id", "assistant_messages", ["chat_id"])

    op.create_table(
        "chat_state",
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("auto_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_user_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_assistant_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_proactive_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_chat_state_next_proactive_at", "chat_state", ["next_proactive_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_index("ix_chat_state_next_proactive_at", table_name="chat_state")
    op.drop_table("chat_state")
    op.drop_index("ix_assistant_messages_chat_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("users")

