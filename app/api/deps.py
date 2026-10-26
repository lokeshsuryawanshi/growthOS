from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from app.core.security import AuthError, ForbiddenError, Principal, authorize, decode_token
from app.core.tenancy import system_scope
from app.db.models import Tenant
from app.db.session import get_db

COOKIE = "growthos_token"


def principal_from_request(request: Request) -> Principal | None:
    token = request.cookies.get(COOKIE)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    if not token:
        return None
    try:
        return decode_token(token)
    except AuthError:
        return None


def current_principal(request: Request) -> Principal:
    p = principal_from_request(request)
    if p is None:
        raise HTTPException(401, "not authenticated")
    return p


def check(p: Principal, permission: str, tenant_id: str | None = None) -> None:
    """Permission failures are 403; tenant-access failures are 404 so tenant ids can't be probed."""
    if not p.can(permission):
        raise HTTPException(403, f"your role can't {permission}")
    if tenant_id is not None:
        try:
            authorize(p, permission, tenant_id)
        except ForbiddenError:
            raise HTTPException(404, "not found")


async def visible_tenants(p: Principal) -> list[Tenant]:
    async with get_db().reader() as s:
        rows = (await s.execute(select(Tenant).order_by(Tenant.name))).scalars().all()
    return [t for t in rows if p.can_access_tenant(t.id)]


async def tenant_or_404(p: Principal, tenant_id: str, permission: str = "tenant:view") -> Tenant:
    check(p, permission, tenant_id)
    async with get_db().reader() as s:
        t = await s.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(404, "not found")
    return t


def admin_scope(reason: str):
    return system_scope(reason)


Principal_ = Depends(current_principal)
