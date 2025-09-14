from __future__ import annotations

"""Command handlers: /start, /help, /auto_on, /auto_off."""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, delete

from app.config.settings import get_settings
from app.db.base import session_scope
from app.db.models import Chat, ChatState, User, Message as DBMessage, AssistantMessage as DBAssistantMessage
from app.utils.time import future_with_jitter, utcnow


commands_router = Router(name="commands")


@commands_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    chat = message.chat
    if user is None:
        await message.answer("Привет! Я готов к работе.")
        return

    async with session_scope() as session:
        # ensure chat and user
        if await session.get(Chat, chat.id) is None:
            session.add(Chat(id=chat.id, type=chat.type))
        u = await session.get(User, user.id)
        if u is None:
            session.add(User(id=user.id, username=user.username, lang=user.language_code))
        state = await session.get(ChatState, chat.id)
        now = utcnow()
        if state is None:
            state = ChatState(chat_id=chat.id, auto_enabled=bool(settings.proactive.default_auto_messages))
            session.add(state)
        state.auto_enabled = bool(settings.proactive.default_auto_messages)
        state.next_proactive_at = future_with_jitter(settings.proactive.min_seconds, settings.proactive.max_seconds, base=now)

    text = (
        "Привет! Я твоя собеседница. Выбери стиль общения:\n\n"
        "— Ника: милая и игривая\n"
        "— Ивания: спокойная и заботливая\n\n"
        "Команды: /help, /persona (сменить персонажа), /auto_on, /auto_off."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Ника", callback_data="persona:nika")
    kb.button(text="Ивания", callback_data="persona:ivania")
    kb.adjust(2)
    await message.answer(text, reply_markup=kb.as_markup())


@commands_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я отвечаю только на текстовые сообщения.\n"
        "Команды: /start, /help, /persona — выбрать персонажа, /reset — очистить контекст, /auto_on, /auto_off."
    )
    await message.answer(text)


@commands_router.message(Command("persona"))
async def cmd_persona(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="Ника", callback_data="persona:nika")
    kb.button(text="Ивания", callback_data="persona:ivania")
    kb.adjust(2)
    await message.answer("Кого выбираешь?", reply_markup=kb.as_markup())


@commands_router.message(Command("auto_on"))
async def cmd_auto_on(message: Message) -> None:
    async with session_scope() as session:
        state = await session.get(ChatState, message.chat.id)
        if state is None:
            state = ChatState(chat_id=message.chat.id, auto_enabled=True)
            session.add(state)
        state.auto_enabled = True
    await message.answer("Проактивный режим включён")


@commands_router.message(Command("auto_off"))
async def cmd_auto_off(message: Message) -> None:
    async with session_scope() as session:
        state = await session.get(ChatState, message.chat.id)
        if state is None:
            state = ChatState(chat_id=message.chat.id, auto_enabled=False)
            session.add(state)
        state.auto_enabled = False
    await message.answer("Проактивный режим выключен")


@commands_router.callback_query(F.data.startswith("persona:"))
async def on_persona_selected(query: CallbackQuery) -> None:
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None:
        await query.answer()
        return
    key = query.data.split(":", 1)[1]
    if key not in {"nika", "ivania"}:
        await query.answer("Неизвестный выбор", show_alert=True)
        return
    async with session_scope() as session:
        state = await session.get(ChatState, chat_id)
        if state is None:
            state = ChatState(chat_id=chat_id)
            session.add(state)
        state.persona_key = key
    names = {"nika": "Ника", "ivania": "Ивания"}
    desc = {
        "nika": "милая и игривая",
        "ivania": "спокойная и заботливая",
    }
    text = f"Персона выбрана: {names[key]} — {desc[key]}. Пиши сообщение!"
    try:
        await query.message.edit_text(text)
    except Exception:
        await query.message.answer(text)
    await query.answer("Готово!")


@commands_router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """Очистить контекст чата: историю в БД и память n8n (через смену версии)."""

    chat_id = message.chat.id
    async with session_scope() as session:
        # wipe messages history
        await session.execute(delete(DBMessage).where(DBMessage.chat_id == chat_id))
        await session.execute(delete(DBAssistantMessage).where(DBAssistantMessage.chat_id == chat_id))
        state = await session.get(ChatState, chat_id)
        if state is None:
            state = ChatState(chat_id=chat_id)
            session.add(state)
        # reset timestamps and bump memory revision to drop Simple Memory session
        state.last_user_msg_at = None
        state.last_assistant_at = None
        state.next_proactive_at = None
        state.memory_rev = (state.memory_rev or 1) + 1

    await message.answer("Контекст очищен: история сброшена, память перезапущена. Можешь продолжать.")
