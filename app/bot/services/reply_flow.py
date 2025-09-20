from __future__ import annotations

"""Core reply flow: save, spam-check, call n8n, delay and send."""

import asyncio
from typing import Optional
import os

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import ChatInfo, Context, MessageIn, N8nRequest
from app.bot.services.anti_spam import is_allowed, remaining_wait_seconds
from app.bot.services.history import fetch_recent_history
from app.bot.services.metrics import metrics
from app.bot.services.n8n_client import call_n8n, N8NServerError, N8NClientError
from app.bot.services.logging import get_logger
from app.config.settings import Settings
from app.db.models import AssistantMessage, Chat, ChatState, Event, Message, User
from app.utils.time import future_with_jitter, utcnow
from datetime import timedelta
import random
import re
from sqlalchemy import select
from app.db.base import session_scope

# Параметры debounce (могут быть вынесены в настройки позже)
DEBOUNCE_INITIAL_SECONDS = 10  # первое окно после первого входящего
DEBOUNCE_EXTENSION_SECONDS = 6  # продление окна после каждого нового фрагмента
DEBOUNCE_ABSOLUTE_MAX_SECONDS = 30  # страховка от бесконечного удержания

# Реестр фоновых задач авто-флаша буфера: chat_id -> Task
_buffer_flush_tasks: dict[int, asyncio.Task] = {}

def _cancel_existing_flush(chat_id: int):
    t = _buffer_flush_tasks.get(chat_id)
    if t and not t.done():
        t.cancel()
    _buffer_flush_tasks.pop(chat_id, None)

def _schedule_flush_task(bot: Bot, chat_id: int, deadline_at_iso: str | None, settings: Settings, trace_id: str | None):
    from datetime import datetime as _dt
    if not deadline_at_iso:
        return
    try:
        deadline = _dt.fromisoformat(deadline_at_iso)
    except Exception:
        return
    wait = (deadline - utcnow()).total_seconds()
    if wait < 0:
        wait = 0.05

    async def _task():  # pragma: no cover (фоновая логика)
        try:
            await asyncio.sleep(wait)
            async with session_scope() as sess:
                state = await sess.get(ChatState, chat_id)
                if not state or not state.pending_input_json:
                    return
                pj = state.pending_input_json
                # Проверим что сроки действительно истекли
                da = pj.get('deadline_at')
                aa = pj.get('absolute_deadline_at')
                def _parse(s):
                    try:
                        return _dt.fromisoformat(s) if s else None
                    except Exception:
                        return None
                da_dt = _parse(da)
                aa_dt = _parse(aa)
                now_local = utcnow()
                if (da_dt and now_local >= da_dt) or (aa_dt and now_local >= aa_dt):
                    # Выполняем flush
                    try:
                        await flush_pending_input(bot, sess, chat_id=chat_id, settings=settings, trace_id=trace_id)
                    except Exception:
                        pass
        finally:
            # Очистим запись о задаче если она наша
            existing = _buffer_flush_tasks.get(chat_id)
            if existing is asyncio.current_task():
                _buffer_flush_tasks.pop(chat_id, None)

    _cancel_existing_flush(chat_id)
    _buffer_flush_tasks[chat_id] = asyncio.create_task(_task())

async def flush_pending_input(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    settings: Settings,
    trace_id: str | None = None,
) -> Optional[str]:
    state = await session.get(ChatState, chat_id)
    if not state or not getattr(state, 'pending_input_json', None):
        return None
    payload = state.pending_input_json or {}
    # Guard against concurrent double flush (background timer + manual) by marking and early exit
    if payload.get('_flushing'):
        return None
    payload['_flushing'] = True
    state.pending_input_json = payload
    await session.flush()
    text = (payload.get('text') or '').strip()
    media = payload.get('media') or None
    user_id = payload.get('user_id')
    username = payload.get('username')
    lang = payload.get('lang')
    chat_type = payload.get('chat_type') or 'private'
    # Очистим буфер перед фактической отправкой (чтобы повторные события не дублировались)
    state.pending_input_json = None
    state.pending_started_at = None
    state.pending_updated_at = None
    # Flush cleared buffer so parallel sessions do not see stale pending_input_json
    await session.flush()
    # Сохранение и обработка как обычного текста
    return await process_user_text(
        bot,
        session,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        username=username,
        lang=lang,
        text=text,
        media=media,
        settings=settings,
        trace_id=trace_id,
    )

async def flush_expired_pending_input(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    settings: Settings,
    trace_id: str | None = None,
) -> bool:
    """Проверяет дедлайны буфера и делает flush ТОЛЬКО если они истекли.

    Возвращает True если был выполнен flush, иначе False.
    """
    state = await session.get(ChatState, chat_id)
    if not state or not getattr(state, 'pending_input_json', None):
        return False
    payload = state.pending_input_json or {}
    from datetime import datetime as _dt
    def _parse(s: str | None):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except Exception:
            return None
    now_local = utcnow()
    da = _parse(payload.get('deadline_at'))
    aa = _parse(payload.get('absolute_deadline_at'))
    if (aa and now_local >= aa) or (da and now_local >= da):
        await flush_pending_input(bot, session, chat_id=chat_id, settings=settings, trace_id=trace_id)
        return True
    return False


async def buffer_or_process(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    chat_type: str,
    user_id: Optional[int],
    username: Optional[str],
    lang: Optional[str],
    text: str,
    media: dict | None,
    settings: Settings,
    trace_id: Optional[str] = None,
) -> str:
    """Агрегирует серию сообщений пользователя (фото + последующие тексты) в одно.

    Правила:
    - Если нет активного буфера: создаём (pending_input_json) и ждём до DEBOUNCE_INITIAL_SECONDS.
    - Каждое новое сообщение в окне продлевает дедлайн на DEBOUNCE_EXTENSION_SECONDS.
    - Абсолютный максимум удержания: DEBOUNCE_ABSOLUTE_MAX_SECONDS.
    - Если приходит новое фото, а в буфере уже есть фото — сначала флэш текущего буфера, потом начинаем новый.
    - Сохраняем первое фото (media.origin=='photo') как media; последующие текстовые добавляем в aggregated text.
    - Для фото без caption text может быть пустым; caption добавляем как часть текста.
    - Не вставляем placeholder [photo] в сам текст; media несёт origin=photo.
    """
    now = utcnow()
    state = await session.get(ChatState, chat_id)
    if state is None:
        # ensure_entities внутри process_user_text создаст state, но нам нужен сразу
        await ensure_entities(
            session,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            username=username,
            lang=lang,
            settings=settings,
        )
        state = await session.get(ChatState, chat_id)

    existing = getattr(state, 'pending_input_json', None)
    # Helper to actually start new buffer
    def _start_buffer():
        state.pending_input_json = {
            'text': text.strip(),
            'media': media if media else None,
            'started_at': now.isoformat(),
            'deadline_at': (now + timedelta(seconds=DEBOUNCE_INITIAL_SECONDS)).isoformat(),
            'absolute_deadline_at': (now + timedelta(seconds=DEBOUNCE_ABSOLUTE_MAX_SECONDS)).isoformat(),
            'user_id': user_id,
            'username': username,
            'lang': lang,
            'chat_type': chat_type,
        }
        state.pending_started_at = now
        state.pending_updated_at = now
        return "(buffer_started)"

    # Если нет существующего буфера — стартуем
    if not existing:
        marker = _start_buffer()
        # Планируем авто-flush по initial дедлайну
        new_state = state.pending_input_json or {}
        _schedule_flush_task(bot, chat_id, new_state.get('deadline_at'), settings, trace_id)
        return marker

    # Есть буфер: проверим абсолютный дедлайн
    from datetime import datetime as _dt
    def _parse_ts(s: str | None):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except Exception:
            return None
    deadline_at = _parse_ts(existing.get('deadline_at'))
    absolute_deadline_at = _parse_ts(existing.get('absolute_deadline_at'))
    started_at = _parse_ts(existing.get('started_at'))
    existing_media = existing.get('media')

    need_flush_before = False
    # Новое фото при уже существующем фото в буфере -> flush потом новая серия
    if media and media.get('origin') == 'photo' and existing_media and existing_media.get('origin') == 'photo':
        need_flush_before = True

    if need_flush_before:
        await flush_pending_input(bot, session, chat_id=chat_id, settings=settings, trace_id=trace_id)
        marker = _start_buffer()
        new_state = state.pending_input_json or {}
        _schedule_flush_task(bot, chat_id, new_state.get('deadline_at'), settings, trace_id)
        return marker

    # Проверим сроки: если абсолютный дедлайн истёк — flush и начнём новый буфер
    if absolute_deadline_at and now >= absolute_deadline_at:
        await flush_pending_input(bot, session, chat_id=chat_id, settings=settings, trace_id=trace_id)
        marker = _start_buffer()
        new_state = state.pending_input_json or {}
        _schedule_flush_task(bot, chat_id, new_state.get('deadline_at'), settings, trace_id)
        return marker

    # Если обычный дедлайн истёк — flush текущий и новый буфер
    if deadline_at and now >= deadline_at:
        await flush_pending_input(bot, session, chat_id=chat_id, settings=settings, trace_id=trace_id)
        marker = _start_buffer()
        new_state = state.pending_input_json or {}
        _schedule_flush_task(bot, chat_id, new_state.get('deadline_at'), settings, trace_id)
        return marker

    # Продлеваем дедлайн
    new_deadline = now + timedelta(seconds=DEBOUNCE_EXTENSION_SECONDS)
    if absolute_deadline_at and new_deadline > absolute_deadline_at:
        new_deadline = absolute_deadline_at

    # Обновляем текст (склеиваем)
    existing_text = existing.get('text') or ''
    new_text_part = text.strip()
    if new_text_part:
        if existing_text:
            existing_text = f"{existing_text} {new_text_part}".strip()
        else:
            existing_text = new_text_part
    existing['text'] = existing_text
    # Сохраняем фото только если в буфере ещё не было и новое media=photo
    if not existing_media and media and media.get('origin') == 'photo':
        existing['media'] = media
    # Обновим дедлайны
    existing['deadline_at'] = new_deadline.isoformat()
    state.pending_input_json = existing
    state.pending_updated_at = now
    # Перепланируем flush по новому дедлайну
    updated_state = state.pending_input_json or {}
    _schedule_flush_task(bot, chat_id, updated_state.get('deadline_at'), settings, trace_id)
    return "(buffer_extended)"

GOODNIGHT_KEYWORDS = [
    "споки", "спокойной", "доброй ночи", "споки ноки", "споки-ноки", "на ночь", "пора спать", "иду спать"
]

def _normalize(txt: str) -> str:
    return txt.lower().strip()

def _has_goodnight(text: str) -> bool:
    t = _normalize(text)
    return any(k in t for k in GOODNIGHT_KEYWORDS)


ABUSE_PATTERNS: list[str] = []  # regex detection disabled (using n8n classifier)


def is_abusive(text: str) -> bool:  # kept for compatibility, always False now
    return False


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
        # Assign default persona 'nika' so that n8n always receives a persona for new chats (userbot or bot).
        session.add(
            ChatState(
                chat_id=chat_id,
                auto_enabled=bool(settings.proactive.default_auto_messages),
                persona_key="nika",
            )
        )
    else:
        if getattr(state, "persona_key", None) is None:
            state.persona_key = "nika"


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
    media: dict | None = None,
    settings: Settings,
    trace_id: Optional[str] = None,
    tg_message_id: int | None = None,
    skip_persist_user: bool = False,
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

    if not skip_persist_user:
        session.add(Message(chat_id=chat_id, user_id=user_id, text=trimmed, tg_message_id=tg_message_id))

    # Update chat state last_user_msg_at
    state = await session.get(ChatState, chat_id)
    now = utcnow()
    if state is None:
        state = ChatState(chat_id=chat_id, auto_enabled=bool(settings.proactive.default_auto_messages))
        session.add(state)
    prev_user_ts = state.last_user_msg_at
    state.last_user_msg_at = now

    # Универсальная поддержка /wake и /reset (в т.ч. через userbot), даже если sleep активен.
    lowered = trimmed.lower()
    if lowered.startswith("/wake"):
        if getattr(state, "sleep_until", None):
            state.sleep_until = None
        reply = "Я проснулась, можем продолжать ☀️"
        await bot.send_message(chat_id, reply)
        state.last_assistant_at = utcnow()
        return reply
    elif lowered.startswith("/reset"):
        # Сброс состояния как в команде reset
        if getattr(state, "sleep_until", None):
            state.sleep_until = None
        reply = "Контекст очищен: история сброшена, память перезапущена. Можешь продолжать."
        await bot.send_message(chat_id, reply)
        state.last_assistant_at = utcnow()
        return reply
    elif lowered.startswith("/status"):
        # Состояние чата (зеркало команды из commands_router, но нужно и для userbot очереди)
        now_local = utcnow()
        sleeping = bool(state.sleep_until and state.sleep_until > now_local)
        remaining = None
        if sleeping:
            remaining = int((state.sleep_until - now_local).total_seconds())  # type: ignore[arg-type]
        reason = None
        if sleeping:
            try:
                from sqlalchemy import select
                abuse_q = select(Event).where(Event.chat_id==chat_id, Event.kind.in_(["abuse_auto_block","abuse_detected"]))\
                    .order_by(Event.id.desc()).limit(1)
                ev = (await session.execute(abuse_q)).scalars().first()
                if ev:
                    reason = "abuse_auto_block" if ev.kind=="abuse_auto_block" else "abuse_detected"
            except Exception:
                pass
        if sleeping and reason is None:
            reason = "night_mode_or_manual"
        persona = getattr(state, "persona_key", None) or "nika"
        auto = "on" if state.auto_enabled else "off"
        parts = [f"persona: {persona}", f"proactive: {auto}"]
        if sleeping:
            parts.append(f"sleep: yes ({remaining}s left, reason={reason})")
        else:
            parts.append("sleep: no")
        reply = "; ".join(parts)
        try:
            await bot.send_message(chat_id, reply)
        except Exception:
            pass
        state.last_assistant_at = utcnow()
        return reply

    metrics.inc("messages_received_total")

    # Anti-spam
    min_gap = settings.antispam.user_min_seconds_between_msg
    if not is_allowed(prev_user_ts, now, min_gap):
        wait = remaining_wait_seconds(prev_user_ts, now, min_gap)
        warn = f"Слишком часто, подождите ещё {wait} c"
        await bot.send_message(chat_id, warn)
        return warn

    # Sleep mode: если уже спим — игнорируем
    if getattr(state, "sleep_until", None) and state.sleep_until and state.sleep_until > now:
        # Не отвечаем, тихий игнор
        return "(sleep)"

    # Moderation: локальный regex отключён, всё решает n8n meta.abuse

    # Определение quiet окна
    def _parse_window(win: str | None):
        if not win:
            return None
        try:
            a, b = win.split('-')
            h1, m1 = [int(x) for x in a.split(':')]
            h2, m2 = [int(x) for x in b.split(':')]
            return (h1 * 60 + m1, h2 * 60 + m2)
        except Exception:
            return None

    def _in_window(minute_of_day: int, w):
        if not w:
            return False
        s, e = w
        if s == e:
            return True
        if s < e:
            return s <= minute_of_day < e
        return minute_of_day >= s or minute_of_day < e

    quiet_w = _parse_window(getattr(settings, "proactive_quiet_window", None))
    offset_min = state.timezone_offset_minutes or 0
    local_now = now + timedelta(minutes=offset_min)
    minute_of_day = local_now.hour * 60 + local_now.minute

    # Если пользователь пишет прощание и мы в quiet окне: один ответ и спать до окончания окна
    if _has_goodnight(trimmed) and quiet_w and _in_window(minute_of_day, quiet_w):
        # вычисляем конец quiet окна
        start, end = quiet_w
        if start < end:
            end_minutes = end
            wake_local = local_now.replace(hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0)
            if wake_local <= local_now:
                wake_local += timedelta(days=1)
        else:
            wake_local = local_now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
            if minute_of_day >= start:
                wake_local += timedelta(days=1)
        wake_utc = wake_local - timedelta(minutes=offset_min)
        # Отправляем через n8n (intent user_goodnight) и уходим в сон
        history = []  # очищаем для нейтрального шаблона
        ctx = Context(history=history, last_user_msg_at=state.last_user_msg_at, last_assistant_at=state.last_assistant_at)
        chat_info = ChatInfo(chat_id=chat_id, user_id=user_id, lang=lang, username=username, persona=getattr(state, "persona_key", None), memory_rev=getattr(state, "memory_rev", None))
        req = N8nRequest(intent="user_goodnight", chat=chat_info, context=ctx, message=MessageIn(text=trimmed), trace_id=trace_id)
        try:
            n8n_resp = await call_n8n(req, trace_id=trace_id)
            reply = n8n_resp.reply
        except Exception:
            reply = "Спокойной ночи!"  # fallback
        await bot.send_message(chat_id, reply)
        # Persist proactive assistant reply
        session.add(
            AssistantMessage(
                chat_id=chat_id,
                text=reply,
                meta_json={
                    "intent": "user_goodnight",
                    "proactive": True,
                    "persona": getattr(state, "persona_key", None),
                },
            )
        )
        state.sleep_until = wake_utc
        state.last_assistant_at = utcnow()
        return reply

    # Если бот уже сам пожелал спокойной ночи (proactive_evening) и пользователь продолжает писать
    if quiet_w and _in_window(minute_of_day, quiet_w):
        if getattr(state, "last_goodnight_sent_at", None):
            if not getattr(state, "last_goodnight_followup_sent_at", None) and not _has_goodnight(trimmed):
                start, end = quiet_w
                if start < end:
                    end_minutes = end
                    wake_local = local_now.replace(hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0)
                    if wake_local <= local_now:
                        wake_local += timedelta(days=1)
                else:
                    wake_local = local_now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
                    if minute_of_day >= start:
                        wake_local += timedelta(days=1)
                wake_utc = wake_local - timedelta(minutes=offset_min)
                # n8n followup intent
                history = []
                ctx = Context(history=history, last_user_msg_at=state.last_user_msg_at, last_assistant_at=state.last_assistant_at)
                chat_info = ChatInfo(chat_id=chat_id, user_id=user_id, lang=lang, username=username, persona=getattr(state, "persona_key", None), memory_rev=getattr(state, "memory_rev", None))
                req = N8nRequest(intent="goodnight_followup", chat=chat_info, context=ctx, message=MessageIn(text=trimmed), trace_id=trace_id)
                try:
                    n8n_resp = await call_n8n(req, trace_id=trace_id)
                    reply = n8n_resp.reply
                except Exception:
                    reply = "Я ухожу спать до утра."  # fallback
                await bot.send_message(chat_id, reply)
                session.add(
                    AssistantMessage(
                        chat_id=chat_id,
                        text=reply,
                        meta_json={
                            "intent": "goodnight_followup",
                            "proactive": True,
                            "persona": getattr(state, "persona_key", None),
                        },
                    )
                )
                state.sleep_until = wake_utc
                state.last_goodnight_followup_sent_at = utcnow()
                state.last_assistant_at = utcnow()
                return reply

    # Build n8n request
    history = await fetch_recent_history(
        session,
        chat_id,
        limit_pairs=50,  # expanded window target (user requirement)
        persona=getattr(state, "persona_key", None),
        soft_char_limit=8000,
        soft_head=4000,
        soft_tail=2000,
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
    # Build message payload, optionally carrying media metadata for n8n STT branch
    msg_payload = {
        "text": trimmed,
    }
    if isinstance(media, dict) and media:
        msg_payload.update(media)
    req = N8nRequest(
        intent="reply",
        chat=chat_info,
        context=ctx,
        message=MessageIn.model_validate(msg_payload),
        trace_id=trace_id,
    )

    # Call n8n
    logger = get_logger().bind(chat_id=chat_id, user_id=user_id, intent="reply", trace_id=trace_id)
    try:
        n8n_resp = await call_n8n(req, trace_id=trace_id)
    except N8NServerError as e:
        # Пробрасываем серверную ошибку дальше (для ретраев в воркере)
        logger.error("n8n_call_failed_5xx", exc_info=True, error=str(e))
        session.add(Event(kind="n8n_error_5xx", chat_id=chat_id, user_id=user_id, payload_json={"intent": "reply", "status": e.status}))
        metrics.inc("n8n_errors_total", labels={"intent": "reply", "class": "5xx"})
        raise
    except N8NClientError as e:
        # 4xx — считаем окончательной ошибкой (не ретраим), можно подавить
        logger.warning("n8n_call_failed_4xx", error=str(e))
        session.add(Event(kind="n8n_error_4xx", chat_id=chat_id, user_id=user_id, payload_json={"intent": "reply", "status": e.status}))
        metrics.inc("n8n_errors_total", labels={"intent": "reply", "class": "4xx"})
        if not getattr(bot, "suppress_errors", False):
            msg = "Некорректный запрос"
            try:
                await bot.send_message(chat_id, msg)
            except Exception:
                pass
            return msg
        return "(n8n_error_suppressed)"
    except Exception as e:
        logger.error("n8n_call_failed_other", exc_info=True, error=str(e))
        session.add(Event(kind="n8n_error_other", chat_id=chat_id, user_id=user_id, payload_json={"intent": "reply"}))
        metrics.inc("n8n_errors_total", labels={"intent": "reply", "class": "other"})
        raise

    # Если n8n пометил сообщение как оскорбительное — фиксируем событие и применяем мьют
    try:
        meta_obj = getattr(n8n_resp, "meta", None)
        abuse_flag = None
        mute_hours = None
        if meta_obj is not None:
            abuse_flag = getattr(meta_obj, "abuse", None)
            mute_hours = getattr(meta_obj, "mute_hours", None)
            # Дополнительно поддержим вложенный словарь flags, если он появится
            flags = getattr(meta_obj, "flags", None)
            if abuse_flag is None and isinstance(flags, dict):
                abuse_flag = flags.get("abuse")
                if mute_hours is None:
                    mh = flags.get("mute_hours")
                    mute_hours = mh if isinstance(mh, (int, float)) else None
        if abuse_flag is True:
            # Записываем событие, но НЕ ставим немедленный mute (frequency-only режим)
            session.add(
                Event(
                    kind="abuse_detected",
                    chat_id=chat_id,
                    user_id=user_id,
                    payload_json={
                        "severity": getattr(meta_obj, "severity", None),
                        # Сохраним рекомендованные mute_hours от n8n для анализа, но не применяем напрямую
                        "suggested_mute_hours": mute_hours,
                    },
                )
            )
            try:
                logger.info(
                    "abuse_flag_recorded",
                    suggested_mute_hours=mute_hours,
                    severity=getattr(meta_obj, "severity", None),
                    text_preview=trimmed[:120],
                )
            except Exception:
                pass
            # Подсчёт злоупотреблений в окне и авто-блокировка при превышении порога
            try:
                from sqlalchemy import select, func
                window_minutes = int(os.getenv("ABUSE_WINDOW_MINUTES", "30"))
                max_in_window = int(os.getenv("ABUSE_MAX_IN_WINDOW", "10"))
                autoblock_hours = int(os.getenv("ABUSE_AUTO_BLOCK_HOURS", "24"))
                cutoff = utcnow() - timedelta(minutes=window_minutes)
                abuse_cnt_q = select(func.count(Event.id)).where(
                    Event.chat_id == chat_id,
                    Event.kind == "abuse_detected",
                    Event.created_at > cutoff,
                )
                abuse_cnt = (await session.execute(abuse_cnt_q)).scalar() or 0
                if abuse_cnt >= max_in_window:
                    state.sleep_until = utcnow() + timedelta(hours=autoblock_hours)
                    session.add(
                        Event(
                            kind="abuse_auto_block",
                            chat_id=chat_id,
                            user_id=user_id,
                            payload_json={
                                "count": abuse_cnt,
                                "window_min": window_minutes,
                                "block_hours": autoblock_hours,
                            },
                        )
                    )
                    try:
                        logger.warning(
                            "abuse_auto_block_set",
                            count=abuse_cnt,
                            window_min=window_minutes,
                            block_hours=autoblock_hours,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    # --- Логика определения задержки ответа ---
    # Определяем предыдущую активность до текущего сообщения пользователя
    prev_activity = None
    if prev_user_ts and state.last_assistant_at:
        prev_activity = max(prev_user_ts, state.last_assistant_at)
    else:
        prev_activity = prev_user_ts or state.last_assistant_at

    now_for_delay = now
    enforced_long_delay_seconds: Optional[float] = None
    # Детерминированная длинная задержка при длиной паузе
    try:
        thr_min = getattr(settings.reply_delay, "inactivity_long_threshold_minutes", 0)
        if prev_activity and thr_min > 0:
            gap_minutes = (now_for_delay - prev_activity).total_seconds() / 60.0
            if gap_minutes >= thr_min:
                # Применяем только если ещё не применяли для этой паузы
                if (
                    getattr(state, "last_long_pause_reply_at", None) is None
                    or prev_activity > state.last_long_pause_reply_at  # type: ignore[arg-type]
                ):
                    il_min = getattr(settings.reply_delay, "inactivity_long_min_seconds", 180)
                    il_max = getattr(settings.reply_delay, "inactivity_long_max_seconds", il_min)
                    if il_max < il_min:
                        il_max = il_min
                    enforced_long_delay_seconds = random.uniform(il_min, il_max)
                    # пометим чтобы не применять повторно на последующие сообщения этой сессии
                    state.last_long_pause_reply_at = now_for_delay
    except Exception:
        pass

    # Вычисляем обычную (короткую) задержку
    delay = future_with_jitter(settings.reply_delay.min_seconds, settings.reply_delay.max_seconds)
    delay_seconds = max(0.0, (delay - now).total_seconds())
    # Приоритет: 1) детерминированная задержка после долгой паузы 2) редкая длинная задержка
    if enforced_long_delay_seconds is not None:
        delay_seconds = enforced_long_delay_seconds
        delay_kind = "inactivity_long"
    else:
        rl_prob = getattr(settings.reply_delay, "rare_long_probability", 0.0)
        if rl_prob > 0 and random.random() < rl_prob:
            long_min = getattr(settings.reply_delay, "rare_long_min_seconds", 180)
            long_max = getattr(settings.reply_delay, "rare_long_max_seconds", long_min + 180)
            if long_max < long_min:
                long_max = long_min
            long_delay = random.uniform(long_min, long_max)
            delay_seconds = long_delay
            delay_kind = "rare_long"
        else:
            delay_kind = "normal"

    metrics.observe("reply_delay_seconds", delay_seconds)

    # --- Специализированные задержки для медиа ---
    media_origin = None
    try:
        if isinstance(media, dict):
            media_origin = media.get("origin")
    except Exception:
        media_origin = None

    # Применяем override только если не enforced_long (приорит.)
    if media_origin == "photo" and enforced_long_delay_seconds is None:
        # Фото: фиксированный небольшой диапазон 5-6 секунд
        try:
            photo_min = int(os.getenv("PHOTO_REPLY_DELAY_MIN", "5"))
            photo_max = int(os.getenv("PHOTO_REPLY_DELAY_MAX", "6"))
            if photo_max < photo_min:
                photo_max = photo_min
        except Exception:
            photo_min, photo_max = 5, 6
        delay_seconds = random.uniform(photo_min, photo_max)
        delay_kind = "photo"
    elif media_origin in {"voice", "audio"} and enforced_long_delay_seconds is None:
        # Голос: длительность + 2-4с (конфигурируемо через ENV)
        import os as _os
        try:
            extra_min = float(_os.getenv("VOICE_DELAY_EXTRA_MIN", "2"))
            extra_max = float(_os.getenv("VOICE_DELAY_EXTRA_MAX", "4"))
            if extra_max < extra_min:
                extra_max = extra_min
        except Exception:
            extra_min, extra_max = 2.0, 4.0
        duration = 0.0
        try:
            if isinstance(media, dict):
                d = media.get("duration")
                if isinstance(d, (int, float)):
                    duration = float(d)
        except Exception:
            duration = 0.0
        duration = max(1.5, min(duration, 120.0))  # кламп
        delay_seconds = duration + random.uniform(extra_min, extra_max)
        delay_kind = "voice"

    metrics.observe("reply_delay_seconds", delay_seconds, labels={"adjusted": "1" if media_origin else "0"})

    async def _typing_loop(action: str, total: float):  # pragma: no cover (сетевой I/O)
        # Telegram скрывает action через ~5с, поэтому обновляем каждые ~4с
        try:
            end = utcnow() + timedelta(seconds=total)
            while utcnow() < end:
                try:
                    await bot.send_chat_action(chat_id, action)
                except Exception:
                    return
                await asyncio.sleep(4)
        except Exception:
            return

    if delay_seconds <= 0:
        pass  # immediate
    elif delay_seconds <= 30:
        # Короткая задержка — единая индикация печатает
        asyncio.create_task(_typing_loop("typing", delay_seconds))
        await asyncio.sleep(delay_seconds)
    else:
        # Длинная задержка: отпускаем функцию сразу, создаём фоновую задачу
        async def _delayed_send(text_to_send: str, ds: float, chat_id_local: int, meta_local: dict, dkind: str):
            # Для длинной задержки тоже всегда показываем "typing"
            asyncio.create_task(_typing_loop("typing", ds))
            await asyncio.sleep(ds)
            try:
                await bot.send_message(chat_id_local, text_to_send)
            except Exception:
                return
            # Пишем в БД вне исходной транзакции
            from app.db.base import session_scope as _sc
            async with _sc() as bg_session:  # pragma: no cover
                meta_enriched = {**meta_local, "delay_kind": dkind, "delay_seconds": ds}
                bg_session.add(AssistantMessage(chat_id=chat_id_local, text=text_to_send, meta_json=meta_enriched))
                state_bg = await bg_session.get(ChatState, chat_id_local)
                if state_bg:
                    state_bg.last_assistant_at = utcnow()
                    if state_bg.auto_enabled:
                        state_bg.next_proactive_at = future_with_jitter(
                            settings.proactive.min_seconds, settings.proactive.max_seconds, base=utcnow()
                        )
        # Планируем и возвращаем до отправки (не блокируем текущее взаимодействие пользователь -> бот)
        asyncio.create_task(_delayed_send(n8n_resp.reply, delay_seconds, chat_id, meta, delay_kind))
        return n8n_resp.reply  # Возвращаем планируемый ответ

    # Send reply (обычная или уже после короткой задержки)
    sent_text = n8n_resp.reply
    if delay_seconds <= 30:  # мы ещё в текущем контексте
        await bot.send_message(chat_id, sent_text)
        meta = n8n_resp.meta.model_dump()
        if getattr(state, "persona_key", None):
            meta = {"persona": state.persona_key, **meta}
        meta = {**meta, "delay_kind": delay_kind, "delay_seconds": delay_seconds}
        session.add(AssistantMessage(chat_id=chat_id, text=sent_text, meta_json=meta))
        state.last_assistant_at = utcnow()
        if state.auto_enabled:
            state.next_proactive_at = future_with_jitter(
                settings.proactive.min_seconds, settings.proactive.max_seconds, base=state.last_assistant_at
            )
        metrics.inc("replies_sent_total")
    # Если была длинная задержка >30с — сообщение уже планировано и сохранится в фоновой задаче
    return sent_text

