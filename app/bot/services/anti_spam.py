from __future__ import annotations

"""Anti-spam logic: enforce minimal interval between user messages."""

from datetime import datetime, timezone


def remaining_wait_seconds(last_user_msg_at: datetime | None, now: datetime, min_gap_seconds: int) -> int:
    """Return remaining seconds to wait to satisfy min_gap_seconds.

    If last_user_msg_at is None or already past the gap, returns 0.
    """

    if last_user_msg_at is None:
        return 0
    # Ensure timezone-aware math
    if last_user_msg_at.tzinfo is None:
        last_user_msg_at = last_user_msg_at.replace(tzinfo=timezone.utc)
    diff = (now - last_user_msg_at).total_seconds()
    wait = max(0, min_gap_seconds - int(diff))
    return wait


def is_allowed(last_user_msg_at: datetime | None, now: datetime, min_gap_seconds: int) -> bool:
    """Return True if user may send next message now."""

    return remaining_wait_seconds(last_user_msg_at, now, min_gap_seconds) == 0
