"""Server-rendered UI (Jinja + HTMX). Same RBAC/tenant checks and services as the JSON API."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.adapters.data.clients import CLIENTS
from app.agents.blackboard import latest
from app.agents.orchestrator.engine import get_orchestrator
from app.api.deps import COOKIE, check, principal_from_request, tenant_or_404, visible_tenants
from app.api.routes import login_principal
from app.core.security import ForbiddenError, Principal, issue_token
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Lead, OutreachMessage, Post, RunNode, Tenant
from app.db.session import get_db
from app.guardrails.state import IllegalTransition
from app.services import approvals, queries
from app.services.kpis import tenant_kpis

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
FOUNDER_TITLES = {c["slug"]: c.get("founder_title", "Founder") for c in CLIENTS}


async def render(request: Request, name: str, me: Principal | None, **ctx) -> HTMLResponse:
    my_tenants = await visible_tenants(me) if me else []
    return templates.TemplateResponse(request, name, {"me": me, "my_tenants": my_tenants, **ctx})


def need_login(request: Request) -> Principal:
    me = principal_from_request(request)
    if me is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return me


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return await render(request, "login.html", None)


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    p = await login_principal(email, password)
    if p is None:
        return await render(request, "login.html", None, error="Wrong email or password.", email=email)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, issue_token(p), httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/")
async def home(request: Request):
    me = need_login(request)
    if me.is_agency:
        return RedirectResponse("/ops", status_code=303)
    tenants = await visible_tenants(me)
    return RedirectResponse(f"/portal/{tenants[0].id}" if tenants else "/logout", status_code=303)


# ---------------------------------------------------------------- portal --
@router.get("/portal/{tid}", response_class=HTMLResponse)
async def portal(request: Request, tid: str, tab: str = "inbox"):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "content:view")
    ctx: dict = {"tenant": tenant, "tab": tab, "founder": _founder(tenant)}
    pending = await queries.posts(tid, ["pending_approval"])
    dms = await queries.outreach(tid, ["pending_approval", "approved"])
    ctx["inbox_count"] = len(pending) + len(dms)
    if tab == "inbox":
        ctx.update(pending_posts=pending, dms=dms)
    elif tab == "calendar":
        weeks = await queries.calendar(tid)
        ctx["weeks"] = [{"week": w["week"], "by_day": {s.publish_date.weekday(): s for s in w["slots"]}} for w in weeks]
    elif tab == "leads":
        ctx["leads"] = await queries.leads(tid)
    else:
        ctx["k"] = await tenant_kpis(tid)
    return await render(request, "portal.html", me, **ctx)


def _founder(t: Tenant) -> dict:
    return {"name": t.founder_name, "title": f"{FOUNDER_TITLES.get(t.slug, 'Founder')} at {t.name}"}


async def _post_card(request, me, tenant, post_id, msg="", bad=False):
    with tenant_scope(tenant.id):
        async with get_db().reader() as s:
            post = (await s.execute(select(Post).where(Post.id == post_id))).scalars().one()
    slot = next((r["slot"] for r in await queries.posts(tenant.id) if r["post"].id == post_id), None)
    return templates.TemplateResponse(request, "partials/post_card.html",
                                      {"post": post, "slot": slot, "tenant": tenant, "me": me,
                                       "founder": _founder(tenant), "msg": msg, "msg_bad": bad})


@router.post("/ui/{tid}/posts/{pid}", response_class=HTMLResponse)
async def ui_post_decision(request: Request, tid: str, pid: str, action: str = Form(...),
                           text: str | None = Form(None), note: str = Form("")):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "content:view")
    msg, bad = {"approve": "Approved and scheduled.", "reject": "Rejected. The slot stays open.",
                "request_changes": "Got it. A new draft is being written.", "edit": "Edit saved."}.get(action, ""), False
    try:
        await approvals.decide_post(me, tid, pid, action, text, note)
    except ForbiddenError as e:
        msg, bad = f"Not allowed: {e}", True
    except IllegalTransition as e:
        msg, bad = str(e), True
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    return await _post_card(request, me, tenant, pid, msg, bad)


async def _dm_card(request, me, tenant, mid, msg="", bad=False):
    with tenant_scope(tenant.id):
        async with get_db().reader() as s:
            m = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == mid))).scalars().one()
            lead = (await s.execute(select(Lead).where(Lead.id == m.lead_id))).scalars().one()
    return templates.TemplateResponse(request, "partials/dm_card.html",
                                      {"m": m, "lead": lead, "tenant": tenant, "me": me, "msg": msg, "msg_bad": bad})


@router.post("/ui/{tid}/outreach/{mid}", response_class=HTMLResponse)
async def ui_dm_decision(request: Request, tid: str, mid: str, action: str = Form(...), text: str | None = Form(None)):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "lead:view")
    msg, bad = {"approve": "Approved. Send it when you're ready.", "reject": "Removed from your list.",
                "edit": "Edit saved."}.get(action, ""), False
    try:
        await approvals.decide_outreach(me, tid, mid, action, text)
    except (ForbiddenError, IllegalTransition) as e:
        msg, bad = str(e), True
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    return await _dm_card(request, me, tenant, mid, msg, bad)


@router.post("/ui/{tid}/outreach/{mid}/send", response_class=HTMLResponse)
async def ui_dm_send(request: Request, tid: str, mid: str):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "lead:view")
    msg, bad = "Sent.", False
    try:
        await approvals.send_outreach(me, tid, mid)
    except ForbiddenError as e:
        msg, bad = f"Not sent: {e}", True
    return await _dm_card(request, me, tenant, mid, msg, bad)


# ------------------------------------------------------------------- ops --
@router.get("/ops", response_class=HTMLResponse)
async def ops_index(request: Request):
    me = need_login(request)
    if not me.is_agency:
        raise HTTPException(404)
    rows = await queries.portfolio([t.id for t in await visible_tenants(me)])
    return await render(request, "ops_index.html", me, rows=rows, section="ops")


@router.get("/ops/{tid}", response_class=HTMLResponse)
async def ops_client(request: Request, tid: str, tab: str = "brief"):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "run:view")
    ctx: dict = {"tenant": tenant, "tab": tab, "k": await tenant_kpis(tid), "founder": _founder(tenant), "section": "ops"}
    if tab == "brief":
        ctx["arts"] = await queries.artifacts(tid)
        with tenant_scope(tid):
            async with get_db().reader() as s:
                ctx["waiting_gate"] = (await s.execute(select(RunNode.run_id).where(RunNode.status == "waiting_human"))).scalars().first()
    elif tab == "runs":
        ctx["runs"] = await queries.runs(tid)
    elif tab == "content":
        rows = await queries.posts(tid)
        ctx["all_posts"] = sorted(rows, key=lambda r: (r["post"].status != "blocked", r["post"].status != "pending_approval"))
    elif tab == "leads":
        ctx["leads"], ctx["rejections"] = await queries.leads(tid), await queries.rejections(tid)
    elif tab == "costs":
        ctx["costs"] = await queries.cost_breakdown(tid)
    elif tab == "guardrails":
        ctx["incidents"] = await queries.incidents(tid)
    elif tab == "audit":
        ctx["audit"] = await queries.audit_log(tid)
    return await render(request, "ops_client.html", me, **ctx)


@router.get("/ops/{tid}/runs/{rid}", response_class=HTMLResponse)
async def ops_run(request: Request, tid: str, rid: str):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "run:view")
    d = await queries.run_detail(tid, rid)
    if d is None:
        raise HTTPException(404)
    return await render(request, "ops_run.html", me, tenant=tenant, d=d, section="ops")


@router.post("/ui/{tid}/runs")
async def ui_start_run(request: Request, tid: str, kind: str = Form(...)):
    me = need_login(request)
    tenant = await tenant_or_404(me, tid, "run:start")
    if tenant.kill_switch:
        return RedirectResponse(f"/ops/{tid}", status_code=303)
    if kind == "weekly" and not await latest(tid, "icp"):
        kind = "discovery"  # weekly work needs a strategy first
    orch = get_orchestrator()
    rid = await orch.create_run(tid, kind, {}, actor=me.user_id)
    orch.start(tid, rid)
    return RedirectResponse(f"/ops/{tid}/runs/{rid}", status_code=303)


@router.post("/ui/ops/weekly")
async def ui_weekly_all(request: Request):
    me = need_login(request)
    check(me, "run:start")
    orch = get_orchestrator()
    for t in await visible_tenants(me):
        if not t.kill_switch and t.status == "active" and await latest(t.id, "icp"):
            orch.start(t.id, await orch.create_run(t.id, "weekly", {}, actor=me.user_id))
    return RedirectResponse("/ops", status_code=303)


@router.post("/ui/{tid}/runs/{rid}/approve/{key}")
async def ui_gate(request: Request, tid: str, rid: str, key: str):
    me = need_login(request)
    await tenant_or_404(me, tid, "strategy:review")
    try:
        await get_orchestrator().approve_gate(tid, rid, key, me.user_id)
    except ValueError:
        pass
    return RedirectResponse(f"/ops/{tid}/runs/{rid}", status_code=303)


@router.post("/ui/{tid}/runs/{rid}/rerun/{key}")
async def ui_rerun(request: Request, tid: str, rid: str, key: str):
    me = need_login(request)
    await tenant_or_404(me, tid, "run:start")
    await get_orchestrator().rerun_from(tid, rid, key, me.user_id)
    return RedirectResponse(f"/ops/{tid}/runs/{rid}", status_code=303)


@router.post("/ui/{tid}/kill")
async def ui_kill(request: Request, tid: str, on: str = Form(...)):
    me = need_login(request)
    await tenant_or_404(me, tid, "tenant:kill_switch")
    async with get_db().session() as s:
        (await s.get(Tenant, tid)).kill_switch = on == "1"
    with tenant_scope(tid):
        async with get_db().session() as s:
            audit(s, me.user_id, "tenant.kill_switch", "tenant", tid, on=on == "1")
    return RedirectResponse(f"/ops/{tid}", status_code=303)
