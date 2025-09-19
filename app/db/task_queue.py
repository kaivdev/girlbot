from __future__ import annotations

"""Примитивная очередь задач на базе таблицы tasks.

Функции:
  enqueue_task(kind, payload, priority=100, dedup_key=None)
  lease_tasks(kinds, limit, lease_seconds)
  heartbeat(task_id, lease_seconds)
  complete(task_id, status, error=None)

Ограничения/допущения (v1):
  - Watchdog/возврат зависших задач реализуется отдельно.
  - Нет backoff стратегии кроме инкремента attempts.
  - Поведение идемпотентности через dedup_key (уникальное поле).
"""

from datetime import timedelta
from typing import Any, Iterable, Sequence
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task
from app.utils.time import utcnow


async def enqueue_task(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    dedup_key: str | None = None,
) -> Task:
    t = Task(
        kind=kind,
        payload_json=payload or {},
        priority=priority,
        dedup_key=dedup_key,
    )
    session.add(t)
    return t


async def lease_tasks(
    session: AsyncSession,
    *,
    kinds: Sequence[str] | None = None,
    limit: int = 10,
    lease_seconds: int = 60,
) -> list[Task]:
    """Атомарно переводит pending задачи в processing и возвращает их.

    Использует SELECT FOR UPDATE SKIP LOCKED чтобы поддерживать несколько воркеров.
    """
    now = utcnow()
    lease_expires = now + timedelta(seconds=lease_seconds)
    q = select(Task).where(Task.status == "pending").order_by(Task.priority.asc(), Task.created_at.asc()).limit(limit).with_for_update(skip_locked=True)
    if kinds:
        q = q.where(Task.kind.in_(kinds))
    rows = (await session.execute(q)).scalars().all()
    leased: list[Task] = []
    for task in rows:
        task.status = "processing"
        task.started_at = task.started_at or now
        task.lease_expires_at = lease_expires
        task.heartbeat_at = now
        task.attempts += 1
        leased.append(task)
    return leased


async def heartbeat(session: AsyncSession, task_id: int, *, lease_seconds: int = 60) -> None:
    now = utcnow()
    lease_expires = now + timedelta(seconds=lease_seconds)
    await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "processing")
        .values(heartbeat_at=now, lease_expires_at=lease_expires)
    )


async def complete(
    session: AsyncSession,
    task_id: int,
    *,
    status: str = "done",
    error: str | None = None,
) -> None:
    if status not in {"done", "failed", "cancelled"}:
        raise ValueError("invalid completion status")
    values = {
        "status": status,
        "finished_at": utcnow(),
        "last_error": error,
    }
    await session.execute(update(Task).where(Task.id == task_id).values(**values))


async def return_to_pending(session: AsyncSession, task_ids: Iterable[int]) -> None:
    ids = list(task_ids)
    if not ids:
        return
    await session.execute(
        update(Task)
        .where(Task.id.in_(ids), Task.status == "processing")
        .values(status="pending", lease_expires_at=None, heartbeat_at=None)
    )
