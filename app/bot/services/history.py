from __future__ import annotations

"""History retrieval helpers for n8n context."""

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import HistoryItem
from app.db.models import AssistantMessage, Message


async def fetch_recent_history(
    session: AsyncSession, chat_id: int, *, limit_pairs: int = 10, persona: str | None = None
) -> list[HistoryItem]:
    """Return up to 2*limit_pairs recent history items (user/assistant roles).

    Items are sorted ascending by time for consumption in LLM contexts.
    """

    user_stmt = (
        select(Message.id, Message.text, Message.created_at)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.desc())
        .limit(limit_pairs * 2)
    )
    assistant_stmt = (
        select(AssistantMessage.id, AssistantMessage.text, AssistantMessage.created_at, AssistantMessage.meta_json)
        .where(AssistantMessage.chat_id == chat_id)
        .order_by(AssistantMessage.created_at.desc())
        .limit(limit_pairs * 6)
    )

    user_rows = (await session.execute(user_stmt)).all()
    assistant_rows = (await session.execute(assistant_stmt)).all()

    combined: list[tuple[str, str, datetime]] = []
    for _, text, created_at in user_rows:
        combined.append(("user", text, created_at))
    for _, text, created_at, meta in assistant_rows:
        if persona:
            # include only assistant messages that explicitly match current persona
            if not isinstance(meta, dict) or meta.get("persona") != persona:
                continue
        combined.append(("assistant", text, created_at))

    combined.sort(key=lambda x: x[2])
    # Keep only the last 2*limit_pairs items
    combined = combined[-(limit_pairs * 2) :]

    return [HistoryItem(role=role, text=text, created_at=created_at) for role, text, created_at in combined]
