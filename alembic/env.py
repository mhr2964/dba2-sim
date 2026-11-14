"""Alembic environment.

Runs migrations over an async engine using the `asyncpg` driver directly
(via SQLAlchemy's asyncio bridge) rather than adding `psycopg2-binary` as a
second Postgres driver dependency — asyncpg is already a runtime dependency
(pyproject.toml), and alembic pulls SQLAlchemy + greenlet in transitively,
so this needs no dependency beyond what Decision 5's dep list already
declares.

No SQLAlchemy ORM models: `target_metadata` stays `None`. Migrations are raw
SQL in `op.execute` (§7's decision) — there is nothing to autogenerate
against, and introducing models solely for autogeneration would create a
second declaration of the schema alongside the migrations themselves.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from alembic import context
from dba2.core.config import get_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _async_database_url() -> str:
    db_url = get_config().database_url
    if db_url.startswith("postgresql+asyncpg://"):
        return db_url
    if db_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql://") :]
    if db_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + db_url[len("postgres://") :]
    return db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_async_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _async_database_url()
    connectable: AsyncEngine = async_engine_from_config(
        configuration,
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
