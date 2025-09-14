from __future__ import annotations

"""Application settings using Pydantic Settings.

Loads configuration from environment variables and optional .env file.
"""

import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import AnyHttpUrl, BaseModel, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ProactiveSettings(BaseModel):
    default_auto_messages: bool = True
    min_seconds: int = 3600
    max_seconds: int = 7200


class ReplyDelaySettings(BaseModel):
    min_seconds: int = 5
    max_seconds: int = 10


class AntiSpamSettings(BaseModel):
    user_min_seconds_between_msg: int = 5


class Settings(BaseSettings):
    """Top-level application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",  # allow flat extra env like AUTO_MESSAGES_DEFAULT
    )

    # Core
    telegram_bot_token: str
    webhook_secret: str
    public_base_url: AnyHttpUrl
    n8n_webhook_url: AnyHttpUrl

    # Optional headers for n8n/OpenRouter attribution
    openrouter_referrer: Optional[str] = None

    # Database
    db_dsn: PostgresDsn

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8080

    # Logging
    log_level: str = "INFO"

    # Features
    proactive: ProactiveSettings = ProactiveSettings()
    reply_delay: ReplyDelaySettings = ReplyDelaySettings()
    antispam: AntiSpamSettings = AntiSpamSettings()

    # Limits
    max_user_text_len: int = 4000

    @field_validator("log_level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in levels:
            raise ValueError("Invalid LOG_LEVEL")
        return v.upper()

    def model_post_init(self, __context: dict[str, object]) -> None:  # type: ignore[override]
        """Map flat env vars into nested settings for convenience."""

        def _get_bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.lower() in {"1", "true", "yes", "y", "on"}

        def _get_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            return int(raw) if raw is not None else default

        self.proactive.default_auto_messages = _get_bool("AUTO_MESSAGES_DEFAULT", self.proactive.default_auto_messages)
        self.proactive.min_seconds = _get_int("PROACTIVE_MIN_SECONDS", self.proactive.min_seconds)
        self.proactive.max_seconds = _get_int("PROACTIVE_MAX_SECONDS", self.proactive.max_seconds)

        self.reply_delay.min_seconds = _get_int("REPLY_DELAY_MIN_SECONDS", self.reply_delay.min_seconds)
        self.reply_delay.max_seconds = _get_int("REPLY_DELAY_MAX_SECONDS", self.reply_delay.max_seconds)

        self.antispam.user_min_seconds_between_msg = _get_int(
            "USER_MIN_SECONDS_BETWEEN_MSG", self.antispam.user_min_seconds_between_msg
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""

    return Settings()  # type: ignore[call-arg]
