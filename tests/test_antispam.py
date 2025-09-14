from __future__ import annotations

from datetime import timedelta, timezone

from app.bot.services.anti_spam import is_allowed, remaining_wait_seconds
from app.utils.time import utcnow


def test_antispam_under_window():
    now = utcnow()
    last = now - timedelta(seconds=4)
    assert not is_allowed(last, now, 5)
    assert remaining_wait_seconds(last, now, 5) >= 1


def test_antispam_over_window():
    now = utcnow()
    last = now - timedelta(seconds=6)
    assert is_allowed(last, now, 5)
    assert remaining_wait_seconds(last, now, 5) == 0

