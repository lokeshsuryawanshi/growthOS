"""Authentication (bcrypt + JWT) and role-based access control.

RBAC is one deny-by-default policy table. Tenant access is a second,
independent check: a role grants *what* you may do, membership grants *where*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings

ROLES = ("platform_admin", "account_manager", "client_owner", "client_viewer")
AGENCY_ROLES = {"platform_admin", "account_manager"}
_ALL = set(ROLES)

# permission -> roles allowed. Anything not listed is denied.
POLICY: dict[str, set[str]] = {
    "tenant:list_all":   {"platform_admin"},
    "tenant:manage":     {"platform_admin"},          # budgets, create tenants, users
    "tenant:kill_switch": {"platform_admin", "account_manager"},
    "tenant:view":       _ALL,
    "strategy:view":     _ALL,
    "strategy:review":   {"platform_admin", "account_manager", "client_owner"},   # self-serve founders sign off their own brief
    "onboarding:edit":   {"platform_admin", "account_manager", "client_owner"},
    "run:start":         {"platform_admin", "account_manager", "client_owner"},
    "run:view":          {"platform_admin", "account_manager"},                    # traces/internals: agency only
    "run:status":        _ALL,                                                     # progress and plain-language errors
    "integration:manage": {"platform_admin", "account_manager", "client_owner"},
    "integration:view":  _ALL,
    "content:view":      _ALL,
    "content:edit":      {"account_manager", "client_owner"},
    "content:approve":   {"client_owner"},            # the founder has the final say
    "lead:view":         _ALL,
    "outreach:edit":     {"account_manager", "client_owner"},
    "outreach:approve":  {"client_owner"},
    "outreach:send":     {"client_owner"},
    "cost:view":         {"platform_admin", "account_manager"},
    "guardrail:view":    {"platform_admin", "account_manager"},
    "eval:view":         {"platform_admin", "account_manager"},
    "kpi:view":          _ALL,
}


class AuthError(Exception):
    pass


class ForbiddenError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    tenant_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_agency(self) -> bool:
        return self.role in AGENCY_ROLES

    def can(self, permission: str) -> bool:
        return self.role in POLICY.get(permission, set())

    def can_access_tenant(self, tenant_id: str) -> bool:
        return self.role == "platform_admin" or tenant_id in self.tenant_ids


def authorize(principal: Principal, permission: str, tenant_id: str | None = None) -> None:
    if not principal.can(permission):
        raise ForbiddenError(f"role {principal.role} lacks {permission}")
    if tenant_id is not None and not principal.can_access_tenant(tenant_id):
        # Callers turn this into 404 so tenant ids can't be probed.
        raise ForbiddenError("no access to tenant")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def issue_token(principal: Principal) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": principal.user_id,
        "name": principal.name,
        "role": principal.role,
        "tenants": sorted(principal.tenant_ids),
        "iat": now,
        "exp": now + timedelta(minutes=s.jwt_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> Principal:
    try:
        p = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    if p.get("role") not in ROLES:
        raise AuthError("unknown role")
    return Principal(user_id=p["sub"], name=p.get("name", ""), role=p["role"], tenant_ids=frozenset(p.get("tenants", [])))
