"""Workspace teams: a founder adds teammates (viewer or co-owner) to their own workspace and manages them.

Rules, all enforced here for the API and the UI alike:
* only a workspace owner manages its team (team:manage), and only inside that workspace (404 elsewhere);
* roles come from the GRANTABLE table: a founder can grant client_viewer or client_owner, never an agency role;
* agency staff attached to a workspace (account managers) are managed by the platform admin, not the founder;
* the last active owner can never be removed, deactivated or demoted;
* an email that already has an account is refused (no attaching someone else's account to your workspace);
* every change is audited, and takes effect on the member's next request (sessions are re-checked against the database).
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.core.security import (CLIENT_ROLES, MIN_PASSWORD, ForbiddenError, Principal, authorize, can_grant,
                               hash_password)
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Membership, User
from app.db.session import get_db
from app.services.accounts import EMAIL_RE, AccountError


class TeamConflict(ValueError):
    """The change is not allowed in the current state (409), e.g. it would leave the workspace without an owner."""


class MemberNotFound(LookupError):
    """No such member in this workspace (404)."""


ROLE_LABELS = {"client_owner": "Owner", "client_viewer": "Viewer (read-only)", "account_manager": "Account manager",
               "platform_admin": "Platform admin"}


async def list_members(p: Principal, tenant_id: str) -> list[dict]:
    authorize(p, "team:view", tenant_id)
    async with get_db().reader() as s:
        rows = (await s.execute(select(User, Membership).join(Membership, Membership.user_id == User.id)
                                .where(Membership.tenant_id == tenant_id).order_by(User.created_at))).all()
        added_by = {u.id: u.email for u in (await s.execute(select(User).where(
            User.id.in_([m.added_by for _, m in rows if m.added_by])))).scalars()}
    return [{"user_id": u.id, "email": u.email, "name": u.name, "role": u.role, "role_label": ROLE_LABELS.get(u.role, u.role),
             "active": u.active, "agency": u.role not in CLIENT_ROLES, "is_me": u.id == p.user_id,
             "added_by": added_by.get(m.added_by, ""), "added_at": m.created_at} for u, m in rows]


def _check_new_user(email: str, name: str, password: str) -> tuple[str, str]:
    email, name = email.strip().lower(), name.strip()
    if not EMAIL_RE.match(email):
        raise AccountError("Please enter a valid email address.")
    if not 1 <= len(name) <= 100:
        raise AccountError("Please enter the person's name.")
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    return email, name


async def add_member(p: Principal, tenant_id: str, email: str, name: str, password: str, role: str) -> dict:
    authorize(p, "team:manage", tenant_id)
    if role not in CLIENT_ROLES or not can_grant(p.role, role):
        raise ForbiddenError(f"you can't give someone the role {role}")
    email, name = _check_new_user(email, name, password)
    async with get_db().session() as s:
        if (await s.execute(select(User.id).where(User.email == email))).first():
            raise TeamConflict("An account with that email already exists. Use a different email.")
        user = User(email=email, name=name, role=role, password_hash=hash_password(password))
        s.add(user)
        await s.flush()
        s.add(Membership(user_id=user.id, tenant_id=tenant_id, added_by=p.user_id))
        uid = user.id
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            audit(s, p.user_id, "team.member_added", "user", uid, email=email, role=role)
    return {"user_id": uid, "email": email, "name": name, "role": role, "active": True}


async def _member(s, tenant_id: str, user_id: str) -> User:
    row = (await s.execute(select(User).join(Membership, Membership.user_id == User.id)
                           .where(Membership.tenant_id == tenant_id, User.id == user_id))).scalars().first()
    if row is None:
        raise MemberNotFound("not found")
    if row.role not in CLIENT_ROLES:
        raise ForbiddenError("agency staff on this workspace are managed by the platform admin")
    return row


async def _other_active_owners(s, tenant_id: str, user_id: str) -> int:
    return (await s.execute(select(func.count(User.id)).join(Membership, Membership.user_id == User.id).where(
        Membership.tenant_id == tenant_id, User.role == "client_owner", User.active.is_(True), User.id != user_id))).scalar_one()


async def update_member(p: Principal, tenant_id: str, user_id: str, role: str | None = None, active: bool | None = None,
                        password: str | None = None) -> dict:
    authorize(p, "team:manage", tenant_id)
    if role is not None and (role not in CLIENT_ROLES or not can_grant(p.role, role)):
        raise ForbiddenError(f"you can't give someone the role {role}")
    if password is not None and len(password) < MIN_PASSWORD:
        raise AccountError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    changes: dict = {}
    async with get_db().session() as s:
        user = await _member(s, tenant_id, user_id)
        losing_owner = user.role == "client_owner" and user.active and (
            (role is not None and role != "client_owner") or active is False)
        if losing_owner and await _other_active_owners(s, tenant_id, user.id) == 0:
            raise TeamConflict("This is the workspace's last owner. Make someone else an owner first.")
        if role is not None and role != user.role:
            changes["role"] = [user.role, role]
            user.role = role
        if active is not None and active != user.active:
            changes["active"] = active
            user.active = active
        if password is not None:
            user.password_hash = hash_password(password)
            changes["password"] = "reset"
        out = {"user_id": user.id, "email": user.email, "role": user.role, "active": user.active}
    if changes:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                audit(s, p.user_id, "team.member_updated", "user", user_id, **changes)
    return out


async def remove_member(p: Principal, tenant_id: str, user_id: str) -> None:
    """Remove a teammate. Their account existed only for this workspace, so it is also deactivated."""
    authorize(p, "team:manage", tenant_id)
    async with get_db().session() as s:
        user = await _member(s, tenant_id, user_id)
        if user.role == "client_owner" and user.active and await _other_active_owners(s, tenant_id, user.id) == 0:
            raise TeamConflict("This is the workspace's last owner and can't be removed.")
        m = (await s.execute(select(Membership).where(Membership.tenant_id == tenant_id,
                                                      Membership.user_id == user_id))).scalars().one()
        await s.delete(m)
        remaining = (await s.execute(select(func.count(Membership.id)).where(Membership.user_id == user_id,
                                                                            Membership.id != m.id))).scalar_one()
        if not remaining:
            user.active = False
        email = user.email
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            audit(s, p.user_id, "team.member_removed", "user", user_id, email=email)
