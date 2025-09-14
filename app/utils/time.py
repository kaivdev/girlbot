from __future__ import annotations

"""Time utilities: utcnow and jitter helpers."""

from datetime import datetime, timedelta, timezone
import random


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""

    return datetime.now(timezone.utc)


def jitter_seconds(min_seconds: int, max_seconds: int) -> int:
    """Return a random integer in [min_seconds, max_seconds]."""

    if min_seconds > max_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds
    return random.randint(min_seconds, max_seconds)


def future_with_jitter(min_seconds: int, max_seconds: int, *, base: datetime | None = None) -> datetime:
    """Return a datetime = base + random seconds in [min_seconds, max_seconds]."""

    base = base or utcnow()
    return base + timedelta(seconds=jitter_seconds(min_seconds, max_seconds))

