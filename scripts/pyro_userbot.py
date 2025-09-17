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
from contextlib import asynccontextmanager
from typing import Optional, Dict, List
import time

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ChatType
from pyrogram.types import Message

from app.config.settings import get_settings
from app.db.base import session_scope
from app.bot.services.reply_flow import process_user_text
from app.db.models import ProactiveOutbox, AssistantMessage, ChatState
from app.utils.time import utcnow


class PyroBotShim:
    """Minimal shim to satisfy process_user_text(bot=...)."""

    def __init__(self, client: Client):
        self.client = client

    async def send_message(self, chat_id: int, text: str):  # aiogram-like surface
        return await self.client.send_message(chat_id, text)


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

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_string: Optional[str] = os.getenv("PYROGRAM_SESSION_STRING")
    if not api_id or not api_hash:
        raise RuntimeError("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")

    if session_string:
        app = Client(name="userbot", api_id=api_id, api_hash=api_hash, session_string=session_string)
    else:
        # Will create a local session file named `userbot.session` and ask for login
        app = Client(name="userbot", api_id=api_id, api_hash=api_hash)

    shim = PyroBotShim(app)

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
    batch_max_wait = float(os.getenv("USERBOT_BATCH_MAX_WAIT", "3.0"))   # сек макс ожидание перед ответом
    batch_quiet_grace = float(os.getenv("USERBOT_BATCH_QUIET_GRACE", "1.0"))  # сколько тишины нужно перед отправкой
    batch_max_messages = int(os.getenv("USERBOT_BATCH_MAX_MESSAGES", "5"))    # максимум объединяемых сообщений
    outbox_poll_seconds = float(os.getenv("USERBOT_OUTBOX_POLL_SECONDS", "10"))

    # Буфер по chat_id
    message_buffers: Dict[int, List[Message]] = {}
    buffer_tasks: Dict[int, asyncio.Task] = {}

    async def flush_buffer(chat_id: int):
        msgs = message_buffers.get(chat_id, [])
        if not msgs:
            return
        # Берём последний для метаданных, объединяем тексты
        base = msgs[-1]
        user = base.from_user
        combined_text = " \n".join(m.text or "" for m in msgs)
        message_buffers[chat_id] = []
        # Дальнейшая логика идентична одиночному сообщению: создаём «виртуальный» Message-подобный контекст
        await _process_single(app, shim, base, combined_text)

    async def schedule_buffer_send(chat_id: int):
        # ждём пока либо тишина, либо истечёт max_wait
        start = time.monotonic()
        last_len = len(message_buffers.get(chat_id, []))
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            # если превысили max_wait — отправляем
            if now - start >= batch_max_wait:
                break
            # если размер не меняется дольше grace и не пусто — завершаем
            current = len(message_buffers.get(chat_id, []))
            if current == last_len and current > 0 and (now - start) >= batch_quiet_grace:
                break
            if current != last_len:
                last_len = current
        await flush_buffer(chat_id)
        buffer_tasks.pop(chat_id, None)

    async def _process_single(app: Client, shim: PyroBotShim, message: Message, text: str):
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
        if typing_enabled:
            typing_task = asyncio.create_task(typing_loop(app, chat.id, typing_interval))
        try:
            async with session_scope() as session:
                await process_user_text(
                    shim,
                    session,
                    chat_id=chat.id,
                    chat_type=_chat_type_str(chat.type),
                    user_id=(user.id if user else None),
                    username=(user.username if user else None),
                    lang=(getattr(user, "language_code", None) if user else None),
                    text=text,
                    settings=settings,
                    trace_id=None,
                )
            # Как только реальный ответ отправлен (функция вернулась) — убираем индикацию набора,
            # чтобы не возникал повторный "всплеск" typing через пару секунд.
            if typing_task:
                typing_task.cancel()
                with contextlib.suppress(Exception):
                    await typing_task
            if typing_enabled and min_typing_seconds > 0:
                # Додерживаем минимальное время ТОЛЬКО если ответ пришёл слишком быстро,
                # но уже без новых send_chat_action (индикатор просто погаснет чуть раньше — это ок).
                elapsed = time.monotonic() - start_t
                remaining = min_typing_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            if mark_read_mode in {"after_reply", "delay"}:
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

        if not batch_enabled:
            await _process_single(app, shim, message, message.text or "")
            return

        buf = message_buffers.setdefault(message.chat.id, [])
        buf.append(message)
        # Если превысили лимит — сразу flush
        if len(buf) >= batch_max_messages:
            if task := buffer_tasks.get(message.chat.id):
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
            await flush_buffer(message.chat.id)
            return
        # Если нет активного таска — запускаем
        if message.chat.id not in buffer_tasks:
            buffer_tasks[message.chat.id] = asyncio.create_task(schedule_buffer_send(message.chat.id))

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
