"""Async engine, sessions and the tenant-isolation interceptors.

Isolation layers enforced here (layer 1 is the repository/service filter,
layer 3 is Postgres RLS in migrations/postgres_rls.sql):

* do_orm_execute: every ORM SELECT/UPDATE/DELETE that touches a TenantScoped
  table must run under a tenant scope; the tenant filter is added
  automatically via with_loader_criteria. No scope -> TenantScopeError.
* before_flush: every INSERT/UPDATE/DELETE of a TenantScoped row must match
  the active scope; tenant_id is filled in for new rows and may never change.

Known bypasses (by design, documented): raw text() SQL and Core Table
statements are not ORM-executed. Application code doesn't use them on tenant
tables; RLS covers them in production.

SQLite concurrency: WAL + synchronous=NORMAL + busy_timeout on every
connection. Writes use BEGIN IMMEDIATE so a transaction takes the write lock
up front (waiting up to busy_timeout) instead of failing with
SQLITE_BUSY when a read transaction later tries to upgrade.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from itertools import chain
from typing import AsyncIterator

from sqlalchemy import Table, event, inspect
from sqlalchemy.sql import visitors
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.config import get_settings, normalize_database_url
from app.core.tenancy import TenantScopeError, current_scope
from app.db.models import Base, TenantScoped

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA foreign_keys=ON",
)


def _make_engine(url: str, immediate: bool) -> AsyncEngine:
    engine = create_async_engine(url, pool_size=10, max_overflow=20) if not url.startswith("sqlite") \
        else create_async_engine(url, connect_args={"timeout": 30})
    if url.startswith("sqlite"):
        sync_engine = engine.sync_engine

        @event.listens_for(sync_engine, "connect")
        def _on_connect(dbapi_conn, _record):
            # Take over transaction control from the driver so BEGIN is ours.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            for pragma in SQLITE_PRAGMAS:
                cur.execute(pragma)
            cur.close()

        @event.listens_for(sync_engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


class ProtectedDatabaseError(RuntimeError):
    """A test, eval or demo process tried to open the production database."""


def _host(url: str) -> str:
    from sqlalchemy.engine import make_url
    try:
        return (make_url(normalize_database_url(url)).host or "").lower()
    except Exception:
        return ""


def protected_hosts() -> set[str]:
    """Hosts that only production may open: DATABASE_URL in the project's .env file (where the live URL lives) and,
    for tests, DATABASE_URL in the process environment. Never the values themselves, only the host names."""
    from pathlib import Path
    from dotenv import dotenv_values
    import os
    urls = [dotenv_values(Path(__file__).resolve().parents[2] / ".env").get("DATABASE_URL") or ""]
    if get_settings().app_env == "test":
        urls.append(os.environ.get("DATABASE_URL", ""))
    return {h for h in map(_host, urls) if h}


def guard_not_production(url: str) -> None:
    """Rule: tests, evals and the demo never touch the live database. Production itself is not restricted."""
    if get_settings().app_env == "production":
        return
    host = _host(url)
    if host and host in protected_hosts():
        raise ProtectedDatabaseError(
            f"refusing to open {host!r} with APP_ENV={get_settings().app_env}: it is the production database "
            "(DATABASE_URL). Use TEST_DATABASE_URL on a separate database or branch.")


class Database:
    """Holds a write engine (BEGIN IMMEDIATE) and a read engine (deferred BEGIN)."""

    def __init__(self, url: str):
        guard_not_production(url)
        self.url = url
        self.write_engine = _make_engine(url, immediate=True)
        self.read_engine = _make_engine(url, immediate=False) if url.startswith("sqlite") else self.write_engine
        self._write = async_sessionmaker(self.write_engine, expire_on_commit=False)
        self._read = async_sessionmaker(self.read_engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.write_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.write_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Short write transaction. Never hold one open across an LLM call."""
        async with self._write() as s:
            async with s.begin():
                yield s

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[AsyncSession]:
        async with self._read() as s:
            yield s

    async def dispose(self) -> None:
        await self.write_engine.dispose()
        if self.read_engine is not self.write_engine:
            await self.read_engine.dispose()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_url)
    return _db


def set_db(db: Database) -> None:
    """Swap the global database (tests, stress test)."""
    global _db
    _db = db


# ---------------------------------------------------------------------------
# Tenant isolation interceptors (registered once, for every Session)
# ---------------------------------------------------------------------------
_TENANT_TABLE_NAMES = {m.__tablename__ for m in Base.__subclasses__() if issubclass(m, TenantScoped)}


def _hidden_tenant_tables(statement) -> set[str]:
    """Tenant tables reachable only as raw FROMs, e.g. select(func.count()).select_from(Lead).

    with_loader_criteria cannot filter those, so they are refused rather than run unscoped.
    """
    return {
        el.name for el in visitors.iterate(statement)
        if isinstance(el, Table) and el.name in _TENANT_TABLE_NAMES
    }


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_scope(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    has_tenant_mapper = any(issubclass(m.class_, TenantScoped) for m in state.all_mappers)
    hidden = set() if has_tenant_mapper else _hidden_tenant_tables(state.statement)
    if not has_tenant_mapper and not hidden:
        return
    scope = current_scope()
    if scope is None:
        raise TenantScopeError("query on tenant data without a tenant scope")
    if scope.system:
        return
    if hidden:
        raise TenantScopeError(
            f"unfilterable query shape on {sorted(hidden)}; select an entity column, "
            "e.g. select(func.count(Model.id))"
        )
    tid = scope.tenant_id
    state.statement = state.statement.options(
        with_loader_criteria(TenantScoped, lambda cls: cls.tenant_id == tid, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _enforce_tenant_writes(session: Session, _ctx, _instances) -> None:
    scope = current_scope()
    for obj in chain(session.new, session.dirty, session.deleted):
        if not isinstance(obj, TenantScoped):
            continue
        if scope is None:
            raise TenantScopeError(f"write to {type(obj).__name__} without a tenant scope")
        if obj in session.new and obj.tenant_id is None and not scope.system:
            obj.tenant_id = scope.tenant_id
        if obj.tenant_id is None:
            raise TenantScopeError(f"{type(obj).__name__} has no tenant_id")
        hist = inspect(obj).attrs.tenant_id.history
        if obj not in session.new and hist.has_changes():
            raise TenantScopeError("tenant_id is immutable")
        if not scope.system and obj.tenant_id != scope.tenant_id:
            raise TenantScopeError(f"cross-tenant write to {type(obj).__name__} blocked")
