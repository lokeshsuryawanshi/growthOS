from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from app.core.security import AuthError, ForbiddenError, Principal, authorize, decode_token
from app.core.tenancy import system_scope
from app.db.models import Membership, Tenant, User
from app.db.session import get_db

COOKIE = "growthos_token"


def token_principal(request: Request) -> Principal | None:
    """What the signed token claims. Never trusted on its own: see principal_from_request."""
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


async def refresh_principal(claimed: Principal) -> Principal | None:
    """Re-read the user, role and workspace memberships from the database.

    Tokens are stateless and last 12 hours, so they can outlive the user (database reset, deactivation, removal) or carry a
    stale role. The database is the source of truth: a missing or inactive user is logged out, and a changed role or removed
    membership takes effect on the very next request."""
    async with get_db().reader() as s:
        user = (await s.execute(select(User).where(User.id == claimed.user_id))).scalars().first()
        if user is None or not user.active:
            return None
        tids = (await s.execute(select(Membership.tenant_id).where(Membership.user_id == user.id))).scalars().all()
    return Principal(user.id, user.name, user.role, frozenset(tids))


async def principal_from_request(request: Request) -> Principal | None:
    claimed = token_principal(request)
    return await refresh_principal(claimed) if claimed else None


async def current_principal(request: Request) -> Principal:
    p = await principal_from_request(request)
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
