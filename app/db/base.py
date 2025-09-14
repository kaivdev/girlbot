from __future__ import annotations

"""Database engine and async session factory."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_settings


settings = get_settings()

engine: AsyncEngine = create_async_engine(
    str(settings.db_dsn),
    future=True,
    pool_pre_ping=True,
)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations."""

    async with AsyncSessionFactory() as session:  # pragma: no cover - context manager
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

