"""Platform admin: a read-only data view across the platform, plus account managers and their client assignments.

The data view selects explicit columns only, so password hashes, OAuth tokens and API keys can never reach it.
Cross-tenant reads run in an explicit system scope with a reason, as everywhere else.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.security import ForbiddenError, Principal, authorize, can_grant, hash_password
from app.core.tenancy import system_scope, tenant_scope
from app.db.audit import audit
from app.db.models import AuditLog, LLMCall, Membership, PlatformAudit, Run, Tenant, User
from app.db.session import get_db
from app.services.team import MemberNotFound, TeamConflict, _check_new_user


def _match(q: str, *values: str) -> bool:
    return not q or any(q in (v or "").lower() for v in values)


async def overview(p: Principal, q: str = "") -> dict:
    authorize(p, "admin:view")
    q = q.strip().lower()
    month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with get_db().reader() as s:
        users = (await s.execute(select(User.id, User.email, User.name, User.role, User.active, User.created_at)
                                 .order_by(User.created_at))).all()
        tenants = (await s.execute(select(Tenant.id, Tenant.name, Tenant.slug, Tenant.status, Tenant.kill_switch,
                                          Tenant.monthly_budget_usd, Tenant.created_at).order_by(Tenant.created_at))).all()
        members = (await s.execute(select(Membership.user_id, Membership.tenant_id, Membership.added_by,
                                          Membership.created_at))).all()
    with system_scope("admin data view: runs, spend and audit across workspaces"):
        async with get_db().reader() as s:
            spend = dict((await s.execute(select(LLMCall.tenant_id, func.sum(LLMCall.cost_usd))
                                          .where(LLMCall.created_at >= month).group_by(LLMCall.tenant_id))).all())
            runs = (await s.execute(select(Run.id, Run.tenant_id, Run.kind, Run.status, Run.cost_usd, Run.created_at,
                                           Run.error).order_by(Run.created_at.desc()).limit(40))).all()
            audits = (await s.execute(select(AuditLog.tenant_id, AuditLog.actor, AuditLog.action, AuditLog.object_type,
                                             AuditLog.object_id, AuditLog.detail, AuditLog.created_at)
                                      .order_by(AuditLog.created_at.desc()).limit(60))).all()
    async with get_db().reader() as s:
        platform = (await s.execute(select(PlatformAudit.actor, PlatformAudit.action, PlatformAudit.target_user,
                                           PlatformAudit.tenant_id, PlatformAudit.detail, PlatformAudit.created_at)
                                    .order_by(PlatformAudit.created_at.desc()).limit(40))).all()
    tname = {t.id: t.name for t in tenants}
    email = {u.id: u.email for u in users}
    ws_of: dict[str, list[str]] = {}
    for m in members:
        ws_of.setdefault(m.user_id, []).append(tname.get(m.tenant_id, m.tenant_id))
    users_out = [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active,
                  "created_at": u.created_at, "workspaces": ws_of.get(u.id, [])}
                 for u in users if _match(q, u.email, u.name, u.role, *ws_of.get(u.id, []))]
    count = {t.id: 0 for t in tenants}
    for m in members:
        count[m.tenant_id] = count.get(m.tenant_id, 0) + 1
    ws_out = [{"id": t.id, "name": t.name, "slug": t.slug, "status": "paused" if t.kill_switch else t.status,
               "members": count.get(t.id, 0), "spend_month": round(float(spend.get(t.id) or 0), 4),
               "budget": t.monthly_budget_usd, "created_at": t.created_at}
              for t in tenants if _match(q, t.name, t.slug)]
    mem_out = [{"user": email.get(m.user_id, m.user_id), "workspace": tname.get(m.tenant_id, m.tenant_id),
                "added_by": email.get(m.added_by, "") if m.added_by else "signup", "since": m.created_at}
               for m in members if _match(q, email.get(m.user_id, ""), tname.get(m.tenant_id, ""))]
    run_out = [{"id": r.id, "workspace": tname.get(r.tenant_id, r.tenant_id), "kind": r.kind, "status": r.status,
                "cost": r.cost_usd, "created_at": r.created_at, "error": (r.error or "")[:160]}
               for r in runs if _match(q, tname.get(r.tenant_id, ""), r.kind, r.status)]
    audit_out = sorted(
        [{"at": a.created_at, "workspace": tname.get(a.tenant_id, a.tenant_id), "actor": email.get(a.actor, a.actor),
          "action": a.action, "object": f"{a.object_type} {a.object_id}".strip(), "detail": a.detail} for a in audits] +
        [{"at": a.created_at, "workspace": tname.get(a.tenant_id, "") if a.tenant_id else "platform",
          "actor": email.get(a.actor, a.actor), "action": a.action, "object": email.get(a.target_user, a.target_user),
          "detail": a.detail} for a in platform],
        key=lambda x: x["at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    audit_out = [a for a in audit_out if _match(q, a["workspace"], a["actor"], a["action"], a["object"])][:60]
    return {"q": q, "users": users_out, "workspaces": ws_out, "memberships": mem_out, "runs": run_out, "audit": audit_out,
            "spend_month_total": round(sum(w["spend_month"] for w in ws_out), 4),
            "account_managers": [u for u in users_out if u["role"] == "account_manager"],
            "all_workspaces": [{"id": t.id, "name": t.name} for t in tenants]}


async def _platform_audit(actor: str, action: str, target_user: str = "", tenant_id: str = "", **detail) -> None:
    async with get_db().session() as s:
        s.add(PlatformAudit(actor=actor, action=action, target_user=target_user, tenant_id=tenant_id, detail=detail))


async def create_account_manager(p: Principal, email: str, name: str, password: str) -> dict:
    authorize(p, "agency:manage")
    if not can_grant(p.role, "account_manager"):
        raise ForbiddenError("you can't create account managers")
    email, name = _check_new_user(email, name, password)
    async with get_db().session() as s:
        if (await s.execute(select(User.id).where(User.email == email))).first():
            raise TeamConflict("An account with that email already exists.")
        user = User(email=email, name=name, role="account_manager", password_hash=hash_password(password))
        s.add(user)
        await s.flush()
        uid = user.id
    await _platform_audit(p.user_id, "admin.account_manager_created", uid, email=email)
    return {"user_id": uid, "email": email, "role": "account_manager"}


async def _account_manager(s, user_id: str) -> User:
    user = (await s.execute(select(User).where(User.id == user_id))).scalars().first()
    if user is None:
        raise MemberNotFound("not found")
    if user.role != "account_manager":
        raise ForbiddenError("only account managers are assigned to clients here")
    return user


async def assign(p: Principal, user_id: str, tenant_id: str, on: bool = True) -> None:
    authorize(p, "agency:manage")
    async with get_db().session() as s:
        await _account_manager(s, user_id)
        if await s.get(Tenant, tenant_id) is None:
            raise MemberNotFound("not found")
        m = (await s.execute(select(Membership).where(Membership.user_id == user_id,
                                                      Membership.tenant_id == tenant_id))).scalars().first()
        if on and m is None:
            s.add(Membership(user_id=user_id, tenant_id=tenant_id, added_by=p.user_id))
        elif not on and m is not None:
            await s.delete(m)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            audit(s, p.user_id, "admin.assigned" if on else "admin.unassigned", "user", user_id)
    await _platform_audit(p.user_id, "admin.assigned" if on else "admin.unassigned", user_id, tenant_id)


async def set_active(p: Principal, user_id: str, active: bool) -> None:
    authorize(p, "agency:manage")
    async with get_db().session() as s:
        user = await _account_manager(s, user_id)
        user.active = active
    await _platform_audit(p.user_id, "admin.user_activated" if active else "admin.user_deactivated", user_id)
