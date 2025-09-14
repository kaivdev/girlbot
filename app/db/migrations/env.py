from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
import sys
import asyncio
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from app.config.settings import get_settings
from app.db.models import Base

# Alembic Config object
config = context.config

# Workaround for Windows + asyncpg + Proactor loop issues
if sys.platform.startswith("win"):
    try:  # pragma: no cover - platform specific
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# Interpret the config file for Python logging.
if config.config_file_name is not None:  # pragma: no cover - alembic boilerplate
    fileConfig(config.config_file_name)

# Set SQLAlchemy url from settings, if present
settings = get_settings()
config.set_main_option("sqlalchemy.url", str(settings.db_dsn))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
