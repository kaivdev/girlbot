from __future__ import annotations

"""Application settings using Pydantic Settings.

Loads configuration from environment variables and optional .env file.
"""

import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import AnyHttpUrl, BaseModel, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ProactiveSettings(BaseModel):
    default_auto_messages: bool = True
    min_seconds: int = 3600
    max_seconds: int = 7200
    # Enable/disable generic fallback intent via env PROACTIVE_GENERIC_ENABLED
    generic_enabled: bool = True


class ReplyDelaySettings(BaseModel):
    min_seconds: int = 5
    max_seconds: int = 10
    # Редкая длинная задержка: вероятность (0..1) и диапазон секунд
    rare_long_probability: float = 0.0
    rare_long_min_seconds: int = 180
    rare_long_max_seconds: int = 360
    # Первый ответ после долгой паузы (детерминированно)
    inactivity_long_threshold_minutes: int = 120
    inactivity_long_min_seconds: int = 180
    inactivity_long_max_seconds: int = 300


class AntiSpamSettings(BaseModel):
    user_min_seconds_between_msg: int = 5

class ModerationSettings(BaseModel):
    abuse_enabled: bool = True
    abuse_mute_hours: int = 24


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
    moderation: ModerationSettings = ModerationSettings()

    # Proactive extended windows (HH:MM-HH:MM). Quiet hours, morning, evening
    proactive_morning_window: str | None = None  # e.g. "07:00-09:30"
    proactive_evening_window: str | None = None  # e.g. "22:30-00:30"
    proactive_quiet_window: str | None = None    # e.g. "00:30-07:00"
    reengage_min_hours: int = 6
    reengage_cooldown_hours: int = 12
    # Global default timezone offset (minutes from UTC). Moscow = +180
    default_timezone_offset_minutes: int = 180

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

        self.proactive.default_auto_messages = _get_bool_env("AUTO_MESSAGES_DEFAULT", self.proactive.default_auto_messages)
        self.proactive.min_seconds = _get_int_env("PROACTIVE_MIN_SECONDS", self.proactive.min_seconds)
        self.proactive.max_seconds = _get_int_env("PROACTIVE_MAX_SECONDS", self.proactive.max_seconds)
        # Optional: disable generic proactive via env
        self.proactive.generic_enabled = _get_bool_env("PROACTIVE_GENERIC_ENABLED", self.proactive.generic_enabled)

        self.reply_delay.min_seconds = _get_int_env("REPLY_DELAY_MIN_SECONDS", self.reply_delay.min_seconds)
        self.reply_delay.max_seconds = _get_int_env("REPLY_DELAY_MAX_SECONDS", self.reply_delay.max_seconds)
        # Дополнительно читаем редкую длинную задержку (если заданы)
        try:
            rl_prob = os.getenv("REPLY_RARE_LONG_PROB")
            if rl_prob is not None:
                self.reply_delay.rare_long_probability = float(rl_prob)
            self.reply_delay.rare_long_min_seconds = _get_int_env(
                "REPLY_RARE_LONG_MIN_SECONDS", self.reply_delay.rare_long_min_seconds
            )
            self.reply_delay.rare_long_max_seconds = _get_int_env(
                "REPLY_RARE_LONG_MAX_SECONDS", self.reply_delay.rare_long_max_seconds
            )
            # Long inactivity deterministic delay
            self.reply_delay.inactivity_long_threshold_minutes = _get_int_env(
                "REPLY_INACTIVITY_LONG_THRESHOLD_MINUTES", self.reply_delay.inactivity_long_threshold_minutes
            )
            self.reply_delay.inactivity_long_min_seconds = _get_int_env(
                "REPLY_INACTIVITY_LONG_MIN_SECONDS", self.reply_delay.inactivity_long_min_seconds
            )
            self.reply_delay.inactivity_long_max_seconds = _get_int_env(
                "REPLY_INACTIVITY_LONG_MAX_SECONDS", self.reply_delay.inactivity_long_max_seconds
            )
        except Exception:
            pass

        self.antispam.user_min_seconds_between_msg = _get_int_env(
            "USER_MIN_SECONDS_BETWEEN_MSG", self.antispam.user_min_seconds_between_msg
        )
        # Moderation
        self.moderation.abuse_enabled = _get_bool_env("ABUSE_ENABLED", self.moderation.abuse_enabled)
        self.moderation.abuse_mute_hours = _get_int_env("ABUSE_MUTE_HOURS", self.moderation.abuse_mute_hours)
        # Nothing else: окна читаем как строки, числа уже есть
        # Default timezone offset fallback (supports two env var names)
        try:
            self.default_timezone_offset_minutes = _get_int_env(
                "DEFAULT_TIMEZONE_OFFSET_MINUTES",
                _get_int_env("DEFAULT_TZ_OFFSET_MINUTES", self.default_timezone_offset_minutes),
            )
        except Exception:
            pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""

    return Settings()  # type: ignore[call-arg]
