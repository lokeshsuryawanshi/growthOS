"""Server-rendered UI (Jinja + HTMX). One operator account, its client profiles, and the work done for each client.

Every client route starts with tenant_or_404: the client must belong to the signed-in account, otherwise 404.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.agents.blackboard import intake_of
from app.agents.orchestrator.engine import get_orchestrator
from app.api.deps import COOKIE, my_clients, principal_from_request, tenant_or_404
from app.core import google_oidc
from app.core.config import get_settings
from app.core.ratelimit import client_ip, limiter
from app.core.security import Principal, issue_token
from app.core.tenancy import tenant_scope
from app.db.models import Lead, OutreachMessage, RunNode, Tenant
from app.db.session import get_db
from app.guardrails.state import IllegalTransition
from app.integrations.errors import IntegrationError
from app.services import accounts, approvals, compose, keys, queries, runs as run_service, strategy
from app.services import progress as progress_service
from app.services.kpis import tenant_kpis

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["compose"] = compose.for_message

NODE_LABELS = {
    "ingest": "Reading the client's materials and website", "voice": "Learning the founder's voice",
    "market": "Researching the market", "competitors": "Analysing competitors", "trends": "Finding current trends",
    "strategy": "Building the content strategy", "icp": "Defining the ideal customers",
    "calendar": "Planning eight weeks of posts", "strategy_review": "Your approval of the strategy",
    "reflect": "Learning from your edits", "trends_refresh": "Refreshing trends", "draft_week": "Writing this week's posts",
    "calendar_extend": "Keeping the calendar full", "leads": "Finding qualified leads", "outreach": "Drafting outreach emails",
    "kpi_snapshot": "Updating the dashboard",
}
RUN_TITLES = {"discovery": "Research and strategy", "content": "Posts and calendar", "leads": "Lead search and outreach"}
ACTIVE = ("pending", "running", "waiting_human")
TABS = ("overview", "content", "calendar", "leads", "research", "activity", "settings")
FLASH = "gx_flash"


# ------------------------------------------------------------------ helpers
def _flash_out(resp, msg: str, kind: str = "ok"):
    s = get_settings()
    tok = jwt.encode({"m": msg[:400], "k": kind, "exp": datetime.now(timezone.utc) + timedelta(minutes=2)}, s.jwt_secret,
                     algorithm="HS256")
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


def _back(url: str, msg: str, kind: str = "ok"):
    return _flash_out(RedirectResponse(url, status_code=303), msg, kind)


async def render(request: Request, name: str, me: Principal | None, status_code: int = 200, **ctx) -> HTMLResponse:
    flash, kind = _flash_in(request)
    s = get_settings()
    resp = templates.TemplateResponse(request, name, {"me": me, "my_clients": await my_clients(me) if me else [],
                                                      "flash": flash, "flash_kind": kind, "demo_mode": s.app_env == "demo",
                                                      "ephemeral": s.ephemeral_storage, **ctx}, status_code=status_code)
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


def _form_error(e: Exception) -> str:
    if hasattr(e, "errors"):
        err = e.errors()[0]
        return err["msg"].removeprefix("Value error, ") + f" ({'.'.join(str(x) for x in err['loc'])})"
    return str(e)


async def _founder(t: Tenant) -> dict:
    intake = await intake_of(t.id)
    return {"name": t.founder_name, "title": f"{intake.get('founder_title') or 'Founder'} at {t.name}"}


# ------------------------------------------------------------------ public pages
@router.get("/termsofservice", response_class=HTMLResponse)
async def terms_page(request: Request):
    return await render(request, "legal/terms.html", await principal_from_request(request), bare=True)


@router.get("/login/policy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return await render(request, "legal/privacy.html", await principal_from_request(request), bare=True,
                        contact_email=get_settings().contact_email)


# ------------------------------------------------------------------ sign in with Google
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if await principal_from_request(request):
        return RedirectResponse("/", status_code=303)
    return await render(request, "auth/login.html", None, error=error[:300], google_ready=google_oidc.configured(),
                        test_login=TEST_LOGIN)


@router.get("/auth/google/start")
async def google_start(request: Request):
    limiter.check(f"login:{client_ip(request)}", 30, 300)
    try:
        url, cookie = google_oidc.start()
    except google_oidc.LoginFailed as e:
        return await render(request, "auth/login.html", None, status_code=503, error=str(e), google_ready=False,
                            test_login=TEST_LOGIN)
    resp = RedirectResponse(url, status_code=303)
    s = get_settings()
    resp.set_cookie(google_oidc.FLOW_COOKIE, cookie, max_age=google_oidc.FLOW_TTL_S, httponly=True, samesite="lax",
                    secure=s.cookie_secure, path="/auth/google")
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    limiter.check(f"login:{client_ip(request)}", 30, 300)

    def fail(msg: str, status: int = 400):
        r = templates.TemplateResponse(request, "auth/login.html", {"me": None, "error": msg, "google_ready": True,
                                                                    "test_login": TEST_LOGIN}, status_code=status)
        r.delete_cookie(google_oidc.FLOW_COOKIE, path="/auth/google")
        return r
    if error:
        return fail("Sign-in was cancelled at Google." if error == "access_denied" else "Google could not sign you in.")
    try:
        ident = await google_oidc.finish(code, state, request.cookies.get(google_oidc.FLOW_COOKIE))
    except google_oidc.LoginFailed as e:
        return fail(str(e))
    p = await accounts.google_user(ident.sub, ident.email, ident.name)
    resp = set_session(RedirectResponse("/", status_code=303), p)
    resp.delete_cookie(google_oidc.FLOW_COOKIE, path="/auth/google")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# A sign-in without Google, for automated tests and the labelled local demo only. The route does not exist in
# production (tests/test_production_integrity.py boots the app with APP_ENV=production and checks).
TEST_LOGIN = get_settings().app_env in ("test", "demo")
if TEST_LOGIN:
    @router.post("/auth/test-login")
    async def test_login(email: str = Form(...), name: str = Form("Test Operator")):
        if get_settings().app_env not in ("test", "demo"):       # belt and braces: never in production
            raise HTTPException(404)
        email = email.strip().lower()
        p = await accounts.google_user("test:" + email, email, name)
        return set_session(RedirectResponse("/", status_code=303), p)


# ------------------------------------------------------------------ clients
@router.get("/")
async def home(request: Request):
    me = await need_login(request)
    return RedirectResponse(f"/portal/{next(iter(me.tenant_ids))}" if len(me.tenant_ids) == 1 else "/clients",
                            status_code=303)


@router.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request):
    me = await need_login(request)
    return await render(request, "clients.html", me, section="clients",
                        rows=await queries.clients_overview(await my_clients(me)))


def _blank() -> dict:
    v = {k: "" for k in ("company_name", "website", "industry", "company", "target_customers", "geographies",
                         "known_competitors", "founder_name", "linkedin_url", "tone", "topics_to_own", "topics_to_avoid",
                         "banned_phrases", "facts", "primary_goal")}
    v.update(founder_title="Founder", posts_per_week=5, leads_per_week_min=10, leads_per_week_max=20)
    v.update({f"sample_{i}": "" for i in range(1, 6)})
    return v


@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_page(request: Request):
    me = await need_login(request)
    return await render(request, "onboarding.html", me, section="clients", v=_blank())


@router.post("/clients/new")
async def new_client_submit(request: Request):
    me = await need_login(request)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        t = await accounts.create_client(me, accounts.OnboardingForm.from_form(form))
    except (accounts.AccountError, ValueError) as e:   # validation problems go back to the form with the values intact
        return await render(request, "onboarding.html", me, status_code=422, section="clients", v={**_blank(), **form},
                            error=_form_error(e))
    return _back(f"/portal/{t.id}", f"{t.name} added. Start the research when you are ready.")


@router.post("/ui/{tid}/delete")
async def delete_client(request: Request, tid: str, confirm: str = Form("")):
    me = await need_login(request)
    t = await tenant_or_404(me, tid)
    if confirm.strip() != t.name:
        return _back(f"/portal/{tid}?tab=settings", "Not deleted: type the client's name exactly to confirm.", "bad")
    await accounts.delete_client(me, tid)
    return _back("/clients", f"{t.name} and everything made for it were deleted.")


# ------------------------------------------------------------------ account: spend cap, close account
@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    me = await need_login(request)
    resp = await render(request, "account.html", me, section="account", spend=await queries.account_spend(me),
                        cap=await accounts.monthly_cap(me), saved_keys=await keys.list_keys(me.user_id),
                        key_events=await keys.events(me.user_id), providers=keys.PROVIDERS, labels=keys.LABELS)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/account/keys")
async def account_key_save(request: Request, kind: str = Form(""), provider: str = Form(""), key: str = Form("")):
    me = await need_login(request)
    limiter.check(f"keys:{me.user_id}", 10, 600)          # each save makes one call to the provider
    try:
        v = await keys.save_key(me, kind, provider, key)
    except keys.KeyRefused as e:
        return _back("/account", str(e), "bad")             # the message never contains the key
    return _back("/account", f"{v.label} key ending in {v.last4} checked and saved.")


@router.post("/account/keys/{kind}/delete")
async def account_key_delete(request: Request, kind: str):
    me = await need_login(request)
    limiter.check(f"keys:{me.user_id}", 10, 600)
    if kind not in keys.PROVIDERS:
        raise HTTPException(404, "not found")
    deleted = await keys.delete_key(me, kind)
    return _back("/account", "Key deleted." if deleted else "There was no key to delete.")


@router.post("/account/cap")
async def account_cap(request: Request, cap: str = Form("")):
    me = await need_login(request)
    try:
        v = await accounts.set_monthly_cap(me, cap)
    except accounts.AccountError as e:
        return _back("/account", str(e), "bad")
    return _back("/account", f"Monthly AI spend cap set to ${v:.2f}." if v is not None else "Monthly cap removed.")


@router.post("/account/delete")
async def account_delete(request: Request, confirm: str = Form("")):
    me = await need_login(request)
    if confirm.strip().lower() != me.email.lower():
        return _back("/account", "Not closed: type your email address exactly to confirm.", "bad")
    await accounts.delete_account(me)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------------ a client's workspace
async def workspace_state(tid: str, k: dict) -> dict:
    owner = await keys.owner_of(tid)
    saved = await keys.list_keys(owner) if owner else {}
    keys_ok = get_settings().uses_fixture_sources or all(saved.get(x) and saved[x].status == "valid" for x in ("llm", "search"))
    arts = await queries.artifacts(tid)
    runs = await queries.runs(tid, 12)

    def act(*kinds):
        return next((r for r in runs if r.status in ACTIVE and r.kind in kinds), None)

    def last(*kinds):
        return next((r for r in runs if r.kind in kinds), None)
    active = next((r for r in runs if r.status in ACTIVE), None)
    active_disc, active_content, active_leads = act("discovery"), act("content"), act("leads")
    last_disc, last_content = last("discovery"), last("content")
    with tenant_scope(tid):
        async with get_db().reader() as s:
            gate_run = (await s.execute(select(RunNode.run_id).where(RunNode.status == "waiting_human"))).scalars().first()
    disc_ok = any(r.kind == "discovery" and r.status in ("succeeded", "succeeded_with_shortfall") for r in runs)
    ready = disc_ok and not gate_run and active_disc is None
    steps = [{"title": "Client profile", "detail": "Profile, writing samples and brand rules are saved.", "state": "done"},
             {"title": "Your API keys", "detail": "An AI key (Groq, OpenAI or Anthropic) and a Tavily search key are saved "
              "and working." if keys_ok else "Add an AI key (Groq, OpenAI or Anthropic) and a Tavily key. Runs use your "
              "own accounts.", "state": "done" if keys_ok else "active", "cta": None if keys_ok else "Add keys",
              "href": "/account", "primary": not keys_ok}]
    research = "Research the market and learn the founder's voice"
    if disc_ok:
        steps.append({"title": research, "detail": "Market, competitors, trends, strategy and an eight-week calendar are ready.",
                      "state": "done", "cta": "View strategy", "href": f"/portal/{tid}?tab=research"})
    elif active_disc:
        steps.append({"title": research, "detail": "Running now. This usually takes a few minutes.", "state": "active",
                      "cta": "Watch progress", "href": f"/portal/{tid}?tab=activity&run={active_disc.id}", "primary": True})
    elif last_disc and last_disc.status == "failed":
        steps.append({"title": research, "detail": "The last attempt did not finish. See why and retry the failed step.",
                      "state": "bad", "cta": "See what happened", "href": f"/portal/{tid}?tab=activity&run={last_disc.id}",
                      "primary": True})
    else:
        steps.append({"title": research, "detail": "Reads the client's site, researches the web and studies the writing "
                      "samples. You approve the result before anything is drafted.", "state": "active",
                      "cta": "Start research" if active is None else None, "post": f"/ui/{tid}/runs?kind=discovery",
                      "primary": True})
    if gate_run:
        steps.append({"title": "Approve the strategy", "detail": "Your sign-off is needed before posts and leads are generated.",
                      "state": "active", "cta": "Review and approve", "href": f"/portal/{tid}?tab=research", "primary": True})
    else:
        steps.append({"title": "Approve the strategy", "detail": "You review it; nothing is drafted until you do.",
                      "state": "done" if disc_ok else "todo"})
    pending = k["posts_pending"]
    if pending:
        steps.append({"title": "Review the posts", "detail": f"{pending} draft{'s' if pending != 1 else ''} waiting.",
                      "state": "active", "cta": "Review drafts", "href": f"/portal/{tid}?tab=content", "primary": True})
    elif active_content:
        steps.append({"title": "Review the posts", "detail": "Drafting now.", "state": "active", "cta": "Watch progress",
                      "href": f"/portal/{tid}?tab=activity&run={active_content.id}"})
    elif k["posts_approved_total"] or (last_content and last_content.status in ("succeeded", "succeeded_with_shortfall")):
        steps.append({"title": "Review the posts", "detail": f"{k['posts_approved_total']} approved so far.", "state": "done",
                      "cta": "Open content", "href": f"/portal/{tid}?tab=content"})
    else:
        steps.append({"title": "Write this week's posts", "detail": "Writes the week's posts from the strategy and calendar.",
                      "state": "todo", "cta": "Write this week's posts" if ready and active_content is None else None,
                      "post": f"/ui/{tid}/runs?kind=content", "primary": disc_ok})
    if active_leads:
        steps.append({"title": "Find qualified leads", "detail": "Searching now.", "state": "active", "cta": "Watch progress",
                      "href": f"/portal/{tid}?tab=activity&run={active_leads.id}"})
    elif k["leads_total"]:
        n, e = k["leads_total"], k["outreach_pending"]
        steps.append({"title": "Find qualified leads", "detail": f"{n} lead{'s' if n != 1 else ''} found, {e} email"
                      f"{'s' if e != 1 else ''} to approve.", "state": "done", "cta": "Open leads",
                      "href": f"/portal/{tid}?tab=leads"})
    else:
        steps.append({"title": "Find qualified leads", "detail": "Searches the web for companies that match the ideal "
                      "customer profile. Separate from the posts.", "state": "todo",
                      "cta": "Find leads" if ready and active_leads is None else None,
                      "post": f"/ui/{tid}/runs?kind=leads", "primary": disc_ok})
    return {"steps": steps, "done_count": sum(1 for s in steps if s["state"] == "done"), "gate_run": gate_run, "keys_ok": keys_ok,
            "can_discover": active is None, "can_content": ready and active_content is None,
            "can_leads": ready and active_leads is None, "active": active, "arts": arts, "runs": runs}


@router.get("/portal/{tid}", response_class=HTMLResponse)
async def portal(request: Request, tid: str, tab: str = "overview", run: str | None = None, saved: int = 0):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    tab = tab if tab in TABS else "overview"
    k = await tenant_kpis(tid)
    pending = await queries.posts(tid, ["pending_approval"])
    dms = await queries.outreach(tid, ["pending_approval", "approved"])
    ws = await workspace_state(tid, k)
    ctx: dict = {"tenant": tenant, "tab": tab, "k": k, "ws": ws, "section": "portal",
                 "nav_counts": {"posts": len(pending), "dms": len([d for d in dms if d["msg"].status == "pending_approval"])}}
    if tab == "content":
        rows = [r for r in await queries.posts(tid) if r["post"].status != "blocked"]
        ctx.update(founder=await _founder(tenant), pending_posts=pending,
                   ready_posts=[r for r in rows if r["post"].status in ("approved", "scheduled")],
                   past_posts=[r for r in rows if r["post"].status in ("published", "rejected")])
    elif tab == "calendar":
        weeks = await queries.calendar(tid)
        ctx["weeks"] = [{"week": w["week"], "by_day": {s.publish_date.weekday(): s for s in w["slots"]}} for w in weeks]
        ctx["slot_posts"] = await queries.slot_posts(tid)
    elif tab == "leads":
        ctx.update(leads=await queries.leads(tid), dms=dms, rejections=await queries.rejections(tid, 100),
                   intake=await intake_of(tid))
    elif tab == "research":
        ctx.update(arts=ws["arts"], suggestions=await strategy.suggestion_history(tid), parts=strategy.PARTS)
    elif tab == "settings":
        ctx.update(v=await accounts.profile_as_form_values(tid), saved=bool(saved), steps=False)
    elif tab == "activity":
        ctx["runs"], ctx["run_titles"] = ws["runs"], RUN_TITLES
        rid = run or (ws["active"].id if ws["active"] else (ws["runs"][0].id if ws["runs"] else None))
        if rid and (d := await queries.run_detail(tid, rid)):
            ctx.update(run=d["run"], nodes=d["nodes"], labels=NODE_LABELS, run_titles=RUN_TITLES,
                       usage=await queries.run_usage(tid, rid),
                       progress=await progress_service.run_progress(tid, d["run"], d["nodes"]), targets=k["targets"])
    return await render(request, "portal.html", me, **ctx)


@router.get("/portal/{tid}/posts/{pid}", response_class=HTMLResponse)
async def post_page(request: Request, tid: str, pid: str):
    """One finished post, as it will look on LinkedIn, with everything needed to decide on it."""
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    row = await queries.post(tid, pid)
    if row is None or row["post"].status == "blocked":
        raise HTTPException(404, "not found")
    k = await tenant_kpis(tid)
    return await render(request, "portal.html", me, tenant=tenant, tab="calendar", page="post", k=k, section="portal",
                        ws=await workspace_state(tid, k), nav_counts={}, post=row["post"], slot=row["slot"],
                        founder=await _founder(tenant))


@router.get("/portal/{tid}/slots/{sid}", response_class=HTMLResponse)
async def slot_page(request: Request, tid: str, sid: str):
    """A calendar slot: its post if one is written, otherwise the plan for it."""
    me = await need_login(request)
    await tenant_or_404(me, tid)
    sl = await queries.slot(tid, sid)
    if sl is None:
        raise HTTPException(404, "not found")
    post = (await queries.slot_posts(tid)).get(sid)
    if post is not None:
        return RedirectResponse(f"/portal/{tid}/posts/{post.id}", status_code=303)
    tenant = await tenant_or_404(me, tid)
    k = await tenant_kpis(tid)
    return await render(request, "portal.html", me, tenant=tenant, tab="calendar", page="slot", k=k, section="portal",
                        ws=await workspace_state(tid, k), nav_counts={}, slot=sl)


@router.get("/ui/{tid}/runs/{rid}/progress", response_class=HTMLResponse)
async def run_progress(request: Request, tid: str, rid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    d = await queries.run_detail(tid, rid)
    if d is None:
        raise HTTPException(404, "not found")
    return templates.TemplateResponse(request, "partials/run_progress.html", {
        "run": d["run"], "nodes": d["nodes"], "labels": NODE_LABELS, "run_titles": RUN_TITLES, "tenant": tenant, "me": me,
        "usage": await queries.run_usage(tid, rid), "progress": await progress_service.run_progress(tid, d["run"], d["nodes"]),
        "targets": (await tenant_kpis(tid))["targets"]})


# ------------------------------------------------------------------ posts
async def _post_card(request, me, tenant, post_id, msg="", bad=False):
    row = await queries.post(tenant.id, post_id)
    if row is None:
        raise HTTPException(404, "not found")
    return templates.TemplateResponse(request, "partials/post_card.html",
                                      {"post": row["post"], "slot": row["slot"], "tenant": tenant, "me": me,
                                       "founder": await _founder(tenant), "msg": msg, "msg_bad": bad})


@router.post("/ui/{tid}/posts/{pid}", response_class=HTMLResponse)
async def ui_post_decision(request: Request, tid: str, pid: str, action: str = Form(...),
                           text: str | None = Form(None), note: str = Form("")):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    if action not in approvals.POST_ACTIONS:
        raise HTTPException(400, "unknown action")
    msg, bad = {"approve": "Approved. Copy it to LinkedIn when you are ready to post.", "reject": "Rejected. The slot stays open.",
                "request_changes": "Got it. A new draft is being written.", "edit": "Edit saved."}[action], False
    try:
        await approvals.decide_post(me, tid, pid, action, text, note)
    except approvals.NotFound:
        raise HTTPException(404, "not found")
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    except (IllegalTransition, ValueError) as e:
        msg, bad = str(e), True
    return await _post_card(request, me, tenant, pid, msg, bad)


@router.post("/ui/{tid}/posts/{pid}/posted", response_class=HTMLResponse)
async def ui_post_posted(request: Request, tid: str, pid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    msg, bad = "Marked as posted.", False
    try:
        await approvals.mark_posted(me, tid, pid)
    except approvals.NotFound:
        raise HTTPException(404, "not found")
    except IllegalTransition as e:
        msg, bad = str(e), True
    return await _post_card(request, me, tenant, pid, msg, bad)


# ------------------------------------------------------------------ outreach emails
async def _dm_card(request, me, tenant, mid, msg="", bad=False):
    with tenant_scope(tenant.id):
        async with get_db().reader() as s:
            m = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == mid))).scalars().first()
            lead = (await s.execute(select(Lead).where(Lead.id == m.lead_id))).scalars().first() if m else None
    if m is None or lead is None:
        raise HTTPException(404, "not found")
    return templates.TemplateResponse(request, "partials/dm_card.html",
                                      {"m": m, "lead": lead, "tenant": tenant, "me": me, "msg": msg, "msg_bad": bad,
                                       "intake": await intake_of(tenant.id)})


@router.post("/ui/{tid}/outreach/{mid}", response_class=HTMLResponse)
async def ui_dm_decision(request: Request, tid: str, mid: str, action: str = Form(...), text: str | None = Form(None),
                         subject: str | None = Form(None)):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    if action not in approvals.OUTREACH_ACTIONS:
        raise HTTPException(400, "unknown action")
    msg, bad = {"approve": "Approved. Open it in Gmail or copy it, send it yourself, then mark it as sent.",
                "reject": "Removed from your list.", "edit": "Edit saved.",
                "request_changes": "Marked for changes."}[action], False
    try:
        await approvals.decide_outreach(me, tid, mid, action, text, subject=subject)
    except approvals.NotFound:
        raise HTTPException(404, "not found")
    except approvals.GuardrailRefusal as e:
        msg, bad = f"Not saved. The edit broke a guardrail: {e}", True
    except (IllegalTransition, ValueError) as e:
        msg, bad = str(e), True
    return await _dm_card(request, me, tenant, mid, msg, bad)


@router.post("/ui/{tid}/outreach/{mid}/sent", response_class=HTMLResponse)
async def ui_dm_sent(request: Request, tid: str, mid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    msg, bad = "Marked as sent.", False
    try:
        await approvals.mark_sent(me, tid, mid)
    except approvals.NotFound:
        raise HTTPException(404, "not found")
    except (approvals.SendRefused, IllegalTransition) as e:
        msg, bad = str(e), True
    return await _dm_card(request, me, tenant, mid, msg, bad)


# ------------------------------------------------------------------ profile
@router.post("/ui/{tid}/settings")
async def ui_settings(request: Request, tid: str):
    me = await need_login(request)
    tenant = await tenant_or_404(me, tid)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        await accounts.update_profile(me, tid, accounts.OnboardingForm.from_form(form))
    except (accounts.AccountError, ValueError) as e:
        k = await tenant_kpis(tid)
        return await render(request, "portal.html", me, status_code=422, tenant=tenant, tab="settings", k=k,
                            ws=await workspace_state(tid, k), section="portal", v={**_blank(), **form},
                            error=_form_error(e), steps=False, nav_counts={})
    return RedirectResponse(f"/portal/{tid}?tab=settings&saved=1", status_code=303)


# ------------------------------------------------------------------ runs
@router.post("/ui/{tid}/runs")
async def ui_start_run(request: Request, tid: str, kind: str = Form(None)):
    me = await need_login(request)
    await tenant_or_404(me, tid)
    kind = kind or request.query_params.get("kind", "discovery")
    back = f"/portal/{tid}?tab=activity"
    try:
        rid = await run_service.start_run(me, tid, kind)
    except run_service.RunRefused as e:
        return _back(back, str(e), "bad")
    except IntegrationError as e:
        return _back(back, f"{e.message}. {e.recovery}".strip(), "bad")
    return _back(f"/portal/{tid}?tab=activity&run={rid}", "Started. You can leave this page; progress is saved.")


@router.post("/ui/{tid}/runs/{rid}/approve/{key}")
async def ui_gate(request: Request, tid: str, rid: str, key: str):
    me = await need_login(request)
    await tenant_or_404(me, tid)
    msg, kind = "Strategy approved. You can now generate posts and leads.", "ok"
    try:
        await get_orchestrator().approve_gate(tid, rid, key, me.user_id)
    except LookupError:
        raise HTTPException(404, "not found")
    except ValueError as e:
        msg, kind = str(e), "bad"
    return _back(f"/portal/{tid}?tab=overview", msg, kind)


@router.post("/ui/{tid}/runs/{rid}/rerun/{key}")
async def ui_rerun(request: Request, tid: str, rid: str, key: str):
    me = await need_login(request)
    await tenant_or_404(me, tid)
    dest = f"/portal/{tid}?tab=activity&run={rid}"
    try:
        await run_service.preflight("rerun", tid)
        await get_orchestrator().rerun_from(tid, rid, key, me.user_id)
    except LookupError:
        raise HTTPException(404, "not found")
    except IntegrationError as e:
        return _back(dest, f"{e.message}. {e.recovery}".strip(), "bad")
    return _back(dest, "Retrying from that step. Earlier steps are kept.")


@router.post("/ui/{tid}/strategy/suggestions")
async def ui_suggest_changes(request: Request, tid: str):
    me = await need_login(request)
    await tenant_or_404(me, tid)
    form = await request.form()
    back = f"/portal/{tid}?tab=research"
    try:
        rid = await strategy.suggest_changes(me, tid, str(form.get("note", "")), [str(x) for x in form.getlist("parts")])
    except (strategy.SuggestionRefused, accounts.AccountError) as e:
        return _back(back, str(e), "bad")
    except IntegrationError as e:
        return _back(back, f"{e.message}. {e.recovery}".strip(), "bad")
    return _back(f"/portal/{tid}?tab=activity&run={rid}",
                 "Revising the strategy with your changes. You approve the new version when it is ready.")
