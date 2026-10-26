"""JSON API. Every route: authenticate -> RBAC permission -> tenant access (404 if none) -> service."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from fastapi import Request

from app.agents.orchestrator.engine import DAGS, get_orchestrator
from app.api.deps import check, current_principal, tenant_or_404, visible_tenants
from app.core.ratelimit import client_ip, limiter
from app.core.security import ForbiddenError, Principal, issue_token, verify_password
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Membership, Tenant, User
from app.db.session import get_db
from app.guardrails.state import IllegalTransition
from app.integrations.status import integration_status
from app.services import accounts, approvals, queries, runs as run_service
from app.services.kpis import tenant_kpis

router = APIRouter(prefix="/api")


class LoginIn(BaseModel):
    email: str
    password: str


async def login_principal(email: str, password: str) -> Principal | None:
    async with get_db().reader() as s:
        user = (await s.execute(select(User).where(User.email == email.strip().lower()))).scalars().first()
        if user is None or not user.active or not verify_password(password, user.password_hash):
            return None
        tids = (await s.execute(select(Membership.tenant_id).where(Membership.user_id == user.id))).scalars().all()
    return Principal(user.id, user.name, user.role, frozenset(tids))


def throttle_login(request: Request, email: str) -> None:
    limiter.check(f"login-ip:{client_ip(request)}", 30, 300)
    limiter.check(f"login-email:{email.strip().lower()}", 8, 600)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request) -> dict[str, Any]:
    throttle_login(request, body.email)
    p = await login_principal(body.email, body.password)
    if p is None:
        raise HTTPException(401, "wrong email or password")
    return {"token": issue_token(p), "role": p.role, "tenants": sorted(p.tenant_ids)}


class SignupIn(BaseModel):
    name: str
    email: str
    password: str
    invite_code: str = ""


@router.post("/auth/signup", status_code=201)
async def signup(body: SignupIn, request: Request) -> dict[str, Any]:
    limiter.check(f"signup:{client_ip(request)}", 5, 3600)
    try:
        p = await accounts.signup(body.name, body.email, body.password, body.invite_code)
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    return {"token": issue_token(p), "role": p.role, "tenants": []}


@router.post("/tenants", status_code=201)
async def create_workspace(body: dict[str, Any], p: Principal = Depends(current_principal)):
    """Onboarding: create the caller's workspace from the same form fields the UI uses."""
    try:
        t = await accounts.create_workspace(p, accounts.OnboardingForm.from_form(body))
    except (accounts.AccountError, ValueError) as e:
        raise HTTPException(422, str(e))
    p2 = Principal(p.user_id, p.name, p.role, frozenset(await accounts.my_tenant_ids(p.user_id)))
    return {"id": t.id, "slug": t.slug, "token": issue_token(p2)}


@router.get("/tenants/{tid}/profile")
async def get_profile(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "strategy:view")
    return await accounts.profile_as_form_values(tid)


@router.put("/tenants/{tid}/profile")
async def put_profile(tid: str, body: dict[str, Any], p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "onboarding:edit")
    try:
        await accounts.update_profile(p, tid, accounts.OnboardingForm.from_form(body))
    except (accounts.AccountError, ValueError) as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@router.get("/integrations")
async def integrations(tid: str | None = None, p: Principal = Depends(current_principal)):
    if tid:
        await tenant_or_404(p, tid, "integration:view")
    return await integration_status(tid)


@router.get("/tenants")
async def tenants(p: Principal = Depends(current_principal)):
    return [{"id": t.id, "slug": t.slug, "name": t.name, "status": t.status} for t in await visible_tenants(p)]


@router.get("/tenants/{tid}/kpis")
async def kpis(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "kpi:view")
    return await tenant_kpis(tid)


@router.get("/tenants/{tid}/artifacts/{kind}")
async def artifact(tid: str, kind: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "strategy:view")
    a = (await queries.artifacts(tid)).get(kind)
    if a is None:
        raise HTTPException(404, "not found")
    return {"kind": a.kind, "version": a.version, "data": a.data, "created_at": a.created_at}


@router.get("/tenants/{tid}/calendar")
async def calendar(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "content:view")
    return [{"week": w["week"], "slots": [{"id": s.id, "date": s.publish_date, "pillar": s.pillar, "format": s.format,
                                            "topic": s.topic, "status": s.status, "post_id": s.post_id} for s in w["slots"]]}
            for w in await queries.calendar(tid)]


def _post_out(p) -> dict:
    return {"id": p.id, "status": p.status, "text": p.text, "pillar": p.pillar, "critic_score": p.critic_score,
            "rounds": p.rounds, "sources": p.sources, "guardrail_report": p.guardrail_report,
            "edit_distance": p.edit_distance, "slot_id": p.slot_id}


@router.get("/tenants/{tid}/posts")
async def posts(tid: str, status: str | None = None, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "content:view")
    rows = await queries.posts(tid, [status] if status else None)
    if not p.is_agency:  # founders never see blocked drafts; those are the agency's problem
        rows = [r for r in rows if r["post"].status != "blocked"]
    return [dict(_post_out(r["post"]), date=r["slot"].publish_date if r["slot"] else None) for r in rows]


@router.post("/tenants/{tid}/posts/{pid}/posted")
async def post_marked_posted(tid: str, pid: str, p: Principal = Depends(current_principal)):
    """The founder confirms an approved post was published on LinkedIn (nothing publishes automatically)."""
    await tenant_or_404(p, tid, "content:view")
    return _post_out(await _domain_errors(approvals.mark_posted)(p, tid, pid))


class DecisionIn(BaseModel):
    action: Literal["approve", "reject", "request_changes", "edit"]
    text: str | None = None
    note: str = ""
    subject: str | None = None


def _domain_errors(fn):
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except ForbiddenError as e:
            raise HTTPException(403, str(e))
        except approvals.NotFound:
            raise HTTPException(404, "not found")
        except IllegalTransition as e:
            raise HTTPException(409, str(e))
        except approvals.GuardrailRefusal as e:
            raise HTTPException(422, f"blocked by guardrails: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))
    return wrapper


@router.post("/tenants/{tid}/posts/{pid}/decision")
async def post_decision(tid: str, pid: str, body: DecisionIn, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "content:view")
    post = await _domain_errors(approvals.decide_post)(p, tid, pid, body.action, body.text, body.note)
    return _post_out(post)


@router.get("/tenants/{tid}/leads")
async def leads(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "lead:view")
    return [{"id": r["lead"].id, "name": r["lead"].full_name, "title": r["lead"].title, "company": r["lead"].company,
             "domain": r["lead"].company_domain, "email_status": r["lead"].email_status,
             "source_url": r["lead"].source_url, "source_provider": r["lead"].source_provider,
             "synthetic": r["lead"].is_synthetic,
             "fit_score": r["lead"].fit_score, "evidence": r["lead"].evidence, "week_of": r["lead"].week_of,
             "status": r["lead"].status, "message": r["msg"].text if r["msg"] else None,
             "message_id": r["msg"].id if r["msg"] else None, "message_status": r["msg"].status if r["msg"] else None}
            for r in await queries.leads(tid)]


@router.post("/tenants/{tid}/outreach/{mid}/decision")
async def outreach_decision(tid: str, mid: str, body: DecisionIn, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "lead:view")
    m = await _domain_errors(approvals.decide_outreach)(p, tid, mid, body.action, body.text, body.note, body.subject)
    return {"id": m.id, "status": m.status}


@router.post("/tenants/{tid}/outreach/{mid}/send")
async def outreach_send(tid: str, mid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "lead:view")
    m = await _domain_errors(approvals.send_outreach)(p, tid, mid)
    return {"id": m.id, "status": m.status, "sent_at": m.sent_at}


class RunIn(BaseModel):
    kind: Literal["discovery", "content", "leads", "weekly"]
    params: dict[str, Any] = {}


@router.post("/tenants/{tid}/runs", status_code=202)
async def start_run(tid: str, body: RunIn, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "run:start")
    try:
        rid = await run_service.start_run(p, tid, body.kind, body.params if p.is_agency else {})
    except run_service.RunRefused as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "nodes": list(DAGS[body.kind])}


@router.get("/tenants/{tid}/runs")
async def list_runs(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "run:status")
    return [{"id": r.id, "kind": r.kind, "status": r.status, "cost_usd": r.cost_usd, "created_at": r.created_at}
            for r in await queries.runs(tid)]


@router.get("/tenants/{tid}/runs/{rid}")
async def get_run(tid: str, rid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "run:status")
    d = await queries.run_detail(tid, rid)
    if d is None:
        raise HTTPException(404, "not found")
    internals = p.can("run:view")           # tokens, costs and raw outputs are agency-only
    return {"id": d["run"].id, "kind": d["run"].kind, "status": d["run"].status,
            "cost_usd": d["run"].cost_usd if internals else None,
            "nodes": [{"key": n.key, "status": n.status, "attempts": n.attempts, "error": n.error,
                       "integration": (n.output or {}).get("integration"),
                       **({"output": n.output, **d["by_node"].get(n.key, {})} if internals else {})}
                      for n in d["nodes"]]}


@router.post("/tenants/{tid}/runs/{rid}/gates/{key}/approve")
async def approve_gate(tid: str, rid: str, key: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "strategy:review")
    try:
        await get_orchestrator().approve_gate(tid, rid, key, p.user_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.post("/tenants/{tid}/runs/{rid}/rerun/{key}")
async def rerun(tid: str, rid: str, key: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "run:start")
    run_service.preflight("rerun")
    await get_orchestrator().rerun_from(tid, rid, key, p.user_id)
    return {"ok": True}


class SwitchIn(BaseModel):
    on: bool


@router.post("/tenants/{tid}/kill-switch")
async def kill_switch(tid: str, body: SwitchIn, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "tenant:kill_switch")
    async with get_db().session() as s:
        (await s.get(Tenant, tid)).kill_switch = body.on
    with tenant_scope(tid):
        async with get_db().session() as s:
            audit(s, p.user_id, "tenant.kill_switch", "tenant", tid, on=body.on)
    return {"kill_switch": body.on}


class BudgetIn(BaseModel):
    monthly_budget_usd: float


@router.post("/tenants/{tid}/budget")
async def set_budget(tid: str, body: BudgetIn, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "tenant:manage")
    async with get_db().session() as s:
        (await s.get(Tenant, tid)).monthly_budget_usd = body.monthly_budget_usd
    return {"monthly_budget_usd": body.monthly_budget_usd}


@router.get("/tenants/{tid}/incidents")
async def incidents(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "guardrail:view")
    return [{"layer": i.layer, "rule": i.rule, "severity": i.severity, "detail": i.detail, "object": i.object_type,
             "at": i.created_at} for i in await queries.incidents(tid)]


@router.get("/tenants/{tid}/costs")
async def costs(tid: str, p: Principal = Depends(current_principal)):
    await tenant_or_404(p, tid, "cost:view")
    return await queries.cost_breakdown(tid)


@router.post("/weekly-cycle", status_code=202)
async def weekly_cycle(p: Principal = Depends(current_principal)):
    """Kick off the weekly run for every active client this user manages."""
    check(p, "run:start")
    if not p.is_agency:
        raise HTTPException(403, "only the agency can start every client's weekly cycle")
    started = []
    for t in await visible_tenants(p):
        if t.kill_switch or t.status != "active":
            continue
        try:
            rid = await run_service.start_run(p, t.id, "weekly")
        except run_service.RunRefused:
            continue
        started.append({"tenant": t.slug, "run_id": rid})
    return {"started": started}
