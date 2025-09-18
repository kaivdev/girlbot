from __future__ import annotations

import io
from typing import Optional

from aiogram import Bot
import httpx

from app.config.settings import get_settings


async def upload_bytes(filename: str, content_type: str | None, data: bytes) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        files = {"file": (filename, io.BytesIO(data), content_type or "application/octet-stream")}
        resp = await client.post(str(settings.public_base_url).rstrip("/") + "/upload", files=files)
        resp.raise_for_status()
        return resp.json()


async def get_file_bytes(bot: Bot, file_id: str) -> tuple[bytes, Optional[str], str]:
    file = await bot.get_file(file_id)
    # aiogram v3 provides file_path to build download URL
    # We'll use Bot.download_file to bytes
    buf = io.BytesIO()
    await bot.download_file(file_path=file.file_path, destination=buf)  # type: ignore[arg-type]
    data = buf.getvalue()
    # We cannot reliably detect mime from Telegram here; caller may pass it
    # Return bytes, mime_type(None), suggested filename
    suggested = file.file_path.split("/")[-1] if getattr(file, "file_path", None) else f"file_{file_id}"
    return data, None, suggested
