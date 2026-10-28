from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.core.security import AuthError, Principal, decode_token
from app.db.models import Tenant, User
from app.db.session import get_db

COOKIE = "growthos_token"


def token_user_id(request: Request) -> str | None:
    """What the signed session cookie claims. Never trusted on its own: see principal_from_request."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return decode_token(token)
    except AuthError:
        return None


async def load_principal(user_id: str) -> Principal | None:
    """Re-read the account and the clients it owns. The database is the source of truth: a token for an account that
    no longer exists (database reset, account deleted) is treated as signed out, never as a foreign-key crash."""
    async with get_db().reader() as s:
        user = await s.get(User, user_id)
        if user is None:
            return None
        tids = (await s.execute(select(Tenant.id).where(Tenant.owner_user_id == user.id))).scalars().all()
    return Principal(user.id, user.name, user.email, frozenset(tids))


async def principal_from_request(request: Request) -> Principal | None:
    uid = token_user_id(request)
    return await load_principal(uid) if uid else None


async def my_clients(p: Principal) -> list[Tenant]:
    async with get_db().reader() as s:
        return list((await s.execute(select(Tenant).where(Tenant.owner_user_id == p.user_id)
                                     .order_by(Tenant.name))).scalars().all())


async def tenant_or_404(p: Principal, tenant_id: str) -> Tenant:
    """The one access rule: the client must exist and belong to this account. Anything else is a plain 404."""
    async with get_db().reader() as s:
        t = await s.get(Tenant, tenant_id)
    if t is None or t.owner_user_id != p.user_id:
        raise HTTPException(404, "not found")
    return t
