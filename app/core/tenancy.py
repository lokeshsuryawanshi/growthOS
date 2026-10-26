"""Request/task-scoped tenant context.

The active scope lives in a ContextVar, so it follows each asyncio task
(DAG fan-out copies it at task creation) and is read by the ORM interceptor
in app.db.session. Tenant ids are always set by server code, never by an LLM.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


class TenantScopeError(PermissionError):
    """Raised when tenant data is touched without a valid tenant scope."""


@dataclass(frozen=True)
class TenantScope:
    tenant_id: str | None
    system: bool = False   # cross-tenant access (platform jobs, admin). Always audited by caller.
    reason: str = ""


_scope: ContextVar[TenantScope | None] = ContextVar("tenant_scope", default=None)


def current_scope() -> TenantScope | None:
    return _scope.get()


def require_tenant_id() -> str:
    scope = _scope.get()
    if scope is None or scope.tenant_id is None:
        raise TenantScopeError("no tenant scope is active")
    return scope.tenant_id


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[TenantScope]:
    if not tenant_id:
        raise TenantScopeError("tenant_id is required")
    token = _scope.set(TenantScope(tenant_id=tenant_id))
    try:
        yield _scope.get()  # type: ignore[misc]
    finally:
        _scope.reset(token)


@contextmanager
def system_scope(reason: str) -> Iterator[TenantScope]:
    """Explicit cross-tenant scope. Use only for platform jobs and admin views."""
    if not reason:
        raise TenantScopeError("system scope needs a reason")
    token = _scope.set(TenantScope(tenant_id=None, system=True, reason=reason))
    try:
        yield _scope.get()  # type: ignore[misc]
    finally:
        _scope.reset(token)
