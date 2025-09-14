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
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ChatType
from pyrogram.types import Message

from app.config.settings import get_settings
from app.db.base import session_scope
from app.bot.services.reply_flow import process_user_text


class PyroBotShim:
    """Minimal shim to satisfy process_user_text(bot=...)."""

    def __init__(self, client: Client):
        self.client = client

    async def send_message(self, chat_id: int, text: str):  # aiogram-like surface
        return await self.client.send_message(chat_id, text)


async def typing_loop(client: Client, chat_id: int):
    """Continuously send typing action every ~4 seconds until cancelled."""
    try:
        while True:
            await client.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
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

    my_id_box: dict[str, int] = {}

    @app.on_message(filters.text & ~filters.me)
    async def handle_text(_: Client, message: Message):
        # Lazy initialize my own user id
        if not my_id_box:
            me = await app.get_me()
            my_id_box["id"] = me.id

        # Only react when the message is "for me"
        if not _is_for_me(message, my_id_box["id"]):
            return

        # Ignore bot senders
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return

        chat = message.chat
        user = message.from_user
        text = message.text or ""

        # Show typing while we think/respond
        typing_task = asyncio.create_task(typing_loop(app, chat.id))
        try:
            async with session_scope() as session:
                await process_user_text(
                    shim,  # aiogram-like bot shim
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
        finally:
            typing_task.cancel()
            with contextlib.suppress(Exception):
                await typing_task

    print("[userbot] starting... press Ctrl+C to stop")
    await app.start()
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
