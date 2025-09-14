from __future__ import annotations

"""Core reply flow: save, spam-check, call n8n, delay and send."""

import asyncio
from typing import Optional

from aiogram import Bot
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import ChatInfo, Context, MessageIn, N8nRequest
from app.bot.services.anti_spam import is_allowed, remaining_wait_seconds
from app.bot.services.history import fetch_recent_history
from app.bot.services.metrics import metrics
from app.bot.services.n8n_client import call_n8n
from app.config.settings import Settings
from app.db.models import AssistantMessage, Chat, ChatState, Event, Message, User
from app.utils.time import future_with_jitter, utcnow


async def ensure_entities(
    session: AsyncSession,
    *,
    chat_id: int,
    chat_type: str,
    user_id: Optional[int],
    username: Optional[str],
    lang: Optional[str],
    settings: Settings,
) -> None:
    """Ensure Chat, User, and ChatState exist."""

    chat = await session.get(Chat, chat_id)
    if chat is None:
        session.add(Chat(id=chat_id, type=chat_type))

    if user_id is not None:
        user = await session.get(User, user_id)
        if user is None:
            session.add(User(id=user_id, username=username, lang=lang))
        else:
            # update known fields
            user.username = username or user.username
            user.lang = lang or user.lang

    state = await session.get(ChatState, chat_id)
    if state is None:
        session.add(
            ChatState(
                chat_id=chat_id,
                auto_enabled=bool(settings.proactive.default_auto_messages),
            )
        )


async def process_user_text(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    chat_type: str,
    user_id: Optional[int],
    username: Optional[str],
    lang: Optional[str],
    text: str,
    settings: Settings,
    trace_id: Optional[str] = None,
) -> str:
    """Process incoming user text and send a reply. Returns reply text sent."""

    trimmed = text.strip()
    if len(trimmed) > settings.max_user_text_len:
        trimmed = trimmed[: settings.max_user_text_len]

    await ensure_entities(
        session,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        username=username,
        lang=lang,
        settings=settings,
    )

    # Save user message
    session.add(Message(chat_id=chat_id, user_id=user_id, text=trimmed))

    # Update chat state last_user_msg_at
    state = await session.get(ChatState, chat_id)
    now = utcnow()
    if state is None:
        state = ChatState(chat_id=chat_id, auto_enabled=bool(settings.proactive.default_auto_messages))
        session.add(state)
    prev_user_ts = state.last_user_msg_at
    state.last_user_msg_at = now

    metrics.inc("messages_received_total")

    # Anti-spam
    min_gap = settings.antispam.user_min_seconds_between_msg
    if not is_allowed(prev_user_ts, now, min_gap):
        wait = remaining_wait_seconds(prev_user_ts, now, min_gap)
        warn = f"Слишком часто, подождите ещё {wait} c"
        await bot.send_message(chat_id, warn)
        return warn

    # Build n8n request
    history = await fetch_recent_history(
        session, chat_id, limit_pairs=10, persona=getattr(state, "persona_key", None)
    )
    ctx = Context(
        history=history,
        last_user_msg_at=state.last_user_msg_at,
        last_assistant_at=state.last_assistant_at,
    )
    chat_info = ChatInfo(
        chat_id=chat_id,
        user_id=user_id,
        lang=lang,
        username=username,
        persona=getattr(state, "persona_key", None),
        memory_rev=getattr(state, "memory_rev", None),
    )
    req = N8nRequest(intent="reply", chat=chat_info, context=ctx, message=MessageIn(text=trimmed), trace_id=trace_id)

    # Call n8n
    try:
        n8n_resp = await call_n8n(req, trace_id=trace_id)
    except Exception:
        # On error: notify softly and log event
        session.add(
            Event(kind="n8n_error", chat_id=chat_id, user_id=user_id, payload_json={"intent": "reply"})
        )
        msg = "Сервис занят, попробуйте позже"
        await bot.send_message(chat_id, msg)
        metrics.inc("n8n_errors_total", labels={"intent": "reply"})
        return msg

    # Delay before sending
    delay = future_with_jitter(settings.reply_delay.min_seconds, settings.reply_delay.max_seconds)
    delay_seconds = max(0.0, (delay - now).total_seconds())
    metrics.observe("reply_delay_seconds", delay_seconds)
    await asyncio.sleep(delay_seconds)

    # Send reply
    sent_text = n8n_resp.reply
    await bot.send_message(chat_id, sent_text)

    # Persist assistant message and update state
    meta = n8n_resp.meta.model_dump()
    if getattr(state, "persona_key", None):
        meta = {"persona": state.persona_key, **meta}
    session.add(AssistantMessage(chat_id=chat_id, text=sent_text, meta_json=meta))
    state.last_assistant_at = utcnow()
    if state.auto_enabled:
        state.next_proactive_at = future_with_jitter(
            settings.proactive.min_seconds, settings.proactive.max_seconds, base=state.last_assistant_at
        )

    metrics.inc("replies_sent_total")
    return sent_text
