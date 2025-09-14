from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.bot.services.proactive import compute_next_proactive_at


@dataclass
class _Proactive:
    min_seconds: int
    max_seconds: int


@dataclass
class _Settings:
    proactive: _Proactive


def test_compute_next_proactive_at_bounds():
    now = datetime.now(timezone.utc)
    settings = _Settings(proactive=_Proactive(min_seconds=3600, max_seconds=7200))
    dt = compute_next_proactive_at(now, settings)  # type: ignore[arg-type]
    delta = (dt - now).total_seconds()
    assert 3600 <= delta <= 7200

