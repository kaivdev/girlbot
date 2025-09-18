from __future__ import annotations

"""Speech-to-text helpers (optional, primarily handled by n8n).

This module provides a small helper to expose a consistent API from the app
side if we ever want to run STT locally. For now, n8n will do the actual
transcription (OpenAI Whisper or other), and we just keep a placeholder here
to avoid tight coupling.
"""

from typing import Optional


async def transcribe_from_url(url: str, *, language: Optional[str] = None) -> str:
    """Placeholder STT function.

    In current architecture, n8n performs STT and passes text back. This
    function exists to make future migration trivial if we decide to move STT
    into the bot service. For now, it raises NotImplementedError to avoid
    accidental use.
    """
    raise NotImplementedError("STT is handled by n8n in this setup")
