from __future__ import annotations

"""Message handlers for text and others."""

from aiogram import F, Router
from aiogram.types import Message

from app.bot.services.reply_flow import process_user_text, buffer_or_process, flush_pending_input
from app.bot.services.media_upload import get_file_bytes, upload_bytes
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
        # Попытка авто-флаша просроченного буфера перед обработкой нового текста
        await flush_pending_input(message.bot, session, chat_id=chat.id, settings=settings)
        await buffer_or_process(
            message.bot,
            session,
            chat_id=chat.id,
            chat_type=chat.type,
            user_id=(user.id if user else None),
            username=(user.username if user else None),
            lang=(user.language_code if user else None),
            text=text,
            media=None,
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

    caption = message.caption or message.caption_html or ""
    # Для фото текст буфера = caption (без placeholder)
    async with session_scope() as session:
        await flush_pending_input(message.bot, session, chat_id=chat.id, settings=settings)
        await buffer_or_process(
            message.bot,
            session,
            chat_id=chat.id,
            chat_type=chat.type,
            user_id=(user.id if user else None),
            username=(user.username if user else None),
            lang=(user.language_code if user else None),
            text=caption,
            media=media,
            settings=settings,
            trace_id=None,
        )


@messages_router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока поддерживаются текст, голос и фото")


@messages_router.message(F.photo)
async def on_photo(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    chat = message.chat

    # Choose best available photo
    file_id = None
    width = None
    height = None
    mime_type = None
    filename = None

    if message.photo and len(message.photo) > 0:
        p = message.photo[-1]
        file_id = p.file_id
        width = p.width
        height = p.height
        filename = f"photo_{p.file_unique_id}.jpg"
        mime_type = "image/jpeg"
    if not file_id:
        await message.answer("Не смог получить фото :(")
        return

    # Download from Telegram and upload to our backend to get a public URL
    try:
        data, inferred_mime, suggested = await get_file_bytes(message.bot, file_id)
        ct = mime_type or inferred_mime or "application/octet-stream"
        name = filename or suggested or "image.bin"
        up = await upload_bytes(name, ct, data)
        image_url = up.get("url")
    except Exception:
        image_url = None

    media = {"origin": "photo"}
    if image_url:
        media["image_url"] = image_url
        media["image_mime_type"] = mime_type
    else:
        # Fallback: pass file_id so n8n could fetch via Bot API if needed
        media["image_file_id"] = file_id
        if mime_type:
            media["image_mime_type"] = mime_type
    if width:
        media["width"] = width
    if height:
        media["height"] = height

    placeholder = "[photo]"

    caption = message.caption or message.caption_html or ""
    async with session_scope() as session:
        await flush_pending_input(message.bot, session, chat_id=chat.id, settings=settings)
        await buffer_or_process(
            message.bot,
            session,
            chat_id=chat.id,
            chat_type=chat.type,
            user_id=(user.id if user else None),
            username=(user.username if user else None),
            lang=(user.language_code if user else None),
            text=caption,
            media=media,
            settings=settings,
            trace_id=None,
        )


@messages_router.message(F.document)
async def on_image_document(message: Message) -> None:
    # Process only image documents
    doc = message.document
    if not doc or not (doc.mime_type and doc.mime_type.startswith("image/")):
        return

    settings = get_settings()
    user = message.from_user
    chat = message.chat

    file_id = doc.file_id
    mime_type = doc.mime_type
    filename = doc.file_name or f"image_{doc.file_unique_id}"

    try:
        data, inferred_mime, suggested = await get_file_bytes(message.bot, file_id)
        ct = mime_type or inferred_mime or "application/octet-stream"
        name = filename or suggested or "image.bin"
        up = await upload_bytes(name, ct, data)
        image_url = up.get("url")
    except Exception:
        image_url = None

    media = {"origin": "photo"}
    if image_url:
        media["image_url"] = image_url
        media["image_mime_type"] = mime_type
    else:
        media["image_file_id"] = file_id
        if mime_type:
            media["image_mime_type"] = mime_type

    placeholder = "[photo]"

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
