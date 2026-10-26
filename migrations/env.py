"""Alembic environment: async engine, URL from application settings, model metadata for autogenerate."""
import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("MIGRATION_DATABASE_URL") or config.attributes.get("url") or \
        __import__("app.core.config", fromlist=["get_settings"]).get_settings().database_url


def do_run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(do_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async())
