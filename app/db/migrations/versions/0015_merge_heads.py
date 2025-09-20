"""Merge heads: unify proactive counter branch and main lineage

Revision ID: 0015_merge_heads
Revises: 0014_set_default_timezone_offset, 0011_proactive_msg_counter
Create Date: 2025-09-20
"""
from __future__ import annotations

# Alembic directives
revision = "0015_merge_heads"
down_revision = ("0014_set_default_timezone_offset", "0011_proactive_msg_counter")
branch_labels = None
depends_on = None


def upgrade() -> None:  # noqa: D401
    """No-op merge upgrade."""
    pass


def downgrade() -> None:  # noqa: D401
    """No-op merge downgrade (would re-split branches)."""
    pass
