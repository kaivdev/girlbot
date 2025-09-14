from __future__ import annotations

"""
Interactive helper to generate a Pyrogram session string.

Usage:
  1) Put TELEGRAM_API_ID and TELEGRAM_API_HASH in .env or export them
  2) Run: python -m scripts.gen_pyrogram_session
  3) Follow prompts (phone number, code). Copy the session string into .env as PYROGRAM_SESSION_STRING
"""

import asyncio
import os
from getpass import getpass

from dotenv import load_dotenv
from pyrogram import Client


async def main() -> None:
    load_dotenv()
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env or env vars")

    # in_memory=True so no session file is written; we only export the string
    async with Client("gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        session = await app.export_session_string()
        print("\nYour PYROGRAM_SESSION_STRING:\n")
        print(session)
        print("\nPaste it into .env as PYROGRAM_SESSION_STRING=...")


if __name__ == "__main__":
    asyncio.run(main())

