"""Server-rendered UI (Jinja + HTMX). Same RBAC, tenant checks and services as the JSON API."""
from __future__ import annotations

from pathlib import Path

import jwt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agents.blackboard import intake_of
from app.agents.orchestrator.engine import DAGS, get_orchestrator
from app.api.deps import COOKIE, check, principal_from_request, tenant_or_404, visible_tenants
from app.api.routes import login_principal, throttle_login
from app.core.config import get_settings
from app.core.ratelimit import client_ip, limiter
from app.core.security import ForbiddenError, Principal, issue_token
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Lead, OutreachMessage, Post, RunNode, Tenant
from app.db.session import get_db
from app.guardrails.state import IllegalTransition
from app.integrations import gmail
from app.integrations.errors import IntegrationError
from app.integrations.hunter import HunterClient
from app.integrations.status import integration_status
from app.services import accounts, approvals, queries, runs as run_service
from app.services.kpis import tenant_kpis

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

NODE_LABELS = {
    "ingest": "Reading your materials and website", "voice": "Learning your voice", "market": "Researching your market",
    "competitors": "Analysing competitors", "trends": "Finding current trends", "strategy": "Building your content strategy",
    "icp": "Defining your ideal customers", "calendar": "Planning eight weeks of posts",
    "strategy_review": "Your approval of the strategy", "reflect": "Learning from your edits",
    "trends_refresh": "Refreshing trends", "draft_week": "Writing this week's posts", "calendar_extend": "Keeping the calendar full",
    "leads": "Finding qualified leads", "outreach": "Drafting outreach emails", "kpi_snapshot": "Updating your dashboard",
}
FLASH = "gx_flash"


# ------------------------------------------------------------------ helpers
def _flash_out(resp, msg: str, kind: str = "ok"):
    s = get_settings()
    tok = jwt.encode({"m": msg[:400], "k": kind, "exp": datetime.now(timezone.utc) + timedelta(minutes=2)}, s.jwt_secret, algorithm="HS256")
    resp.set_cookie(FLASH, tok, max_age=120, httponly=True, samesite="lax", secure=s.cookie_secure)
    return resp


def _flash_in(request: Request) -> tuple[str | None, str | None]:
    tok = request.cookies.get(FLASH)
    if not tok:
        return None, None
    try:
        p = jwt.decode(tok, get_settings().jwt_secret, algorithms=["HS256"])
        return p["m"], p["k"]
    except jwt.PyJWTError:
        return None, None


async def render(request: Request, name: str, me: Principal | None, status_code: int = 200, **ctx) -> HTMLResponse:
    my_tenants = await visible_tenants(me) if me else []
    flash, kind = _flash_in(request)
    resp = templates.TemplateResponse(request, name, {"me": me, "my_tenants": my_tenants, "flash": flash, "flash_kind": kind,
                                                      "demo_mode": get_settings().app_env == "demo", **ctx},
                                      status_code=status_code)
    if flash:
        resp.delete_cookie(FLASH)
    return resp


async def need_login(request: Request) -> Principal:
    me = await principal_from_request(request)
    if me is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return me


def set_session(resp, p: Principal):
    s = get_settings()
    resp.set_cookie(COOKIE, issue_token(p), httponly=True, samesite="lax", secure=s.cookie_secure,
                    max_age=s.jwt_ttl_minutes * 60)
    return resp


async def _founder(t: Tenant) -> dict:
    intake = await intake_of(t.id)
    return {"name": t.founder_name, "title": f"{intake.get('founder_title') or 'Founder'} at {t.name}"}


async def _gmail_connected(tid: str) -> bool:
    c = await gmail.connection(tid)
    return bool(c and c.status == "connected")


# ------------------------------------------------------------------ auth
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await principal_from_request(request):
        return RedirectResponse("/", status_code=303)
    return await render(request, "auth/login.html", None)


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        throttle_login(request, email)
    except HTTPException as e:
        return await render(request, "auth/login.html", None, status_code=429, error=e.detail, email=email)
    p = await login_principal(email, password)
    if p is None:
        return await render(request, "auth/login.html", None, status_code=401, error="Wrong email or password.", email=email)
    return set_session(RedirectResponse("/", status_code=303), p)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if await principal_from_request(request):
        return RedirectResponse("/", status_code=303)
    return await render(request, "auth/signup.html", None, invite_required=bool(get_settings().signup_invite_code))


@router.post("/signup")
async def signup_submit(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""),
                        invite_code: str = Form("")):
    ctx = dict(invite_required=bool(get_settings().signup_invite_code), name=name, email=email)
    try:
        limiter.check(f"signup:{client_ip(request)}", 5, 3600)
        p = await accounts.signup(name, email, password, invite_code)
    except HTTPException as e:
        return await render(request, "auth/signup.html", None, status_code=429, error=e.detail, **ctx)
    except accounts.AccountError as e:
        return await render(request, "auth/signup.html", None, status_code=422, error=str(e), **ctx)
    return set_session(RedirectResponse("/onboarding", status_code=303), p)


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/")
async def home(request: Request):
    me = await need_login(request)
    if me.is_agency:
        return RedirectResponse("/ops", status_code=303)
    tenants = await visible_tenants(me)
    return RedirectResponse(f"/portal/{tenants[0].id}" if tenants else "/onboarding", status_code=303)


# ------------------------------------------------------------------ onboarding
def _blank() -> dict:
    v = {k: "" for k in ("company_name", "website", "industry", "company", "target_customers", "geographies", "known_competitors",
                         "founder_name", "linkedin_url", "tone", "topics_to_own", "topics_to_avoid", "banned_phrases",
                         "facts", "primary_goal")}
    v.update(founder_title="Founder", posts_per_week=5, leads_per_week_min=10, leads_per_week_max=20)
    v.update({f"sample_{i}": "" for i in range(1, 6)})
    return v


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    me = await need_login(request)
    if me.is_agency or await accounts.my_tenant_ids(me.user_id):
        return RedirectResponse("/", status_code=303)
    return await render(request, "onboarding.html", me, bare=True, v=_blank())


@router.post("/onboarding")
async def onboarding_submit(request: Request):
    me = await need_login(request)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        t = await accounts.create_workspace(me, accounts.OnboardingForm.from_form(form))
    except Exception as e:  # validation problems go back to the form with the values intact
        msg = e.errors()[0]["msg"].removeprefix("Value error, ") + f" ({'.'.join(str(x) for x in e.errors()[0]['loc'])})" \
            if hasattr(e, "errors") else str(e)
        if isinstance(e, ForbiddenError):
            raise HTTPException(403, str(e))
        return await render(request, "onboarding.html", me, status_code=422, bare=True, v={**_blank(), **form}, error=msg)
    me2 = Principal(me.user_id, me.name, me.role, frozenset(await accounts.my_tenant_ids(me.user_id)))
    resp = RedirectResponse(f"/portal/{t.id}", status_code=303)
    return _flash_out(set_session(resp, me2), "Workspace created. Start your research when you are ready.", "ok")


# ------------------------------------------------------------------ workspace
RUN_TITLES = {"discovery": "Research and strategy", "content": "Posts and calendar", "leads": "Lead search and outreach",
              "weekly": "Weekly content and leads"}
ACTIVE = ("pending", "running", "waiting_human")


async def workspace_state(tid: str, me: Principal, k: dict) -> dict:
    arts = await queries.artifacts(tid)
    runs = await queries.runs(tid, 12)

    def act(*kinds):
        return next((r for r in runs if r.status in ACTIVE and r.kind in kinds), None)

    def last(*kinds):
        return next((r for r in runs if r.kind in kinds), None)
    active = next((r for r in runs if r.status in ACTIVE), None)
    active_disc, active_content, active_leads = act("discovery"), act("content", "weekly"), act("leads", "weekly")
    last_disc, last_content, last_leads = last("discovery"), last("content", "weekly"), last("leads", "weekly")
    with tenant_scope(tid):
        async with get_db().reader() as s:
            gate_run = (await s.execute(select(RunNode.run_id).where(RunNode.status == "waiting_human"))).scalars().first()
    disc_ok = any(r.kind == "discovery" and r.status == "succeeded" for r in runs)
    can_run = me.can("run:start")
    ready = can_run and disc_ok and not gate_run and active_disc is None
    hunter, gm = HunterClient().configured, await _gmail_connected(tid)
    steps = [{"title": "Tell us about your company", "detail": "Profile, writing samples and brand rules are saved.", "state": "done"}]

    if disc_ok:
        steps.append({"title": "Research your market and learn your voice", "detail": "Market, competitors, trends, strategy and an eight-week calendar are ready.", "state": "done",
                      "cta": "View strategy", "href": f"/portal/{tid}?tab=research"})
    elif active_disc:
        steps.append({"title": "Research your market and learn your voice", "detail": "Running now. This takes about ten minutes.", "state": "active",
                      "cta": "Watch progress", "href": f"/portal/{tid}?tab=activity&run={active_disc.id}", "primary": True})
    elif last_disc and last_disc.status == "failed":
        steps.append({"title": "Research your market and learn your voice", "detail": "The last attempt did not finish. See why and retry the failed step.", "state": "bad",
                      "cta": "See what happened", "href": f"/portal/{tid}?tab=activity&run={last_disc.id}", "primary": True})
    else:
        steps.append({"title": "Research your market and learn your voice", "detail": "We read your site, research the web and study your writing. You approve the result before anything is drafted.",
                      "state": "active", "cta": "Start research" if (can_run and active is None) else None, "post": f"/ui/{tid}/runs?kind=discovery", "primary": True})
    if gate_run:
        steps.append({"title": "Approve your strategy", "detail": "Your sign-off is needed before posts and leads are generated.", "state": "active",
                      "cta": "Review and approve", "href": f"/portal/{tid}?tab=research", "primary": True})
    else:
        steps.append({"title": "Approve your strategy", "detail": "You review it; nothing is drafted until you do.", "state": "done" if disc_ok else "todo"})

    pending = k["posts_pending"]
    if pending:
        steps.append({"title": "Review your posts", "detail": f"{pending} draft{'s' if pending != 1 else ''} waiting in your voice.", "state": "active",
                      "cta": "Review drafts", "href": f"/portal/{tid}?tab=content", "primary": True})
    elif active_content:
        steps.append({"title": "Review your posts", "detail": "Drafting now.", "state": "active", "cta": "Watch progress",
                      "href": f"/portal/{tid}?tab=activity&run={active_content.id}"})
    elif k["posts_approved_total"] or (last_content and last_content.status in ("succeeded", "succeeded_with_shortfall")):
        steps.append({"title": "Review your posts", "detail": f"{k['posts_approved_total']} approved so far.", "state": "done",
                      "cta": "Open content", "href": f"/portal/{tid}?tab=content"})
    else:
        steps.append({"title": "Write this week's posts", "detail": "Writes the week's posts from your strategy and calendar. Separate from lead search.",
                      "state": "todo", "cta": "Write this week's posts" if ready and active_content is None else None,
                      "post": f"/ui/{tid}/runs?kind=content", "primary": disc_ok})
    if active_leads:
        steps.append({"title": "Find qualified leads", "detail": "Searching now.", "state": "active", "cta": "Watch progress",
                      "href": f"/portal/{tid}?tab=activity&run={active_leads.id}"})
    elif k["leads_total"]:
        steps.append({"title": "Find qualified leads", "detail": f"{k['leads_total']} lead{'s' if k['leads_total'] != 1 else ''} found, {k['outreach_pending']} email{'s' if k['outreach_pending'] != 1 else ''} to approve.",
                      "state": "done", "cta": "Open leads", "href": f"/portal/{tid}?tab=leads"})
    else:
        steps.append({"title": "Find qualified leads", "detail": "Searches the web for companies that match your ideal customer profile. Separate from your posts.",
                      "state": "todo", "cta": "Find leads" if ready and active_leads is None else None,
                      "post": f"/ui/{tid}/runs?kind=leads", "primary": disc_ok})
    steps.append({"title": "Connect Hunter.io for verified lead emails", "detail": "Optional. Without it, leads are companies with no contact person." if not hunter else "Connected.",
                  "state": "done" if hunter else "todo", "cta": None if hunter else "How to connect", "href": f"/portal/{tid}?tab=integrations"})
    steps.append({"title": "Connect Gmail to send approved emails", "detail": "Optional. You approve every email; it sends from your own mailbox." if not gm else "Connected.",
                  "state": "done" if gm else "todo", "cta": None if gm else "Connect", "href": f"/portal/{tid}?tab=integrations"})
    return {"steps": steps, "done_count": sum(1 for s in steps if s["state"] == "done"), "gate_run": gate_run,
            "can_discover": can_run and active is None,
            "can_content": ready and active_content is None, "can_leads": ready and active_leads is None,
            "can_weekly": ready and active_content is None and active_leads is None,
            "active": active, "arts": arts, "runs": runs}


@router.get("/portal/{tid}", response_class=HTMLResponse)
async def portal(request: Request, tid: str, tab: str = "overview", run: str | None = None, saved: int = 0):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "content:view")
    if tab not in ("overview", "content", "calendar", "leads", "research", "integrations", "settings", "activity"):
        tab = "overview"
    k = await tenant_kpis(tid)
    pending = await queries.posts(tid, ["pending_approval"])
    dms = await queries.outreach(tid, ["pending_approval", "approved", "sending"])
    ws = await workspace_state(tid, me, k)
    ctx: dict = {"tenant": tenant, "tab": tab, "k": k, "ws": ws, "section": "portal",
                 "nav_counts": {"posts": len(pending), "dms": len([d for d in dms if d["msg"].status == "pending_approval"])}}
    if tab == "content":
        rows = await queries.posts(tid)
        if not me.is_agency:
            rows = [r for r in rows if r["post"].status != "blocked"]
        ctx.update(founder=await _founder(tenant), pending_posts=pending,
                   ready_posts=[r for r in rows if r["post"].status in ("approved", "scheduled")],
                   past_posts=[r for r in rows if r["post"].status in ("published", "rejected")])
    elif tab == "calendar":
        weeks = await queries.calendar(tid)
        ctx["weeks"] = [{"week": w["week"], "by_day": {s.publish_date.weekday(): s for s in w["slots"]}} for w in weeks]
    elif tab == "leads":
        ctx.update(leads=await queries.leads(tid), dms=dms, gmail_connected=await _gmail_connected(tid),
                   integ={"hunter_ok": HunterClient().configured})
    elif tab == "research":
        ctx["arts"] = ws["arts"]
    elif tab == "integrations":
        ctx.update(integrations=await integration_status(tid), gmail_ready=gmail.configured())
    elif tab == "settings":
        ctx.update(v=await accounts.profile_as_form_values(tid), saved=bool(saved), steps=False)
    elif tab == "activity":
        ctx["runs"], ctx["run_titles"] = ws["runs"], RUN_TITLES
        rid = run or (ws["active"].id if ws["active"] else (ws["runs"][0].id if ws["runs"] else None))
        if rid:
            d = await queries.run_detail(tid, rid)
            if d:
                ctx.update(run=d["run"], nodes=d["nodes"], labels=NODE_LABELS, run_titles=RUN_TITLES, can_retry=me.can("run:start"),
                           usage=await queries.run_usage(tid, rid))
    return await render(request, "portal.html", me, **ctx)


@router.get("/ui/{tid}/runs/{rid}/progress", response_class=HTMLResponse)
async def run_progress(request: Request, tid: str, rid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "run:status")
    d = await queries.run_detail(tid, rid)
    if d is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partials/run_progress.html", {
        "run": d["run"], "nodes": d["nodes"], "labels": NODE_LABELS, "run_titles": RUN_TITLES, "tenant": tenant, "me": me, "can_retry": me.can("run:start"),
        "usage": await queries.run_usage(tid, rid)})


async def _post_card(request, me, tenant, post_id, msg="", bad=False):
    with tenant_scope(tenant.id):
        async with get_db().reader() as s:
            post = (await s.execute(select(Post).where(Post.id == post_id))).scalars().one()
    slot = next((r["slot"] for r in await queries.posts(tenant.id) if r["post"].id == post_id), None)
    return templates.TemplateResponse(request, "partials/post_card.html",
                                      {"post": post, "slot": slot, "tenant": tenant, "me": me,
                                       "founder": await _founder(tenant), "msg": msg, "msg_bad": bad})


@router.post("/ui/{tid}/posts/{pid}", response_class=HTMLResponse)
async def ui_post_decision(request: Request, tid: str, pid: str, action: str = Form(...),
                           text: str | None = Form(None), note: str = Form("")):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "content:view")
    msg, bad = {"approve": "Approved. Copy it to LinkedIn when you are ready to post.", "reject": "Rejected. The slot stays open.",
                "request_changes": "Got it. A new draft is being written.", "edit": "Edit saved."}.get(action, ""), False
    try:
        await approvals.decide_post(me, tid, pid, action, text, note)
    except ForbiddenError as e:
        msg, bad = f"Not allowed: {e}", True
    except IllegalTransition as e:
        msg, bad = str(e), True
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    except ValueError as e:
        msg, bad = str(e), True
    return await _post_card(request, me, tenant, pid, msg, bad)


@router.post("/ui/{tid}/posts/{pid}/posted", response_class=HTMLResponse)
async def ui_post_posted(request: Request, tid: str, pid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "content:view")
    msg, bad = "Marked as posted.", False
    try:
        await approvals.mark_posted(me, tid, pid)
    except (ForbiddenError, IllegalTransition) as e:
        msg, bad = str(e), True
    return await _post_card(request, me, tenant, pid, msg, bad)


async def _dm_card(request, me, tenant, mid, msg="", bad=False):
    with tenant_scope(tenant.id):
        async with get_db().reader() as s:
            m = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == mid))).scalars().one()
            lead = (await s.execute(select(Lead).where(Lead.id == m.lead_id))).scalars().one()
    return templates.TemplateResponse(request, "partials/dm_card.html",
                                      {"m": m, "lead": lead, "tenant": tenant, "me": me, "msg": msg, "msg_bad": bad,
                                       "gmail_connected": await _gmail_connected(tenant.id)})


@router.post("/ui/{tid}/outreach/{mid}", response_class=HTMLResponse)
async def ui_dm_decision(request: Request, tid: str, mid: str, action: str = Form(...), text: str | None = Form(None),
                         subject: str | None = Form(None)):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "lead:view")
    msg, bad = {"approve": "Approved. Send it when you're ready.", "reject": "Removed from your list.",
                "edit": "Edit saved."}.get(action, ""), False
    try:
        await approvals.decide_outreach(me, tid, mid, action, text, subject=subject)
    except (ForbiddenError, IllegalTransition) as e:
        msg, bad = str(e), True
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    return await _dm_card(request, me, tenant, mid, msg, bad)


@router.post("/ui/{tid}/outreach/{mid}/send", response_class=HTMLResponse)
async def ui_dm_send(request: Request, tid: str, mid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "lead:view")
    msg, bad = "Sent from your Gmail.", False
    try:
        await approvals.send_outreach(me, tid, mid)
    except ForbiddenError as e:
        msg, bad = f"Not sent: {e}", True
    except IntegrationError as e:
        msg, bad = f"Not sent: {e.message}. {e.recovery}".strip(), True
    return await _dm_card(request, me, tenant, mid, msg, bad)


@router.post("/ui/{tid}/settings")
async def ui_settings(request: Request, tid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "onboarding:edit")
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        await accounts.update_profile(me, tid, accounts.OnboardingForm.from_form(form))
    except Exception as e:
        msg = (e.errors()[0]["msg"].removeprefix("Value error, ") + f" ({'.'.join(str(x) for x in e.errors()[0]['loc'])})") \
            if hasattr(e, "errors") else str(e)
        k = await tenant_kpis(tid)
        return await render(request, "portal.html", me, status_code=422, tenant=tenant, tab="settings", k=k,
                            ws=await workspace_state(tid, me, k), section="portal", v={**_blank(), **form}, error=msg,
                            steps=False, nav_counts={})
    return RedirectResponse(f"/portal/{tid}?tab=settings&saved=1", status_code=303)


# ------------------------------------------------------------------ runs
@router.post("/ui/{tid}/runs")
async def ui_start_run(request: Request, tid: str, kind: str = Form(None)):
    me = await need_login(request)
    await tenant_or_404(me, tid, "run:start")
    kind = kind or request.query_params.get("kind", "discovery")
    back = f"/ops/{tid}" if me.is_agency else f"/portal/{tid}?tab=activity"
    try:
        rid = await run_service.start_run(me, tid, kind)
    except run_service.RunRefused as e:
        return _flash_out(RedirectResponse(back, status_code=303), str(e), "bad")
    except IntegrationError as e:
        return _flash_out(RedirectResponse(back, status_code=303), f"{e.message}. {e.recovery}".strip(), "bad")
    dest = f"/ops/{tid}/runs/{rid}" if me.is_agency else f"/portal/{tid}?tab=activity&run={rid}"
    return _flash_out(RedirectResponse(dest, status_code=303), "Started. You can leave this page; progress is saved.", "ok")


@router.post("/ui/{tid}/runs/{rid}/approve/{key}")
async def ui_gate(request: Request, tid: str, rid: str, key: str):
    me = await need_login(request)
    await tenant_or_404(me, tid, "strategy:review")
    msg, kind = "Strategy approved. You can now generate content and leads.", "ok"
    try:
        await get_orchestrator().approve_gate(tid, rid, key, me.user_id)
    except ValueError as e:
        msg, kind = str(e), "bad"
    dest = f"/ops/{tid}/runs/{rid}" if me.is_agency else f"/portal/{tid}?tab=overview"
    return _flash_out(RedirectResponse(dest, status_code=303), msg, kind)


@router.post("/ui/{tid}/runs/{rid}/rerun/{key}")
async def ui_rerun(request: Request, tid: str, rid: str, key: str):
    me = await need_login(request)
    await tenant_or_404(me, tid, "run:start")
    dest = f"/ops/{tid}/runs/{rid}" if me.is_agency else f"/portal/{tid}?tab=activity&run={rid}"
    try:
        run_service.preflight("rerun")
        await get_orchestrator().rerun_from(tid, rid, key, me.user_id)
    except IntegrationError as e:
        return _flash_out(RedirectResponse(dest, status_code=303), f"{e.message}. {e.recovery}".strip(), "bad")
    return _flash_out(RedirectResponse(dest, status_code=303), "Retrying from that step. Earlier steps are kept.", "ok")


# ------------------------------------------------------------------ Gmail OAuth
@router.get("/oauth/google/start")
async def gmail_start(request: Request, tid: str):
    me = await need_login(request)
    await tenant_or_404(me, tid, "integration:manage")
    try:
        return RedirectResponse(gmail.authorize_url(tid, me.user_id), status_code=303)
    except IntegrationError as e:
        return _flash_out(RedirectResponse(f"/portal/{tid}?tab=integrations", status_code=303), f"{e.message}. {e.recovery}", "bad")


@router.get("/oauth/google/callback")
@router.get("/auth/gmail/callback")     # alias: matches the redirect URI already registered in Google Cloud
async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    me = await need_login(request)
    try:
        st = gmail.read_state(state)
    except IntegrationError as e:
        raise HTTPException(400, e.message)
    tid = st["tid"]
    if st["uid"] != me.user_id:
        raise HTTPException(403, "this sign-in was started by a different user")
    await tenant_or_404(me, tid, "integration:manage")
    back = f"/portal/{tid}?tab=integrations"
    if error or not code:
        return _flash_out(RedirectResponse(back, status_code=303), "Gmail was not connected: permission was not granted.", "bad")
    try:
        email = await gmail.complete_connection(tid, me.user_id, code)
    except IntegrationError as e:
        return _flash_out(RedirectResponse(back, status_code=303), f"{e.message}. {e.recovery}".strip(), "bad")
    with tenant_scope(tid):
        async with get_db().session() as s:
            audit(s, me.user_id, "integration.gmail_connected", "tenant", tid, account=email)
    return _flash_out(RedirectResponse(back, status_code=303), f"Gmail connected as {email}.", "ok")


@router.post("/ui/{tid}/integrations/gmail/disconnect")
async def gmail_disconnect(request: Request, tid: str):
    me = await need_login(request)
    await tenant_or_404(me, tid, "integration:manage")
    await gmail.disconnect(tid)
    with tenant_scope(tid):
        async with get_db().session() as s:
            audit(s, me.user_id, "integration.gmail_disconnected", "tenant", tid)
    return _flash_out(RedirectResponse(f"/portal/{tid}?tab=integrations", status_code=303), "Gmail disconnected.", "ok")


# ------------------------------------------------------------------ ops (agency)
@router.get("/ops", response_class=HTMLResponse)
async def ops_index(request: Request):
    me = await need_login(request)
    if not me.is_agency:
        raise HTTPException(404)
    rows = await queries.portfolio([t.id for t in await visible_tenants(me)])
    return await render(request, "ops_index.html", me, rows=rows, section="ops")


@router.get("/ops/{tid}", response_class=HTMLResponse)
async def ops_client(request: Request, tid: str, tab: str = "brief"):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "run:view")
    ctx: dict = {"tenant": tenant, "tab": tab, "k": await tenant_kpis(tid), "founder": await _founder(tenant), "section": "ops"}
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
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid, "run:view")
    d = await queries.run_detail(tid, rid)
    if d is None:
        raise HTTPException(404)
    return await render(request, "ops_run.html", me, tenant=tenant, d=d, section="ops")


@router.post("/ui/ops/weekly")
async def ui_weekly_all(request: Request):
    me = await need_login(request)
    check(me, "run:start")
    if not me.is_agency:
        raise HTTPException(403)
    started = 0
    for t in await visible_tenants(me):
        if t.kill_switch or t.status != "active":
            continue
        try:
            await run_service.start_run(me, t.id, "weekly")
            started += 1
        except (run_service.RunRefused, IntegrationError):
            continue
    return _flash_out(RedirectResponse("/ops", status_code=303), f"Started weekly runs for {started} client(s).", "ok")


@router.post("/ui/{tid}/kill")
async def ui_kill(request: Request, tid: str, on: str = Form(...)):
    me = await need_login(request)
    await tenant_or_404(me, tid, "tenant:kill_switch")
    async with get_db().session() as s:
        (await s.get(Tenant, tid)).kill_switch = on == "1"
    with tenant_scope(tid):
        async with get_db().session() as s:
            audit(s, me.user_id, "tenant.kill_switch", "tenant", tid, on=on == "1")
    return RedirectResponse(f"/ops/{tid}", status_code=303)
