from __future__ import annotations

"""Initialize aiogram bot, dispatcher, DB session factory, and scheduler."""

from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI

from app.bot.handlers.commands import commands_router
from app.bot.handlers.messages import messages_router
from app.bot.services.logging import get_logger
from app.bot.services.proactive import start_scheduler
from app.config.settings import get_settings
from app.db.base import AsyncSessionFactory, session_scope


logger = get_logger()


class BotContext:
    bot: Bot
    dp: Dispatcher
    scheduler: Any


def setup_bot(app: FastAPI) -> BotContext:
    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.include_router(messages_router)

    scheduler = start_scheduler(session_scope, bot, settings)

    # attach to app state for reuse
    app.state.bot = bot
    app.state.dp = dp
    app.state.scheduler = scheduler

    logger.info("bot_setup_complete", level=settings.log_level)
    return BotContext()

