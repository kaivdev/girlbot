from __future__ import annotations

"""APScheduler integration for proactive messaging."""

from datetime import datetime
from typing import Callable, Optional, AsyncContextManager

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import ChatInfo, Context, N8nRequest
from app.bot.services.history import fetch_recent_history
from app.bot.services.metrics import metrics
from app.bot.services.n8n_client import call_n8n
from app.config.settings import Settings
from app.db.models import AssistantMessage, ChatState, Event
from app.utils.time import future_with_jitter, utcnow


def compute_next_proactive_at(now: datetime, settings: Settings) -> datetime:
    return future_with_jitter(settings.proactive.min_seconds, settings.proactive.max_seconds, base=now)


async def process_due_chats(session: AsyncSession, bot: Bot, settings: Settings) -> None:
    now = utcnow()
    q = select(ChatState).where(
        ChatState.auto_enabled.is_(True), ChatState.next_proactive_at.is_not(None), ChatState.next_proactive_at <= now
    )
    result = await session.execute(q)
    states = list(result.scalars().all())
    for state in states:
        # Skip if persona not selected yet
        if not getattr(state, "persona_key", None):
            continue
        chat_id = state.chat_id
        # Build context
        history = await fetch_recent_history(session, chat_id, limit_pairs=10, persona=state.persona_key)
        ctx = Context(history=history, last_user_msg_at=state.last_user_msg_at, last_assistant_at=state.last_assistant_at)
        chat_info = ChatInfo(chat_id=chat_id, user_id=None, persona=state.persona_key, memory_rev=state.memory_rev)
        req = N8nRequest(intent="proactive", chat=chat_info, context=ctx)
        try:
            resp = await call_n8n(req)
        except Exception:
            session.add(Event(kind="n8n_error", chat_id=chat_id, user_id=None, payload_json={"intent": "proactive"}))
            metrics.inc("n8n_errors_total", labels={"intent": "proactive"})
            # postpone next attempt
            state.next_proactive_at = compute_next_proactive_at(now, settings)
            continue

        await bot.send_message(chat_id, resp.reply)
        session.add(AssistantMessage(chat_id=chat_id, text=resp.reply, meta_json=resp.meta.model_dump()))
        state.last_assistant_at = utcnow()
        state.next_proactive_at = compute_next_proactive_at(state.last_assistant_at, settings)
        metrics.inc("proactive_sent_total")


def start_scheduler(
    session_context: Callable[[], AsyncContextManager[AsyncSession]], bot: Bot, settings: Settings
) -> AsyncIOScheduler:
    """Start AsyncIO scheduler to run due proactive job every 60 seconds."""

    scheduler = AsyncIOScheduler()

    async def job_wrapper() -> None:
        async with session_context() as session:  # type: ignore[arg-type]
            await process_due_chats(session, bot, settings)

    scheduler.add_job(job_wrapper, "interval", seconds=60, id="proactive_due_check", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
