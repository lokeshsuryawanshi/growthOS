"""Sessions and the one access rule.

There are no roles. A signed-in operator may act on a client profile (tenant) if and only if they own it. The session
token says who you claim to be; every request re-reads the account and the clients it owns from the database
(app.api.deps), so a token can never grant more than the database does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import get_settings


class AuthError(Exception):
    pass


class ForbiddenError(Exception):
    pass


class NoTenantAccess(ForbiddenError):
    """Not your client. Callers answer 404 so client ids cannot be probed."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    email: str = ""
    tenant_ids: frozenset[str] = field(default_factory=frozenset)   # the clients this account owns

    def owns(self, tenant_id: str) -> bool:
        return tenant_id in self.tenant_ids


def authorize(principal: Principal, tenant_id: str) -> None:
    """Service-layer check (defence in depth behind the route check): the client must belong to this account."""
    if not principal.owns(tenant_id):
        raise NoTenantAccess("no access to this client")


def issue_token(principal: Principal) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": principal.user_id, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)},
                      s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> str:
    """The user id the token was issued to. Raises AuthError on a bad signature, expiry or shape."""
    try:
        p = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    if not isinstance(p.get("sub"), str) or not p["sub"]:
        raise AuthError("bad subject")
    return p["sub"]
