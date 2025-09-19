from __future__ import annotations

"""Watchdog для восстановления зависших задач tasks.

Правила:
  - Задачи в статусе processing с истекшим lease_expires_at возвращаются в pending.
  - Если attempts превышает MAX_ATTEMPTS -> переводим в failed.
"""

from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Task
from app.utils.time import utcnow

MAX_ATTEMPTS = 5


async def watchdog_pass(session: AsyncSession) -> dict[str, int]:
    now = utcnow()
    stats = {"returned": 0, "failed": 0}
    # Возврат просроченных
    q = select(Task.id, Task.attempts).where(Task.status == "processing", Task.lease_expires_at.is_not(None), Task.lease_expires_at < now)
    rows = (await session.execute(q)).all()
    to_return: list[int] = []
    to_fail: list[int] = []
    for rid, attempts in rows:
        if attempts >= MAX_ATTEMPTS:
            to_fail.append(rid)
        else:
            to_return.append(rid)
    if to_return:
        await session.execute(
            update(Task)
            .where(Task.id.in_(to_return))
            .values(status="pending", lease_expires_at=None, heartbeat_at=None)
        )
        stats["returned"] = len(to_return)
    if to_fail:
        await session.execute(
            update(Task)
            .where(Task.id.in_(to_fail))
            .values(status="failed", finished_at=utcnow(), last_error="max attempts exceeded")
        )
        stats["failed"] = len(to_fail)
    return stats
