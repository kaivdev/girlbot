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
from app.bot.services.reply_flow import process_user_text
from app.db.models import ProactiveOutbox, AssistantMessage, ChatState, Message as DBMessage, Chat
from app.utils.time import utcnow


class PyroBotShim:
    """Minimal shim to satisfy process_user_text(bot=...)."""

    def __init__(self, client: Client, reply_selector: Optional[callable] = None):
        self.client = client
        self.reply_selector = reply_selector

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

    def _select_reply_to(chat_id: int) -> Optional[int]:
        if not reply_quote_enabled:
            return None
        # init structures
        cnt = reply_counters.get(chat_id, 0) + 1
        reply_counters[chat_id] = cnt
        thr = reply_thresholds.get(chat_id)
        if thr is None or thr <= 0:
            thr = _new_threshold()
            reply_thresholds[chat_id] = thr
        if cnt >= thr:
            # reset for next cycle
            reply_counters[chat_id] = 0
            reply_thresholds[chat_id] = _new_threshold()
            dq = recent_user_msgs.get(chat_id)
            if dq and len(dq) > 0:
                # pick a random recent message id to reply to
                try:
                    idx = random.randrange(0, len(dq))
                    return list(dq)[idx]
                except Exception:
                    try:
                        return dq[-1]
                    except Exception:
                        return None
        return None

    shim = PyroBotShim(app, reply_selector=_select_reply_to)

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
    cancel_on_new_msg = os.getenv("USERBOT_CANCEL_ON_NEW_MSG", "1").lower() in {"1","true","yes","on"}

    # Буфер по chat_id
    message_buffers: Dict[int, List[Message]] = {}
    buffer_tasks: Dict[int, asyncio.Task] = {}
    inflight_tasks: Dict[int, asyncio.Task] = {}

    def _start_generation(chat_id: int, base: Message, combined_text: str):
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
        _start_generation(chat_id, base, combined_text)

    async def schedule_buffer_send(chat_id: int):
        # ждём пока либо тишина >= batch_quiet_grace с момента последнего изменения,
        # либо истечёт batch_max_wait с момента первого сообщения в буфере
        start = time.monotonic()
        last_len = len(message_buffers.get(chat_id, []))
        last_change_ts = start
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            # тишина достаточная?
            if len(message_buffers.get(chat_id, [])) > 0 and (now - last_change_ts) >= batch_quiet_grace:
                break
            # превысили общий максимум ожидания
            if now - start >= batch_max_wait:
                break
            # отслеживаем изменения размера буфера
            current = len(message_buffers.get(chat_id, []))
            if current != last_len:
                last_len = current
                last_change_ts = now
        await flush_buffer(chat_id)
        buffer_tasks.pop(chat_id, None)

    async def _process_single(app: Client, shim: PyroBotShim, message: Message, text: str, media: Optional[Dict] = None, *, disable_local_typing: bool = False):
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
                        trace_id=None,
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

        if not batch_enabled:
            await _process_single(app, shim, message, message.text or "")
            return

        # Если во время генерации пришло новое сообщение — по желанию отменяем генерацию
        if cancel_on_new_msg:
            task = inflight_tasks.get(message.chat.id)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(Exception):
                    await task

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
                await app.send_message(message.chat.id, "Не получилось обработать голос, пришли текстом?")
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
            await _process_single(app, shim, message, "[voice_message]", media, disable_local_typing=True)
        finally:
            pass

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
                await app.send_message(message.chat.id, "Не получилось обработать фото, пришли текстом?")
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

            await _process_single(app, shim, message, "[photo]", media, disable_local_typing=True)
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
