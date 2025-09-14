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


@messages_router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока поддерживается только текст")
