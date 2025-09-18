from __future__ import annotations

"""Pydantic models for n8n I/O contract."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime


class Context(BaseModel):
    history: list[HistoryItem] = Field(default_factory=list)
    last_user_msg_at: Optional[datetime] = None
    last_assistant_at: Optional[datetime] = None


class ChatInfo(BaseModel):
    chat_id: int
    user_id: Optional[int] = None
    lang: Optional[str] = None
    username: Optional[str] = None
    persona: Optional[str] = None
    memory_rev: Optional[int] = None


class MessageIn(BaseModel):
    text: Optional[str] = None
    # Optional media fields for voice/audio workflows handled in n8n
    origin: Optional[Literal["text", "voice", "audio"]] = None
    audio_url: Optional[str] = None  # direct URL n8n can download
    voice_file_id: Optional[str] = None  # Telegram file_id (if n8n will resolve)
    mime_type: Optional[str] = None
    duration: Optional[int] = None  # seconds

    model_config = {
        "extra": "allow",  # allow further media-specific fields if needed
    }


class N8nRequest(BaseModel):
    intent: Literal[
        "reply",
        "proactive_morning",
        "proactive_evening",
        "proactive_reengage",
        "proactive_generic",
        "user_goodnight",
        "goodnight_followup",
    ]
    chat: ChatInfo
    context: Context
    message: Optional[MessageIn] = None
    trace_id: Optional[str] = None


class Meta(BaseModel):
    model: Optional[str] = None
    tokens: Optional[int] = None

    model_config = {
        "extra": "allow",  # allow extra fields from n8n
    }


class N8nResponse(BaseModel):
    reply: str
    meta: Meta = Field(default_factory=Meta)
