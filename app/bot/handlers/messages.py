from __future__ import annotations

"""Message handlers for text and others."""

from aiogram import F, Router
from aiogram.types import Message

from app.bot.services.reply_flow import process_user_text
from app.config.settings import get_settings
from app.db.base import session_scope


messages_router = Router(name="messages")


# Обрабатываем только обычный текст, исключая команды вида "/..."
# В aiogram v3 `Command()` требует указать команды, поэтому для исключения
# используем предикат: текст не начинается с "/".
@messages_router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    chat = message.chat
    text = message.text or ""

    async with session_scope() as session:
        await process_user_text(
            message.bot,
            session,
            chat_id=chat.id,
            chat_type=chat.type,
            user_id=(user.id if user else None),
            username=(user.username if user else None),
            lang=(user.language_code if user else None),
            text=text,
            settings=settings,
            trace_id=None,
        )


@messages_router.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    """Handle Telegram voice/audio by sending a special marker text.

    We keep the app-side simple: we forward a short marker text to n8n;
    the n8n workflow should detect `origin: voice` and perform STT, then
    continue the usual pipeline.
    """
    settings = get_settings()
    user = message.from_user
    chat = message.chat

    # Build media metadata for n8n to perform STT
    media: dict = {"origin": "voice"}
    if message.voice:
        media.update({
            "voice_file_id": message.voice.file_id,
            "mime_type": message.voice.mime_type,
            "duration": message.voice.duration,
        })
    elif message.audio:
        media.update({
            "voice_file_id": message.audio.file_id,
            "mime_type": message.audio.mime_type,
            "duration": message.audio.duration,
        })
    # Placeholder text; transcription will replace it in n8n pipeline
    placeholder = "[voice_message]"

    async with session_scope() as session:
        await process_user_text(
            message.bot,
            session,
            chat_id=chat.id,
            chat_type=chat.type,
            user_id=(user.id if user else None),
            username=(user.username if user else None),
            lang=(user.language_code if user else None),
            text=placeholder,
            media=media,
            settings=settings,
            trace_id=None,
        )


@messages_router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока поддерживаются текст и голосовые сообщения")
