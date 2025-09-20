from __future__ import annotations

"""
Pyrogram userbot runner that reuses the existing reply flow.

- Listens to private messages and group mentions/replies to you
- Shows typing action while thinking
- Calls your n8n flow via existing code and sends the reply

Usage:
  1) Fill .env with TELEGRAM_API_ID / TELEGRAM_API_HASH and PYROGRAM_SESSION_STRING
  2) pip install -r requirements.txt
  3) python -m scripts.pyro_userbot
"""

import asyncio
import contextlib
import os
import random
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Optional, Dict, List
import time
from collections import deque

from dotenv import load_dotenv
from sqlalchemy import delete
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ChatType
from pyrogram.types import Message
import httpx

from app.config.settings import get_settings
from app.db.base import session_scope
from app.bot.services.reply_flow import process_user_text, buffer_or_process, flush_pending_input, flush_expired_pending_input
from app.db.task_queue import enqueue_task, lease_tasks, complete, heartbeat, return_to_pending
from app.db.task_watchdog import watchdog_pass
from app.bot.services.metrics import metrics
from app.bot.services.logging import get_logger
from app.db.models import ProactiveOutbox, AssistantMessage, ChatState, Message as DBMessage, Chat
from app.utils.time import utcnow


class PyroBotShim:
    """Minimal shim to satisfy process_user_text(bot=...)."""

    def __init__(self, client: Client, reply_selector: Optional[callable] = None, suppress_errors: bool = True):
        self.client = client
        self.reply_selector = reply_selector
        self.suppress_errors = suppress_errors

    async def send_message(self, chat_id: int, text: str):  # aiogram-like surface
        reply_to_id = None
        if self.reply_selector:
            try:
                reply_to_id = self.reply_selector(chat_id)
            except Exception:
                reply_to_id = None
        if reply_to_id:
            return await self.client.send_message(chat_id, text, reply_to_message_id=reply_to_id)
        return await self.client.send_message(chat_id, text)

    async def send_chat_action(self, chat_id: int, action: str):  # emulate aiogram Bot interface
        # Map aiogram-like action strings to Pyrogram ChatAction enums
        mapping = {
            "typing": ChatAction.TYPING,
            "upload_photo": getattr(ChatAction, "UPLOAD_PHOTO", ChatAction.TYPING),
            "record_voice": getattr(ChatAction, "RECORD_AUDIO", ChatAction.TYPING),
            "record_audio": getattr(ChatAction, "RECORD_AUDIO", ChatAction.TYPING),
        }
        enum_val = mapping.get(action.lower(), ChatAction.TYPING)
        try:
            await self.client.send_chat_action(chat_id, enum_val)
        except Exception:
            # Fallback to typing silently
            with contextlib.suppress(Exception):
                await self.client.send_chat_action(chat_id, ChatAction.TYPING)


async def typing_loop(client: Client, chat_id: int, interval: float):
    """Continuously send typing action until cancelled."""
    try:
        while True:
            await client.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return


def _chat_type_str(chat_type: ChatType | str | None) -> str:
    if chat_type is None:
        return "private"
    if isinstance(chat_type, ChatType):
        # Values are 'private', 'group', 'supergroup', 'channel'
        return chat_type.value
    return str(chat_type)


def _is_for_me(msg: Message, my_id: int) -> bool:
    if msg.chat.type in (ChatType.PRIVATE,):
        return True
    # For groups/supergroups: only react when mentioned or replied to me
    if msg.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if getattr(msg, "mentioned", False):
            return True
        if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == my_id:
            return True
    return False


async def main() -> None:
    load_dotenv()
    settings = get_settings()

    raw_api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_string: Optional[str] = os.getenv("PYROGRAM_SESSION_STRING")
    if not raw_api_id or not raw_api_id.strip():
        raise RuntimeError("TELEGRAM_API_ID is empty or not set (add to .env / service env)")
    if not raw_api_id.isdigit():
        raise RuntimeError(f"TELEGRAM_API_ID must be digits, got: {raw_api_id!r}")
    api_id = int(raw_api_id)
    if not api_hash or not api_hash.strip():
        raise RuntimeError("TELEGRAM_API_HASH is empty or not set (add to .env / service env)")

    if session_string:
        app = Client(name="userbot", api_id=api_id, api_hash=api_hash, session_string=session_string)
    else:
        # Will create a local session file named `userbot.session` and ask for login
        app = Client(name="userbot", api_id=api_id, api_hash=api_hash)

    # Periodic reply-to configuration
    reply_quote_enabled = os.getenv("USERBOT_REPLY_QUOTE_ENABLED", "1").lower() in {"1","true","yes","on"}
    reply_quote_every_min = int(float(os.getenv("USERBOT_REPLY_QUOTE_EVERY_MIN", "10")))
    reply_quote_every_max = int(float(os.getenv("USERBOT_REPLY_QUOTE_EVERY_MAX", "15")))
    if reply_quote_every_max < reply_quote_every_min:
        reply_quote_every_max = reply_quote_every_min

    # Per-chat counters and thresholds
    reply_counters: Dict[int, int] = {}
    reply_thresholds: Dict[int, int] = {}
    recent_user_msgs: Dict[int, deque[int]] = {}

    def _new_threshold() -> int:
        return random.randint(reply_quote_every_min, reply_quote_every_max)

    # Стратегия выбора сообщения для ответа зафиксирована: всегда последнее входящее
    reply_quote_strategy = "last"

    def _select_reply_to(chat_id: int) -> Optional[int]:
        if not reply_quote_enabled:
            return None
        cnt = reply_counters.get(chat_id, 0) + 1
        reply_counters[chat_id] = cnt
        thr = reply_thresholds.get(chat_id)
        if thr is None or thr <= 0:
            thr = _new_threshold()
            reply_thresholds[chat_id] = thr
        if cnt >= thr:
            reply_counters[chat_id] = 0
            reply_thresholds[chat_id] = _new_threshold()
            dq = recent_user_msgs.get(chat_id)
            if dq and len(dq) > 0:
                try:
                    if reply_quote_strategy == "last":
                        return dq[-1]
                    elif reply_quote_strategy == "first":
                        return dq[0]
                    else:  # random
                        idx = random.randrange(0, len(dq))
                        return list(dq)[idx]
                except Exception:
                    with contextlib.suppress(Exception):
                        return dq[-1]
        return None

    shim = PyroBotShim(app, reply_selector=_select_reply_to, suppress_errors=True)

    # Userbot behavior configuration (env overrides)
    # Поведение прочтения:
    # immediate | after_reply | never | delay | random
    # random -> прочитать через случайный интервал в заданных пределах
    mark_read_mode = os.getenv("USERBOT_MARK_READ_MODE", "after_reply").lower()
    delay_mark_read_sec = float(os.getenv("USERBOT_DELAY_MARK_READ", "0"))  # для mode=delay
    random_read_min = float(os.getenv("USERBOT_MARK_READ_RANDOM_MIN", "1.0"))
    random_read_max = float(os.getenv("USERBOT_MARK_READ_RANDOM_MAX", "5.0"))
    # Новая опция: принудительно читать до начала typing-индикации
    read_before_typing = os.getenv("USERBOT_READ_BEFORE_TYPING", "1").lower() in {"1","true","yes","on"}

    # Задержка между моментом прочтения и началом индикации набора (имитация «прочитала, осмысливает»)
    typing_start_delay_min = float(os.getenv("USERBOT_TYPING_START_DELAY_MIN", "0"))
    typing_start_delay_max = float(os.getenv("USERBOT_TYPING_START_DELAY_MAX", "0"))

    # Доп. притормаживание перед началом обработки (имитация что человек не сразу прочитал/отреагировал)
    pre_proc_min = float(os.getenv("USERBOT_PRE_PROCESS_DELAY_MIN", "0"))
    pre_proc_max = float(os.getenv("USERBOT_PRE_PROCESS_DELAY_MAX", "0"))
    typing_enabled = os.getenv("USERBOT_TYPING", "1").lower() in {"1", "true", "yes", "on"}
    typing_interval = float(os.getenv("USERBOT_TYPING_INTERVAL", "4"))
    min_typing_seconds = float(os.getenv("USERBOT_MIN_TYPING_SECONDS", "0"))  # ensure at least this visual typing

    my_id_box: dict[str, int] = {}

    # Batch configuration
    batch_enabled = os.getenv("USERBOT_BATCH_ENABLED", "0").lower() in {"1","true","yes","on"}
    # Новая логика: вместо склейки -> задержанный запуск обработки после окна тишины или достижения лимита сообщений
    batch_inactivity_sec = float(os.getenv("USERBOT_BATCH_INACTIVITY", "10.0"))  # окно бездействия
    batch_max_messages = int(os.getenv("USERBOT_BATCH_MAX_MESSAGES", "50"))    # максимум сообщений в пакете
    batch_debounce_cancel_ms = int(os.getenv("USERBOT_BATCH_DEBOUNCE_CANCEL_MS", "900"))
    batch_cancel_cooldown_sec = float(os.getenv("USERBOT_BATCH_CANCEL_COOLDOWN", "15"))
    batch_cancel_window_sec = float(os.getenv("USERBOT_BATCH_CANCEL_WINDOW", "10"))
    outbox_poll_seconds = float(os.getenv("USERBOT_OUTBOX_POLL_SECONDS", "10"))
    cancel_on_new_msg = os.getenv("USERBOT_CANCEL_ON_NEW_MSG", "1").lower() in {"1","true","yes","on"}
    # Wait time to attach follow-up text to a photo that's still uploading
    photo_agg_wait = float(os.getenv("USERBOT_PHOTO_AGGREGATION_WAIT", "3.0"))

    # Буфер по chat_id
    message_buffers: Dict[int, List[Message]] = {}
    buffer_tasks: Dict[int, asyncio.Task] = {}
    inflight_tasks: Dict[int, asyncio.Task] = {}
    last_message_ts: Dict[int, float] = {}
    cancel_events: Dict[int, List[float]] = {}
    debounce_timers: Dict[int, float] = {}
    # Chats where a photo is being processed (uploading/creating DB pending)
    photo_inflight: Dict[int, float] = {}

    def _start_generation(chat_id: int, base: Message, combined_text: str):
        get_logger().info(
            "buffer_generation_start",
            chat_id=chat_id,
            messages=len(message_buffers.get(chat_id, [])),
            sample=combined_text[:120],
        )
        task = asyncio.create_task(_process_single(app, shim, base, combined_text))
        inflight_tasks[chat_id] = task
        def _cleanup(_):
            # убрать из карты по завершению (успех/ошибка/отмена)
            if inflight_tasks.get(chat_id) is task:
                inflight_tasks.pop(chat_id, None)
        task.add_done_callback(_cleanup)
        return task

    async def flush_buffer(chat_id: int):
        msgs = message_buffers.get(chat_id, [])
        if not msgs:
            return
        base = msgs[-1]
        combined_text = " \n".join(m.text or "" for m in msgs)
        message_buffers[chat_id] = []
        get_logger().info(
            "buffer_flush",
            chat_id=chat_id,
            count=len(msgs),
            reason=current_flush_reasons.get(chat_id, "unknown"),
            total_chars=len(combined_text),
        )
        _start_generation(chat_id, base, combined_text)

    current_flush_reasons: Dict[int, str] = {}

    async def schedule_buffer_send(chat_id: int):
        # Новая логика: ждём бездействия batch_inactivity_sec или переполнения лимита
        get_logger().info("buffer_timer_start", chat_id=chat_id, inactivity=batch_inactivity_sec, max_messages=batch_max_messages)
        while True:
            await asyncio.sleep(0.3)
            if chat_id not in message_buffers:
                break
            buf = message_buffers.get(chat_id, [])
            if not buf:
                continue
            last_ts = last_message_ts.get(chat_id, 0)
            if time.monotonic() - last_ts >= batch_inactivity_sec:
                current_flush_reasons[chat_id] = "inactivity"
                break
            if len(buf) >= batch_max_messages:
                current_flush_reasons[chat_id] = "max_messages"
                break
        await flush_buffer(chat_id)
        buffer_tasks.pop(chat_id, None)

    async def _process_single(app: Client, shim: PyroBotShim, message: Message, text: str, media: Optional[Dict] = None, *, disable_local_typing: bool = False, use_buffer: bool = False):
        trace_id = str(uuid.uuid4())
        try:
            return await _process_single_inner(app, shim, message, text, media, disable_local_typing=disable_local_typing, use_buffer=use_buffer, trace_id=trace_id)
        except asyncio.CancelledError:
            get_logger().info("userbot_task_cancelled", chat_id=message.chat.id, trace_id=trace_id)
            return
        except Exception as e:
            get_logger().error("userbot_task_failed", chat_id=message.chat.id, error=str(e), trace_id=trace_id)
            # fallback можно добавить по желанию
            return

    async def _process_single_inner(app: Client, shim: PyroBotShim, message: Message, text: str, media: Optional[Dict] = None, *, disable_local_typing: bool = False, use_buffer: bool = False, trace_id: Optional[str] = None):
        # Lazy initialize my own user id
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id
        if not _is_for_me(message, my_id_box["id"]):
            return
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return
        chat = message.chat
        user = message.from_user
        # Автоматически помечаем чат для проактивов через userbot
        try:
            await mark_chat_state(chat.id)  # noqa: F821 (определена ниже до обработки событий)
        except Exception:
            pass
        # Опциональная случайная задержка ДО любой активности (имитация того, что "прочитал не мгновенно")
        if pre_proc_max > 0 and pre_proc_max >= pre_proc_min and (pre_proc_min > 0 or pre_proc_max > 0):
            pre_sleep = random.uniform(pre_proc_min, pre_proc_max)
            await asyncio.sleep(pre_sleep)

        read_done = False
        read_task: Optional[asyncio.Task] = None
        # Сначала логика режима, но если включен read_before_typing и режим не "never" — читаем сразу.
        if read_before_typing and mark_read_mode != "never":
            with contextlib.suppress(Exception):
                await app.read_chat_history(chat.id)
                read_done = True
        else:
            if mark_read_mode == "immediate":
                with contextlib.suppress(Exception):
                    await app.read_chat_history(chat.id)
                    read_done = True
            elif mark_read_mode == "random":
                # Планируем случайное прочтение (если пользователь успеет получить ответ раньше — может прочитаться позже)
                rand_delay = random.uniform(random_read_min, max(random_read_min, random_read_max))

                async def _delayed_read():
                    nonlocal read_done
                    try:
                        await asyncio.sleep(rand_delay)
                        if not read_done:
                            with contextlib.suppress(Exception):
                                await app.read_chat_history(chat.id)
                                read_done = True
                    except asyncio.CancelledError:
                        pass

                read_task = asyncio.create_task(_delayed_read())
        # Возможная задержка ПОСЛЕ чтения и ПЕРЕД началом typing
        if typing_enabled and typing_start_delay_max >= typing_start_delay_min and typing_start_delay_max > 0:
            await asyncio.sleep(random.uniform(typing_start_delay_min, typing_start_delay_max))

        start_t = time.monotonic()
        typing_task: Optional[asyncio.Task] = None
        if typing_enabled and not disable_local_typing:
            typing_task = asyncio.create_task(typing_loop(app, chat.id, typing_interval))
        try:
            cancelled = False
            async with session_scope() as session:
                try:
                    if use_buffer:
                        # Перед добавлением текста пробуем авто-флаш просроченного буфера
                        await flush_pending_input(shim, session, chat_id=chat.id, settings=settings)
                        marker = await buffer_or_process(
                            shim,
                            session,
                            chat_id=chat.id,
                            chat_type=_chat_type_str(chat.type),
                            user_id=(user.id if user else None),
                            username=(user.username if user else None),
                            lang=(getattr(user, "language_code", None) if user else None),
                            text=text,
                            media=media,
                            settings=settings,
                            trace_id=trace_id,
                        )
                        # Если просто буфер — ответа сейчас не будет
                        if marker in {"(buffer_started)", "(buffer_extended)"}:
                            return
                    else:
                        await process_user_text(
                            shim,
                            session,
                            chat_id=chat.id,
                            chat_type=_chat_type_str(chat.type),
                            user_id=(user.id if user else None),
                            username=(user.username if user else None),
                            lang=(getattr(user, "language_code", None) if user else None),
                            text=text,
                            media=media,
                            settings=settings,
                            trace_id=trace_id,
                        )
                except asyncio.CancelledError:
                    cancelled = True
                    with contextlib.suppress(Exception):
                        await session.rollback()
                # exit context manager normally to ensure proper cleanup
            # Как только реальный ответ отправлен (функция вернулась) — убираем индикацию набора,
            # чтобы не возникал повторный "всплеск" typing через пару секунд.
            if typing_task:
                typing_task.cancel()
                with contextlib.suppress(Exception):
                    await typing_task
            if not cancelled and typing_enabled and min_typing_seconds > 0:
                # Додерживаем минимальное время ТОЛЬКО если ответ пришёл слишком быстро,
                # но уже без новых send_chat_action (индикатор просто погаснет чуть раньше — это ок).
                elapsed = time.monotonic() - start_t
                remaining = min_typing_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            if not cancelled and mark_read_mode in {"after_reply", "delay"}:
                if mark_read_mode == "delay" and delay_mark_read_sec > 0:
                    await asyncio.sleep(delay_mark_read_sec)
                if not read_done:
                    with contextlib.suppress(Exception):
                        await app.read_chat_history(chat.id)
                        read_done = True
        finally:
            # Финальное страхующее отключение (если отменили раньше — ничего не произойдёт)
            if typing_task and not typing_task.cancelled():
                typing_task.cancel()
                with contextlib.suppress(Exception):
                    await typing_task
            # Отменяем отложенное чтение, если ещё не произошло и мы уже прочли
            if read_task and not read_task.done():
                if read_done:
                    read_task.cancel()
                    with contextlib.suppress(Exception):
                        await read_task
        # Если задача была отменена — корректно выходим без ответа
        if locals().get("cancelled"):
            return

    @app.on_message(filters.command(["reset"]) & ~filters.me)
    async def handle_reset(_: Client, message: Message):
        # Ограничим обработку командами, адресованными нам (для групп — при упоминании/ответе)
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id
        if not _is_for_me(message, my_id_box["id"]):
            return
        chat_id = message.chat.id
        # Остановим буферы/генерацию для чата
        if task := buffer_tasks.get(chat_id):
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            buffer_tasks.pop(chat_id, None)
        if task := inflight_tasks.get(chat_id):
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            inflight_tasks.pop(chat_id, None)
        message_buffers[chat_id] = []

        # Очистим историю чата и сбросим память
        async with session_scope() as session:
            await session.execute(delete(DBMessage).where(DBMessage.chat_id == chat_id))
            await session.execute(delete(AssistantMessage).where(AssistantMessage.chat_id == chat_id))
            
            # Убедимся, что запись чата существует
            chat = await session.get(Chat, chat_id)
            if chat is None:
                chat = Chat(id=chat_id)
                session.add(chat)
            
            state = await session.get(ChatState, chat_id)
            if state is None:
                state = ChatState(chat_id=chat_id)
                session.add(state)
            state.last_user_msg_at = None
            state.last_assistant_at = None
            state.next_proactive_at = None
            state.memory_rev = (state.memory_rev or 1) + 1

        await app.send_message(chat_id, "Контекст очищен: история сброшена, память перезапущена. Можешь продолжать.")

    use_queue = os.getenv("USERBOT_QUEUE_ENABLED", "1").lower() in {"1","true","yes","on"}
    get_logger().info("queue_init", enabled=use_queue)

    # Async migration status check (non-fatal)
    async def _check_migrations():
        try:
            from sqlalchemy import text as _sql_text
            async with session_scope() as session:
                res = await session.execute(_sql_text("SELECT version_num FROM alembic_version"))
                row = res.first()
                current = row[0] if row else None
                expected_head = "0013_add_tg_message_id"
                get_logger().info("migrations_status", current=current, expected=expected_head, up_to_date=(current == expected_head))
        except Exception as e:
            get_logger().warning("migrations_status_error", error=str(e))
    asyncio.create_task(_check_migrations())

    async def _enqueue_incoming(message: Message, combined_text: str, media: Optional[Dict] = None, source: str = "live"):
        async with session_scope() as session:
            payload = {
                "telegram_message_id": message.id,
                "chat_id": message.chat.id,
                "chat_type": _chat_type_str(message.chat.type),
                "user_id": getattr(message.from_user, 'id', None) if message.from_user else None,
                "username": getattr(message.from_user, 'username', None) if message.from_user else None,
                "lang": getattr(message.from_user, 'language_code', None) if message.from_user else None,
                "text": combined_text,
                "media": media,
                "trace_id": str(uuid.uuid4()),
                "source": source,
            }
            # dedup по (chat_id, telegram_message_id) для одиночных сообщений
            dedup = f"inmsg:{message.chat.id}:{message.id}"
            await enqueue_task(session, kind="incoming_user_message", payload=payload, priority=100, dedup_key=dedup)
            metrics.inc("tasks_created_total", labels={"kind": "incoming_user_message", "source": source})
            get_logger().info("task_enqueue", kind="incoming_user_message", chat_id=message.chat.id, tg_message_id=message.id, source=source, trace_id=payload["trace_id"])

    @app.on_message(filters.text & ~filters.me)
    async def handle_text(_: Client, message: Message):
        # Lazy initialize my own user id
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id
        if not _is_for_me(message, my_id_box["id"]):
            return
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return

        # Запоминаем id входящих юзерских сообщений для потенциального reply_to
        dq = recent_user_msgs.setdefault(message.chat.id, deque(maxlen=20))
        dq.append(message.id)
        # Helper to append text to a pending photo buffer in DB and avoid double replies
        async def _try_append_to_pending_photo() -> bool:
            try:
                async with session_scope() as session:
                    state = await session.get(ChatState, message.chat.id)
                    pending = getattr(state, 'pending_input_json', None) if state else None
                    pending_is_photo = bool(pending and pending.get('media') and pending['media'].get('origin') == 'photo')
                    if pending_is_photo:
                        flushed = await flush_expired_pending_input(shim, session, chat_id=message.chat.id, settings=get_settings())
                        if not flushed:
                            await buffer_or_process(
                                shim,
                                session,
                                chat_id=message.chat.id,
                                chat_type=_chat_type_str(message.chat.type),
                                user_id=(message.from_user.id if message.from_user else None),
                                username=(message.from_user.username if message.from_user else None),
                                lang=(getattr(message.from_user, 'language_code', None) if message.from_user else None),
                                text=message.text or "",
                                media=None,
                                settings=get_settings(),
                                trace_id=None,
                            )
                        return True
            except Exception:
                return False
            return False

        # Fast path: DB already has a pending photo buffer
        if await _try_append_to_pending_photo():
            return

        # Slow path: a photo is in-flight (upload not finished) — wait briefly
        ts = photo_inflight.get(message.chat.id)
        if ts is not None and (time.monotonic() - ts) <= max(0.1, photo_agg_wait):
            deadline = time.monotonic() + photo_agg_wait
            while time.monotonic() < deadline:
                if await _try_append_to_pending_photo():
                    return
                await asyncio.sleep(0.2)

        if not batch_enabled:
            # Попробуем сначала только истекший буфер сбросить. Если буфер активен (фото) и не истёк — просто расширяем его.
            async with session_scope() as session:
                state = await session.get(ChatState, message.chat.id)
                pending = getattr(state, 'pending_input_json', None) if state else None
                pending_is_photo = bool(pending and pending.get('media') and pending['media'].get('origin') == 'photo')
                if pending_is_photo:
                    # Если дедлайны истекли – flush, тогда текст станет новым буфером / сообщением.
                    flushed = await flush_expired_pending_input(shim, session, chat_id=message.chat.id, settings=get_settings())
                    if not flushed:
                        # Буфер активен и не истёк – просто расширяем.
                        await buffer_or_process(
                            shim,
                            session,
                            chat_id=message.chat.id,
                            chat_type=_chat_type_str(message.chat.type),
                            user_id=(message.from_user.id if message.from_user else None),
                            username=(message.from_user.username if message.from_user else None),
                            lang=(getattr(message.from_user, 'language_code', None) if message.from_user else None),
                            text=message.text or "",
                            media=None,
                            settings=get_settings(),
                            trace_id=None,
                        )
                        return
            # Если не было активного фото-буфера или он сброшен – обычная обработка (с проверкой на старт нового буфера в _process_single при use_buffer)
            if use_queue:
                await _enqueue_incoming(message, message.text or "", media=None)
            else:
                await _process_single(app, shim, message, message.text or "")
            return

        # Если во время генерации пришло новое сообщение — по желанию отменяем генерацию
        if cancel_on_new_msg:
            task = inflight_tasks.get(message.chat.id)
            if task and not task.done():
                now = time.monotonic()
                # Дебаунс отмены
                last_fire = debounce_timers.get(message.chat.id, 0)
                if (now - last_fire)*1000 >= batch_debounce_cancel_ms:
                    # Проверяем окно частых отмен
                    ev_list = cancel_events.setdefault(message.chat.id, [])
                    ev_list.append(now)
                    # чистим просроченные
                    cancel_events[message.chat.id] = [t for t in ev_list if now - t <= batch_cancel_window_sec]
                    if len(cancel_events[message.chat.id]) >= 2:
                        # Вошли в бурст отмен — возможно пропускаем отмену в cooldown период
                        last_two = cancel_events[message.chat.id][-2]
                        if now - last_two < batch_cancel_window_sec:
                            # Если уже в cooldown — не отменяем
                            if now - last_two < batch_cancel_window_sec and (now - last_fire) < batch_cancel_cooldown_sec:
                                get_logger().info(
                                    "buffer_cancel_skipped",
                                    chat_id=message.chat.id,
                                    reason="cooldown",
                                    recent_cancels=len(cancel_events[message.chat.id]),
                                )
                            else:
                                task.cancel()
                                debounce_timers[message.chat.id] = now
                                get_logger().info(
                                    "buffer_cancel_attempt",
                                    chat_id=message.chat.id,
                                    reason="burst_cancel",
                                    recent_cancels=len(cancel_events[message.chat.id]),
                                )
                        else:
                            task.cancel()
                            debounce_timers[message.chat.id] = now
                            get_logger().info(
                                "buffer_cancel_attempt",
                                chat_id=message.chat.id,
                                reason="second_in_window",
                                recent_cancels=len(cancel_events[message.chat.id]),
                            )
                    else:
                        task.cancel()
                        debounce_timers[message.chat.id] = now
                        get_logger().info(
                            "buffer_cancel_attempt",
                            chat_id=message.chat.id,
                            reason="first_cancel",
                            recent_cancels=len(cancel_events[message.chat.id]),
                        )
                else:
                    get_logger().info(
                        "buffer_cancel_skipped",
                        chat_id=message.chat.id,
                        reason="debounce",
                        since_last_ms=(now - last_fire)*1000,
                    )
        buf = message_buffers.setdefault(message.chat.id, [])
        buf.append(message)
        get_logger().info(
            "buffer_append",
            chat_id=message.chat.id,
            size=len(buf),
            max=batch_max_messages,
            text_preview=(message.text or "")[:80],
        )
        last_message_ts[message.chat.id] = time.monotonic()
        # Лимит — немедленный flush
        if len(buf) >= batch_max_messages:
            if task := buffer_tasks.get(message.chat.id):
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
            if use_queue:
                msgs = message_buffers.get(message.chat.id, [])
                if msgs:
                    base = msgs[-1]
                    combined_text = " \n".join(m.text or "" for m in msgs)
                    get_logger().info(
                        "buffer_flush",
                        chat_id=message.chat.id,
                        count=len(msgs),
                        reason="max_messages",
                        total_chars=len(combined_text),
                    )
                    message_buffers[message.chat.id] = []
                    await _enqueue_incoming(base, combined_text, media=None, source="batch")
            else:
                await flush_buffer(message.chat.id)
            return
        if message.chat.id not in buffer_tasks:
            buffer_tasks[message.chat.id] = asyncio.create_task(schedule_buffer_send(message.chat.id))
            get_logger().info("buffer_timer_created", chat_id=message.chat.id)

    @app.on_message((filters.voice | filters.audio) & ~filters.me)
    async def handle_voice(_: Client, message: Message):
        # Обрабатываем только адресованные нам сообщения
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id
        if not _is_for_me(message, my_id_box["id"]):
            return
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return

        # Индикация "печатает" пока обрабатываем
        # Скачиваем в память и загружаем на наш backend /upload
        try:
            # Mark photo processing in-flight for this chat to let following text attach as caption
            photo_inflight[message.chat.id] = time.monotonic()
            bio: BytesIO = await message.download(in_memory=True)  # type: ignore[assignment]
            # Определяем имя и mime
            mime: str = "application/octet-stream"
            filename: str = "audio.bin"
            duration: Optional[int] = None
            voice_file_id: Optional[str] = None
            if message.voice:
                filename = "voice.ogg"
                mime = getattr(message.voice, "mime_type", None) or "audio/ogg"
                duration = getattr(message.voice, "duration", None)
                voice_file_id = getattr(message.voice, "file_id", None)
            elif message.audio:
                filename = message.audio.file_name or "audio.mp3"
                mime = getattr(message.audio, "mime_type", None) or "audio/mpeg"
                duration = getattr(message.audio, "duration", None)
                voice_file_id = getattr(message.audio, "file_id", None)

            upload_url = str(settings.public_base_url).rstrip("/") + "/upload"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as hc:
                    files = {"file": (filename, bio.getvalue(), mime)}
                    resp = await hc.post(upload_url, files=files)
                    resp.raise_for_status()
                    data = resp.json()
                    audio_url = data.get("url")
                    if not audio_url:
                        raise RuntimeError("empty upload url")
            except Exception:
                # Тихо игнорируем (suppress)
                return

            # Отправляем в общий поток с медиаметаданными; текст – плейсхолдер
            media = {
                "origin": "voice",
                "audio_url": audio_url,
                "voice_file_id": voice_file_id,
                "mime_type": mime,
                "duration": duration,
            }
            # Отключаем локальный typing_loop — его реализует reply_flow через shim.send_chat_action
            if use_queue:
                await _enqueue_incoming(message, "[voice_message]", media, source="voice")
            else:
                await _process_single(app, shim, message, "[voice_message]", media, disable_local_typing=True)
        finally:
            # Clear in-flight flag
            photo_inflight.pop(message.chat.id, None)

    @app.on_message(((filters.photo) | (filters.document)) & ~filters.me)
    async def handle_photo(_: Client, message: Message):
        # Обрабатываем только адресованные нам сообщения
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id
        if not _is_for_me(message, my_id_box["id"]):
            return
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return

        # Если это документ, но не изображение — пропускаем
        if getattr(message, "document", None) and getattr(message.document, "mime_type", None):
            if not str(message.document.mime_type).startswith("image/"):
                return

        # Скачиваем изображение (Pyrogram сам возьмёт лучший размер для photo)
        try:
            bio: BytesIO = await message.download(in_memory=True)  # type: ignore[assignment]
            mime: str = "image/jpeg"
            filename: str = "photo.jpg"
            width = None
            height = None
            image_file_id = None
            if getattr(message, "photo", None):
                p = message.photo
                try:
                    width = getattr(p, "width", None)
                    height = getattr(p, "height", None)
                except Exception:
                    pass
                try:
                    image_file_id = getattr(p, "file_id", None)
                except Exception:
                    image_file_id = None
            if getattr(message, "document", None) and getattr(message.document, "mime_type", None):
                # document тут гарантированно image/* благодаря проверке выше
                mime = message.document.mime_type
                filename = message.document.file_name or filename
                image_file_id = message.document.file_id

            upload_url = str(settings.public_base_url).rstrip("/") + "/upload"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as hc:
                    files = {"file": (filename, bio.getvalue(), mime)}
                    resp = await hc.post(upload_url, files=files)
                    resp.raise_for_status()
                    data = resp.json()
                    image_url = data.get("url")
                    if not image_url:
                        raise RuntimeError("empty upload url")
            except Exception:
                # Тихо игнорируем (suppress)
                return

            media = {
                "origin": "photo",
                "image_url": image_url,
                "image_file_id": image_file_id,
                "image_mime_type": mime,
            }
            if width:
                media["width"] = width
            if height:
                media["height"] = height

            # caption если есть
            cap = message.caption or ""
            if use_queue:
                # В режиме очереди фото + caption сразу как единичное сообщение (агрегация caption уже есть)
                await _enqueue_incoming(message, cap, media, source="photo")
            else:
                await _process_single(app, shim, message, cap, media, disable_local_typing=True, use_buffer=True)
        finally:
            pass

    print("[userbot] starting... press Ctrl+C to stop")
    await app.start()
    # Отметим все новые чаты как proactive_via_userbot=True (если нужно поведение проактивов через userbot)
    async def mark_chat_state(chat_id: int):
        async with session_scope() as session:
            state = await session.get(ChatState, chat_id)
            if state and not state.proactive_via_userbot:
                state.proactive_via_userbot = True

    # Периодический воркер отправки outbox
    async def outbox_worker():
        while True:
            await asyncio.sleep(outbox_poll_seconds)
            try:
                async with session_scope() as session:
                    q = await session.execute(
                        ProactiveOutbox.__table__.select().where(ProactiveOutbox.sent_at.is_(None)).order_by(ProactiveOutbox.id).limit(20)
                    )
                    rows = q.fetchall()
                    if not rows:
                        continue
                    for row in rows:
                        row_obj = ProactiveOutbox(**row._mapping)
                        try:
                            await app.send_message(row_obj.chat_id, row_obj.text)
                            # Логируем как assistant message для целостности истории
                            session.add(AssistantMessage(chat_id=row_obj.chat_id, text=row_obj.text, meta_json=row_obj.meta_json))
                            # Обновляем state.last_assistant_at
                            state = await session.get(ChatState, row_obj.chat_id)
                            if state:
                                state.last_assistant_at = utcnow()
                            # mark sent
                            await session.execute(
                                ProactiveOutbox.__table__.update().where(ProactiveOutbox.id == row_obj.id).values(sent_at=utcnow(), attempts=row_obj.attempts + 1)
                            )
                        except Exception:
                            # increment attempts
                            await session.execute(
                                ProactiveOutbox.__table__.update().where(ProactiveOutbox.id == row_obj.id).values(attempts=row_obj.attempts + 1)
                            )
            except Exception:
                # глушим, чтобы воркер не падал
                pass

    asyncio.create_task(outbox_worker())

    async def tasks_worker():
        if not use_queue:
            return
        lease_sec = int(float(os.getenv("TASK_LEASE_SECONDS", "60")))
        heartbeat_every = max(10, int(float(os.getenv("TASK_HEARTBEAT_SECONDS", "30"))))
        last_hb: dict[int, float] = {}
        while True:
            await asyncio.sleep(0.5)
            try:
                async with session_scope() as session:
                    tasks = await lease_tasks(session, kinds=["incoming_user_message"], limit=5, lease_seconds=lease_sec)
                    if not tasks:
                        continue
                    get_logger().info("task_lease_batch", count=len(tasks))
                    for t in tasks:
                        payload = t.payload_json
                        start_monotonic = time.monotonic()
                        logger = get_logger().bind(task_id=t.id, attempt=t.attempts, chat_id=payload.get("chat_id"), trace_id=payload.get("trace_id"))
                        logger.info("task_start", kind=t.kind)
                        try:
                            await process_user_text(
                                shim,
                                session,
                                chat_id=payload["chat_id"],
                                chat_type=payload.get("chat_type", "private"),
                                user_id=payload.get("user_id"),
                                username=payload.get("username"),
                                lang=payload.get("lang"),
                                text=payload.get("text", ""),
                                media=payload.get("media"),
                                settings=settings,
                                trace_id=payload.get("trace_id"),
                                tg_message_id=payload.get("telegram_message_id"),
                            )
                            await complete(session, t.id, status="done")
                            metrics.inc("tasks_processed_total", labels={"kind": t.kind, "status": "done"})
                            metrics.observe("task_processing_seconds", time.monotonic() - start_monotonic, labels={"kind": t.kind})
                            logger.info("task_done", kind=t.kind)
                        except Exception as e:
                            # Классификация серверных ошибок n8n по тексту (исключение прокинуто из N8NServerError)
                            is_5xx = "n8n 5xx" in str(e)
                            if is_5xx and t.attempts < 5:
                                # Возвращаем задачу в pending с простым backoff через sleep перед повторным лизингом
                                await return_to_pending(session, [t.id])
                                metrics.inc("tasks_retried_total", labels={"kind": t.kind})
                                logger.warning("task_requeue", reason="n8n_5xx", attempts=t.attempts)
                            else:
                                await complete(session, t.id, status="failed", error=str(e)[:4000])
                                metrics.inc("tasks_processed_total", labels={"kind": t.kind, "status": "failed"})
                                logger.error("task_fail", error_class="n8n_5xx" if is_5xx else "exception", error=str(e))
                        else:
                            last_hb.pop(t.id, None)
                # Heartbeat processed tasks still running (в нашей реализации процесс сразу завершает, heartbeat нужен для длинных задач — оставлено заделом)
                now_mono = time.monotonic()
                to_hb: list[int] = []
                for tid, ts in list(last_hb.items()):
                    if now_mono - ts >= heartbeat_every:
                        to_hb.append(tid)
                        last_hb[tid] = now_mono
                if to_hb:
                    async with session_scope() as session:
                        for tid in to_hb:
                            await heartbeat(session, tid, lease_seconds=lease_sec)
            except Exception:
                pass

    asyncio.create_task(tasks_worker())

    async def watchdog_worker():
        if not use_queue:
            return
        interval = float(os.getenv("TASK_WATCHDOG_INTERVAL", "10"))
        while True:
            await asyncio.sleep(interval)
            try:
                async with session_scope() as session:
                    stats = await watchdog_pass(session)
                    if stats.get("returned"):
                        metrics.inc("tasks_retried_total", value=stats["returned"], labels={"kind": "incoming_user_message"})
                    if stats.get("failed"):
                        metrics.inc("tasks_failed_total", value=stats["failed"], labels={"kind": "incoming_user_message"})
                    if stats.get("returned") or stats.get("failed"):
                        get_logger().info("watchdog_pass", returned=stats.get("returned",0), failed=stats.get("failed",0))
            except Exception:
                pass

    asyncio.create_task(watchdog_worker())

    async def recovery_worker():
        if not use_queue:
            return
        # Одноразовый прогон после старта
        recovery_limit = int(float(os.getenv("RECOVERY_HISTORY_LIMIT", "500")))
        try:
            me = await app.get_me()
            my_id = me.id
        except Exception:
            return
        # Соберём чаты где есть state или сообщения (упрощённо: все chat_ids из ChatState)
        from sqlalchemy import select as _select
        async with session_scope() as session:
            rows = (await session.execute(_select(ChatState.chat_id))).all()
            chat_ids = [r[0] for r in rows]
        for cid in chat_ids:
            try:
                # Найти последний сохранённый tg_message_id
                async with session_scope() as session:
                    from sqlalchemy import func as _f
                    from app.db.models import Message as _Msg
                    max_row = (await session.execute(_select(_f.max(_Msg.tg_message_id)).where(_Msg.chat_id == cid))).scalar()
                    last_saved = max_row or 0
                missing: list[Message] = []
                count = 0
                async for h in app.get_chat_history(cid, limit=recovery_limit):
                    # Pyrogram возвращает от новых к старым. Останавливаемся когда дошли до сохранённого
                    if h.id <= last_saved:
                        break
                    if h.from_user and h.from_user.id == my_id:
                        continue  # пропускаем собственные ответы
                    if not (h.text or h.caption):
                        continue
                    missing.append(h)
                    count += 1
                    if count >= recovery_limit:
                        break
                if missing:
                    metrics.inc("recovery_gap_messages_total", value=len(missing), labels={"kind": "incoming_user_message"})
                    for m in sorted(missing, key=lambda x: x.id):
                        text_val = m.text or m.caption or ""
                        payload = {
                            "telegram_message_id": m.id,
                            "chat_id": cid,
                            "chat_type": _chat_type_str(m.chat.type if getattr(m, 'chat', None) else None),
                            "user_id": getattr(m.from_user, 'id', None) if m.from_user else None,
                            "username": getattr(m.from_user, 'username', None) if m.from_user else None,
                            "lang": getattr(m.from_user, 'language_code', None) if m.from_user else None,
                            "text": text_val,
                            "media": None,
                        }
                        async with session_scope() as session:
                            dedup = f"recovery:{cid}:{m.id}"
                            await enqueue_task(session, kind="incoming_user_message", payload=payload, priority=90, dedup_key=dedup)
                            metrics.inc("tasks_created_total", labels={"kind": "incoming_user_message", "source": "recovery"})
                # Assistant backfill (берём последние сообщения userbot'а если их нет в БД)
                try:
                    async with session_scope() as session:
                        from sqlalchemy import select as _select2
                        from app.db.models import AssistantMessage as _A
                        existing_a_ids = set(
                            r[0]
                            for r in (
                                await session.execute(
                                    _select2(_A.tg_message_id).where(_A.chat_id == cid, _A.tg_message_id.is_not(None))
                                )
                            ).all()
                        )
                    a_missing: list[Message] = []
                    count_a = 0
                    async for h in app.get_chat_history(cid, limit=recovery_limit):
                        if not (h.text or h.caption):
                            continue
                        if not (h.from_user and h.from_user.id == my_id):
                            continue
                        if h.id in existing_a_ids:
                            continue
                        a_missing.append(h)
                        count_a += 1
                        if count_a >= recovery_limit:
                            break
                    if a_missing:
                        metrics.inc("recovery_gap_messages_total", value=len(a_missing), labels={"kind": "assistant_backfill"})
                        async with session_scope() as session:
                            for am in sorted(a_missing, key=lambda x: x.id):
                                session.add(AssistantMessage(chat_id=cid, text=am.text or am.caption or "", meta_json={"recovered": True}))
                except Exception:
                    pass
            except Exception:
                continue

    asyncio.create_task(recovery_worker())
    try:
        await idle()
    finally:
        await app.stop()


async def idle():
    # Simple idle loop similar to pyrogram.idle()
    evt = asyncio.Event()

    def _stop(*_: object) -> None:
        evt.set()

    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(__import__("signal"), sig), _stop)
        except (NotImplementedError, AttributeError):
            # Windows may not support signal handlers; rely on KeyboardInterrupt
            pass
    try:
        await evt.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
