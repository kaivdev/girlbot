from __future__ import annotations

"""History retrieval helpers for n8n context."""

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import HistoryItem
from app.db.models import AssistantMessage, Message


async def fetch_recent_history(
    session: AsyncSession,
    chat_id: int,
    *,
    limit_pairs: int = 10,
    persona: str | None = None,
    soft_char_limit: int | None = None,
    soft_head: int = 4000,
    soft_tail: int = 2000,
) -> list[HistoryItem]:
    """Return ordered recent history.

    - Collects recent user & assistant messages (assistant filtered by persona ONLY if meta has persona key).
    - Trims to last 2*limit_pairs records.
    - Optional soft char trimming: if total text length > soft_char_limit -> keep head & tail windows.
    """

    user_stmt = (
        select(Message.id, Message.text, Message.created_at)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.desc())
        .limit(limit_pairs * 4)  # fetch extra, we'll slice later
    )
    assistant_stmt = (
        select(AssistantMessage.id, AssistantMessage.text, AssistantMessage.created_at, AssistantMessage.meta_json)
        .where(AssistantMessage.chat_id == chat_id)
        .order_by(AssistantMessage.created_at.desc())
        .limit(limit_pairs * 8)
    )

    user_rows = (await session.execute(user_stmt)).all()
    assistant_rows = (await session.execute(assistant_stmt)).all()

    combined: list[tuple[str, str, datetime]] = []
    for _, text, created_at in user_rows:
        combined.append(("user", text, created_at))
    for _, text, created_at, meta in assistant_rows:
        if persona and isinstance(meta, dict) and ("persona" in meta):
            if meta.get("persona") != persona:
                continue
        combined.append(("assistant", text, created_at))

    combined.sort(key=lambda x: x[2])
    # Keep only the last 2*limit_pairs items
    max_items = limit_pairs * 2
    combined = combined[-max_items:]

    items = [HistoryItem(role=role, text=text, created_at=created_at) for role, text, created_at in combined]

    # De-duplicate consecutive identical (role,text) pairs that can appear if
    # an aggregated buffered message and its original fragments both persisted.
    deduped: list[HistoryItem] = []
    for it in items:
        if deduped and deduped[-1].role == it.role and deduped[-1].text == it.text:
            continue
        deduped.append(it)
    items = deduped

    if soft_char_limit and soft_char_limit > 0:
        total = sum(len(it.text) for it in items)
        if total > soft_char_limit and len(items) > 2:
            # Flatten texts, keep earliest & latest windows of items list, not splitting individual messages.
            # Heuristic: trim middle messages while preserving sequence.
            acc = 0
            head_idx = 0
            while head_idx < len(items) and acc < soft_head:
                acc += len(items[head_idx].text)
                head_idx += 1
            acc_tail = 0
            tail_idx = len(items) - 1
            while tail_idx >= 0 and acc_tail < soft_tail:
                acc_tail += len(items[tail_idx].text)
                tail_idx -= 1
            # Ensure we don't overlap
            if tail_idx < head_idx:
                # Degenerate overlap: just keep ends without trimming
                return items
            trimmed = items[:head_idx] + items[tail_idx + 1 :]
            return trimmed
    return items
