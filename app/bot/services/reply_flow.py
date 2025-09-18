from __future__ import annotations

"""Core reply flow: save, spam-check, call n8n, delay and send."""

import asyncio
from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import ChatInfo, Context, MessageIn, N8nRequest
from app.bot.services.anti_spam import is_allowed, remaining_wait_seconds
from app.bot.services.history import fetch_recent_history
from app.bot.services.metrics import metrics
from app.bot.services.n8n_client import call_n8n
from app.bot.services.logging import get_logger
from app.config.settings import Settings
from app.db.models import AssistantMessage, Chat, ChatState, Event, Message, User
from app.utils.time import future_with_jitter, utcnow
from datetime import timedelta
import random
import re

GOODNIGHT_KEYWORDS = [
    "споки", "спокойной", "доброй ночи", "споки ноки", "споки-ноки", "на ночь", "пора спать", "иду спать"
]

def _normalize(txt: str) -> str:
    return txt.lower().strip()

def _has_goodnight(text: str) -> bool:
    t = _normalize(text)
    return any(k in t for k in GOODNIGHT_KEYWORDS)


ABUSE_PATTERNS = [
    r"\b(сука|шлюх|туп(ая|ой)|идиот|дебил|мразь|тварь|урод|еблан|нахуй|выеб|пошел\s*нах)\b",
    r"\b(fuck|bitch|whore|slut|stupid|idiot|moron|retard)\b",
]


def is_abusive(text: str) -> bool:
    t = _normalize(text)
    for pat in ABUSE_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return True
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

    # Moderation: если пользователь оскорбляет — включаем «грубый» ответ и/или мьют
    try:
        if getattr(settings, "moderation", None) and settings.moderation.abuse_enabled and is_abusive(trimmed):
            # Включаем короткий резкий ответ и мьют до now + abuse_mute_hours
            mute_until = now + timedelta(hours=settings.moderation.abuse_mute_hours)
            state.sleep_until = mute_until
            history = []
            ctx = Context(history=history, last_user_msg_at=state.last_user_msg_at, last_assistant_at=state.last_assistant_at)
            chat_info = ChatInfo(chat_id=chat_id, user_id=user_id, lang=lang, username=username, persona=getattr(state, "persona_key", None), memory_rev=getattr(state, "memory_rev", None))
            req = N8nRequest(intent="reply", chat=chat_info, context=ctx, message=MessageIn(text="[abuse_detected] " + trimmed), trace_id=trace_id)
            try:
                n8n_resp = await call_n8n(req, trace_id=trace_id)
                reply_text = n8n_resp.reply
            except Exception:
                reply_text = "Не перегибай. Вернусь позже."
            await bot.send_message(chat_id, reply_text)
            state.last_assistant_at = utcnow()
            return reply_text
    except Exception:
        pass

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
                state.sleep_until = wake_utc
                state.last_goodnight_followup_sent_at = utcnow()
                state.last_assistant_at = utcnow()
                return reply

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
    except Exception as e:
        # On error: notify softly and log event
        logger.error("n8n_call_failed", exc_info=True, error=str(e))
        session.add(
            Event(kind="n8n_error", chat_id=chat_id, user_id=user_id, payload_json={"intent": "reply"})
        )
        msg = "Сервис занят, попробуйте позже"
        await bot.send_message(chat_id, msg)
        metrics.inc("n8n_errors_total", labels={"intent": "reply"})
        return msg

    # Если n8n пометил сообщение как оскорбительное — сразу уходим в «сон» на указанный срок
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
            try:
                hours = float(mute_hours) if mute_hours is not None else float(getattr(getattr(settings, "moderation", object()), "abuse_mute_hours", 12))
            except Exception:
                hours = float(getattr(getattr(settings, "moderation", object()), "abuse_mute_hours", 12))
            state.sleep_until = utcnow() + timedelta(hours=hours)
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

    if delay_seconds <= 0:
        pass  # immediate
    elif delay_seconds <= 30:
        # Короткая задержка — ждём inline
        await asyncio.sleep(delay_seconds)
    else:
        # Длинная задержка: отпускаем функцию сразу, создаём фоновую задачу
        async def _delayed_send(text_to_send: str, ds: float, chat_id_local: int, meta_local: dict, dkind: str):
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

