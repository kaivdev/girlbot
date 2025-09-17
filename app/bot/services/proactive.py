from __future__ import annotations

"""APScheduler integration for proactive messaging."""

from datetime import datetime, time, timedelta
from typing import Callable, Optional, AsyncContextManager, Tuple

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.schemas.n8n_io import ChatInfo, Context, N8nRequest
from app.bot.services.history import fetch_recent_history
from app.bot.services.metrics import metrics
from app.bot.services.logging import get_logger
from app.bot.services.n8n_client import call_n8n
from app.config.settings import Settings
from app.db.models import AssistantMessage, ChatState, Event, ProactiveOutbox
from app.utils.time import future_with_jitter, utcnow

def _parse_window(win: str | None) -> Optional[Tuple[int, int]]:
    if not win:
        return None
    try:
        a, b = win.split("-", 1)
        h1, m1 = [int(x) for x in a.split(":", 1)]
        h2, m2 = [int(x) for x in b.split(":", 1)]
        start = h1 * 60 + m1
        end = h2 * 60 + m2
        return (start, end)
    except Exception:
        return None

def _in_window(minute_of_day: int, window: Tuple[int, int]) -> bool:
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= minute_of_day < end
    # overnight window (e.g. 22:00-02:00)
    return minute_of_day >= start or minute_of_day < end

def _same_utc_day(a: Optional[datetime], b: datetime) -> bool:
    if a is None:
        return False
    return a.date() == b.date()

def _minutes_since(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if not dt:
        return None
    return (now - dt).total_seconds() / 60.0


def _cooldown_passed(last: Optional[datetime], now: datetime, delta: timedelta) -> bool:
    if last is None:
        return True
    return (now - last) >= delta


def compute_next_proactive_at(now: datetime, settings: Settings) -> datetime:
    return future_with_jitter(settings.proactive.min_seconds, settings.proactive.max_seconds, base=now)


logger = get_logger()


MORNING_SPAM_WINDOW_MINUTES = 30
MORNING_SPAM_MAX = 1  # допустимо столько morning внутри окна


async def process_due_chats(session: AsyncSession, bot: Bot, settings: Settings) -> None:
    now_utc = utcnow()
    # Stateless: просто берём все auto_enabled + persona выбран
    q = select(ChatState).where(ChatState.auto_enabled.is_(True))
    result = await session.execute(q)
    states = list(result.scalars().all())

    win_morning = _parse_window(settings.proactive_morning_window)
    win_evening = _parse_window(settings.proactive_evening_window)
    win_quiet = _parse_window(settings.proactive_quiet_window)

    for state in states:
        persona = getattr(state, "persona_key", None)
        if not persona:
            continue
        # Спит?
        if getattr(state, "sleep_until", None) and state.sleep_until > now_utc:
            continue
        # вычисляем локальное время (пока через offset, иначе UTC)
        offset_min = state.timezone_offset_minutes or 0
        local_now = now_utc + timedelta(minutes=offset_min)
        minute_of_day = local_now.hour * 60 + local_now.minute

        # quiet hours
        if win_quiet and _in_window(minute_of_day, win_quiet):
            continue

        # last activity
        last_activity = max(filter(None, [state.last_user_msg_at, state.last_assistant_at]), default=None)  # type: ignore[arg-type]
        hours_since_activity = (
            (now_utc - last_activity).total_seconds() / 3600.0 if last_activity else None
        )

        intent: Optional[str] = None
        history_trim: bool = False

        # Morning window (раз в день)
        if not intent and win_morning and _in_window(minute_of_day, win_morning) and not _same_utc_day(state.last_morning_sent_at, now_utc):
            intent = "proactive_morning"
            history_trim = True

        # Evening window (раз в день + доп. 30-минутный cooldown на случай проблем с датой)
        if (
            not intent
            and win_evening
            and _in_window(minute_of_day, win_evening)
            and not _same_utc_day(state.last_goodnight_sent_at, now_utc)
            and _cooldown_passed(state.last_goodnight_sent_at, now_utc, timedelta(minutes=30))
        ):
            intent = "proactive_evening"
            history_trim = True

        # Re-engage (если нет утро/вечер)
        if not intent:
            if hours_since_activity is not None and hours_since_activity >= settings.reengage_min_hours:
                # cooldown
                if (
                    state.last_reengage_sent_at is None
                    or (now_utc - state.last_reengage_sent_at).total_seconds() / 3600.0 >= settings.reengage_cooldown_hours
                ):
                    intent = "proactive_reengage"
                    history_trim = True

        # Generic fallback (используем старый next_proactive_at механизм)
        if not intent and state.next_proactive_at and state.next_proactive_at <= now_utc:
            intent = "proactive_generic"
            history_trim = False

        if not intent:
            continue

        # Advisory lock per chat внутри общей транзакции, чтобы параллельные процессы не дублировали
        try:
            lock_res = await session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": state.chat_id})
            if not lock_res.scalar():  # уже обрабатывается другим инстансом
                continue
        except Exception:
            # если БД не PG или нет привилегий — просто продолжаем без локов
            pass

        chat_id = state.chat_id

        if history_trim:
            history = []
        else:
            history = await fetch_recent_history(session, chat_id, limit_pairs=10, persona=persona)

        ctx = Context(history=history, last_user_msg_at=state.last_user_msg_at, last_assistant_at=state.last_assistant_at)
        chat_info = ChatInfo(chat_id=chat_id, user_id=None, persona=persona, memory_rev=state.memory_rev)
        req = N8nRequest(intent=intent, chat=chat_info, context=ctx)
        try:
            resp = await call_n8n(req)
        except Exception:
            session.add(Event(kind="n8n_error", chat_id=chat_id, user_id=None, payload_json={"intent": intent}))
            metrics.inc("n8n_errors_total", labels={"intent": intent})
            # Для generic переназначим next_proactive_at
            if intent == "proactive_generic":
                state.next_proactive_at = compute_next_proactive_at(now_utc, settings)
            continue

        meta = resp.meta.model_dump()
        meta = {"intent": intent, **meta}
        # Антиспам для morning: если уже есть отправка за окно, отключаем auto
        if intent == "proactive_morning":
            recent_cnt_q = select(func.count(AssistantMessage.id)).where(
                AssistantMessage.chat_id == chat_id,
                AssistantMessage.created_at > now_utc - timedelta(minutes=MORNING_SPAM_WINDOW_MINUTES),
            )
            recent_cnt = (await session.execute(recent_cnt_q)).scalar() or 0
            if recent_cnt >= MORNING_SPAM_MAX:
                state.auto_enabled = False
                logger.warning(
                    "proactive_morning_spam_disabled", chat_id=chat_id, recent_cnt=recent_cnt
                )
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                continue

        # Ставим отметку СРАЗУ (раньше отправки), чтобы при падении после send не зациклиться
        if intent == "proactive_morning":
            state.last_morning_sent_at = now_utc
        elif intent == "proactive_evening":
            state.last_goodnight_sent_at = now_utc
        elif intent == "proactive_reengage":
            state.last_reengage_sent_at = now_utc

        # Flush отметок до отправки
        try:
            await session.flush()
        except Exception:
            logger.warning("proactive_flush_error_pre_send", intent=intent, chat_id=chat_id)

        if getattr(state, "proactive_via_userbot", False):
            session.add(ProactiveOutbox(chat_id=chat_id, intent=intent, text=resp.reply, meta_json=meta))
        else:
            try:
                await bot.send_message(chat_id, resp.reply)
                state.last_assistant_at = utcnow()
                session.add(AssistantMessage(chat_id=chat_id, text=resp.reply, meta_json=meta))
            except Exception as e:
                logger.exception("proactive_send_error", intent=intent, chat_id=chat_id, error=str(e))
                # отметку не откатываем — иначе зациклится

        # Немедленный flush чтобы штамп сохранился даже если остальные чаты вызовут сбой
        # Финальный flush уже после отправки (оставляем для generic ниже)
        try:
            await session.flush()
        except Exception as e:
            logger.warning("proactive_flush_error_post_send", intent=intent, chat_id=chat_id, error=str(e))

        if intent == "proactive_generic":
            base_time = state.last_assistant_at or utcnow()
            state.next_proactive_at = compute_next_proactive_at(base_time, settings)

        metrics.inc("proactive_sent_total", labels={"intent": intent})

        # Пер-чата commit чтобы минимизировать вероятность повторов при сбоях далее
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("proactive_commit_error", intent=intent, chat_id=chat_id)


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
