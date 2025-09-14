from __future__ import annotations

"""Webhook endpoint for Telegram updates."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from aiogram.types import Update

from app.config.settings import get_settings, Settings


router = APIRouter()


def _check_secret(secret: str | None, settings: Settings) -> None:
    if not secret or secret != settings.webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid secret")


@router.post("/tg/webhook")
async def telegram_webhook(request: Request, secret: str | None = None, settings: Settings = Depends(get_settings)) -> dict:
    _check_secret(secret, settings)
    body = await request.json()
    update = Update.model_validate(body)
    bot = request.app.state.bot
    dp = request.app.state.dp
    await dp.feed_update(bot, update)
    return {"ok": True}

