"""RBAC eval: a hand-written role x action x endpoint matrix over the JSON API and the UI, plus escalation attempts.

The expected outcomes below are written by hand from the product rules, not derived from the POLICY table, so the
eval checks the implementation against the intent. Codes per actor:
  A  allowed: the request gets past authorization (2xx, a redirect that is not to /login, or a business answer such
     as 400/409/422/424 that only an authorized caller can get)
  F  must be 403 (the role may not do this)
  N  must be 404 (not this actor's workspace; ids cannot be probed)
  D  any denial: 403 or 404
  U  unauthenticated: 401 (API) or a redirect to /login (UI)
Actors: platform admin, account manager assigned to workspace A, account manager with no clients, A's founder, A's
read-only viewer, B's founder (a stranger to A), a deactivated user and a removed member of A, and an anonymous caller.

Metrics: rbac_matrix_pass_rate (must be 1.0), cross_tenant_access_violations (any allowed outcome for an actor with no
access to the workspace; must be 0), privilege_escalation_successes (must be 0).
"""
from __future__ import annotations

import tempfile

ACTORS = ["admin", "am_a", "am_none", "owner_a", "viewer_a", "owner_b", "deactivated", "removed", "anon"]
NO_ACCESS_TO_A = {"am_none", "owner_b", "deactivated", "removed", "anon"}

FORM = {"company_name": "LedgerLoop", "website": "ledgerloop.example", "industry": "Accounting software",
        "company": "Bookkeeping and month-end close software for small finance teams.",
        "target_customers": "Controllers and CFOs at 20-200 person companies", "founder_name": "Priya Raman",
        "sample_1": "Month-end close is slow because it is a ritual, not a process. We cut the steps nobody questioned.",
        "sample_2": "Your books are a decision tool. Close them on the 3rd and you can answer hiring questions in minutes.",
        "posts_per_week": "5", "leads_per_week_min": "10", "leads_per_week_max": "20"}

#  label                                    method  path                                              body                       admin am_a am_none owner_a viewer_a owner_b deact removed anon
MATRIX = [
    ("api read: KPIs",                      "get",  "/api/tenants/{A}/kpis",                          None,                      "A A N A A N U U U"),
    ("api read: posts",                     "get",  "/api/tenants/{A}/posts",                         None,                      "A A N A A N U U U"),
    ("api read: leads",                     "get",  "/api/tenants/{A}/leads",                         None,                      "A A N A A N U U U"),
    ("api read: strategy",                  "get",  "/api/tenants/{A}/artifacts/content_strategy",    None,                      "A A N A A N U U U"),
    ("api read: run status",                "get",  "/api/tenants/{A}/runs/{RUN}",                    None,                      "A A N A A N U U U"),
    ("api read: team",                      "get",  "/api/tenants/{A}/members",                       None,                      "A A N A F N U U U"),
    ("api read: costs (agency only)",       "get",  "/api/tenants/{A}/costs",                         None,                      "A A N F F F U U U"),
    ("api read: guardrail incidents",       "get",  "/api/tenants/{A}/incidents",                     None,                      "A A N F F F U U U"),
    ("api read: platform data view",        "get",  "/api/admin/overview",                            None,                      "A F F F F F U U U"),
    ("api write: start a lead run",         "post", "/api/tenants/{A}/runs",                          {"kind": "leads"},         "A A N A F N U U U"),
    ("api write: approve a post",           "post", "/api/tenants/{A}/posts/{POST1}/decision",        {"action": "approve"},     "F F D A F N U U U"),
    ("api write: edit a post",              "post", "/api/tenants/{A}/posts/{POST2}/decision",        {"action": "edit", "text": "{TEXT2}"}, "F A D A F N U U U"),
    ("api write: approve an email",         "post", "/api/tenants/{A}/outreach/{MSG1}/decision",      {"action": "approve"},     "F F D A F N U U U"),
    ("api write: send an email",            "post", "/api/tenants/{A}/outreach/{MSG2}/send",          None,                      "F F D A F N U U U"),
    ("api write: edit the profile",         "put",  "/api/tenants/{A}/profile",                       FORM,                      "A A N A F N U U U"),
    ("api write: add a teammate",           "post", "/api/tenants/{A}/members",                       {"email": "new.{ACTOR}@ledgerloop.example", "name": "New", "password": "a long password 1", "role": "client_viewer"}, "F F F A F N U U U"),
    ("api write: change a teammate's role", "patch", "/api/tenants/{A}/members/{VIEWER}",             {"role": "client_viewer"}, "F F F A F N U U U"),
    ("api write: pause the workspace",      "post", "/api/tenants/{A}/kill-switch",                   {"on": False},             "A A N F F F U U U"),
    ("api write: set the budget",           "post", "/api/tenants/{A}/budget",                        {"monthly_budget_usd": 25}, "A F F F F F U U U"),
    ("api write: add a lead by hand",       "post", "/api/tenants/{A}/leads",                         {"full_name": "Test Lead", "email": "lead.{ACTOR}@example.com", "company": "Test Co", "confirm_unverified": True}, "F A N A F N U U U"),
    ("api write: suggest strategy changes", "post", "/api/tenants/{A}/strategy/suggestions",          {"note": "Add a pillar on pricing transparency.", "parts": ["strategy"]}, "A A N A F N U U U"),
    ("api write: create account manager",   "post", "/api/admin/account-managers",                    {"email": "am.{ACTOR}@agency.example", "name": "AM", "password": "a long password 1"}, "A F F F F F U U U"),
    ("api write: assign a client",          "post", "/api/admin/assignments",                         {"user_id": "{AM_A}", "tenant_id": "{B}", "on": True}, "A F F F F F U U U"),
    ("ui page: workspace",                  "get",  "/portal/{A}",                                    None,                      "A A N A A N U U U"),
    ("ui page: team",                       "get",  "/portal/{A}?tab=team",                           None,                      "A A N A F N U U U"),
    ("ui page: platform data view",         "get",  "/admin",                                         None,                      "A F F F F F U U U"),
    ("ui page: agency console",             "get",  "/ops",                                           None,                      "A A A N N N U U U"),
    ("ui write: approve a post",            "post", "/ui/{A}/posts/{POST3}",                          {"action": "approve"},     "F F D A F N U U U"),
    ("ui write: edit the profile",          "post", "/ui/{A}/settings",                               FORM,                      "A A N A F N U U U"),
    ("ui write: start a lead run",          "post", "/ui/{A}/runs",                                   {"kind": "leads"},         "A A N A F N U U U"),
    ("ui write: add a teammate",            "post", "/ui/{A}/team",                                   {"email": "ui.{ACTOR}@ledgerloop.example", "name": "New", "password": "a long password 1", "role": "client_viewer"}, "F F F A F N U U U"),
    ("ui write: connect Gmail",             "get",  "/oauth/google/start?tid={A}",                    None,                      "A A N A F N U U U"),
    ("ui write: pause the workspace",       "post", "/ui/{A}/kill",                                   {"on": "0"},               "A A N F F F U U U"),
    # the founder and viewer of A probing workspace B
    ("cross: A's founder reads B",          "get",  "/api/tenants/{B}/kpis",                          None,                      "A A N N N A U U U"),
    ("cross: A's founder writes B's team",  "post", "/api/tenants/{B}/members",                       {"email": "x.{ACTOR}@b.example", "name": "X", "password": "a long password 1", "role": "client_viewer"}, "F F F N F A U U U"),
]


def _fill(v, ctx: dict):
    if isinstance(v, str):
        for k, val in ctx.items():
            v = v.replace("{" + k + "}", str(val))
        return v
    if isinstance(v, dict):
        return {k: _fill(x, ctx) for k, x in v.items()}
    return v


def _verdict(status: int, location: str, is_api: bool) -> str:
    if status == 401 or (not is_api and status in (302, 303, 307) and location.endswith("/login")):
        return "U"
    if status == 403:
        return "F"
    if status == 404:
        return "N"
    return "A"


def _ok(expected: str, got: str) -> bool:
    return got == expected or (expected == "D" and got in ("F", "N"))


async def eval_rbac() -> dict:
    import httpx
    from sqlalchemy import select

    from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
    from app.core.security import issue_token, Principal
    from app.core.tenancy import tenant_scope
    from app.db.models import OutreachMessage, Post, User
    from app.db.session import Database, get_db, set_db
    from app.fixtures.clients import DEMO_PASSWORD
    from app.fixtures.seed import seed
    from app.main import app

    from app.core.ratelimit import limiter
    limiter.reset()                # login throttling is real; a fresh eval world starts with fresh counters
    previous = get_db()
    db = Database(f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='growthos-rbac-')}/rbac.db")
    set_db(db)
    orch = Orchestrator()
    set_orchestrator(orch)
    ids = await seed(db)
    A, B = ids["ledgerloop"], ids["shiftwise"]
    disc = await orch.create_run(A, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True})
    await orch.execute(A, disc)
    await orch.execute(A, await orch.create_run(A, "weekly", {}))
    with tenant_scope(A):
        async with db.reader() as s:
            posts = (await s.execute(select(Post).where(Post.status == "pending_approval"))).scalars().all()
            msgs = (await s.execute(select(OutreachMessage).where(OutreachMessage.status == "pending_approval"))).scalars().all()
    rows: list[dict] = []
    tokens: dict[str, dict] = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval") as c:
        async def login(email, password=DEMO_PASSWORD):
            r = await c.post("/api/auth/login", json={"email": email, "password": password})
            assert r.status_code == 200, f"eval setup: login {email} -> {r.status_code} {r.text[:120]}"
            return {"Authorization": f"Bearer {r.json()['token']}"}

        for actor, email in [("admin", "admin@growthos.example"), ("am_a", "am.sam@growthos.example"),
                             ("am_none", "am.riya@growthos.example"), ("owner_a", "priya@ledgerloop.example"),
                             ("viewer_a", "viewer@ledgerloop.example"), ("owner_b", "marcus@shiftwise.example")]:
            tokens[actor] = await login(email)
        owner = tokens["owner_a"]
        for actor in ("deactivated", "removed"):           # real teammates of A, then revoked by the founder
            r = await c.post(f"/api/tenants/{A}/members", headers=owner, json={
                "email": f"{actor}@ledgerloop.example", "name": actor, "password": "a long password 1", "role": "client_owner"})
            assert r.status_code == 201, f"eval setup: add {actor} -> {r.status_code} {r.text[:120]}"
            uid = r.json()["user_id"]
            tokens[actor] = await login(f"{actor}@ledgerloop.example", "a long password 1")
            if actor == "deactivated":
                await c.patch(f"/api/tenants/{A}/members/{uid}", headers=owner, json={"active": False})
            else:
                await c.delete(f"/api/tenants/{A}/members/{uid}", headers=owner)
        tokens["anon"] = {}
        async with db.reader() as s:
            viewer_id = (await s.execute(select(User.id).where(User.email == "viewer@ledgerloop.example"))).scalar_one()
            am_a_id = (await s.execute(select(User.id).where(User.email == "am.sam@growthos.example"))).scalar_one()
        ctx = {"A": A, "B": B, "RUN": disc, "VIEWER": viewer_id, "AM_A": am_a_id,
               "POST1": posts[0].id, "POST2": posts[1].id, "TEXT2": posts[1].text, "POST3": posts[2].id,
               "MSG1": msgs[0].id, "MSG2": msgs[1].id if len(msgs) > 1 else msgs[0].id}

        for label, method, path, body, expect in MATRIX:
            for actor, exp in zip(ACTORS, expect.split()):
                hdr = tokens[actor] or {}
                p = _fill(path, {**ctx, "ACTOR": actor})
                b = _fill(body, {**ctx, "ACTOR": actor}) if body is not None else None
                is_api = p.startswith("/api/")
                c.cookies.clear()
                if not is_api and hdr:                       # the UI is cookie-authenticated
                    c.cookies.set("growthos_token", hdr["Authorization"][7:])
                kw: dict = {}
                if method != "get":
                    kw = {"json": b} if is_api else {"data": b or {}}
                r = await c.request(method.upper(), p, headers=hdr if is_api else {}, follow_redirects=False, **kw)
                got = _verdict(r.status_code, r.headers.get("location", ""), is_api)
                rows.append({"check": label, "actor": actor, "expected": exp, "got": got, "status": r.status_code,
                             "ok": _ok(exp, got),
                             "cross_tenant_violation": got == "A" and (
                                 (actor in NO_ACCESS_TO_A and "{A}" in path) or
                                 (actor in ("owner_a", "viewer_a") and "{B}" in path))})
        c.cookies.clear()
        escalations = await _escalation_attempts(c, tokens, ctx, issue_token, Principal)
    set_db(previous)
    await db.dispose()
    failed = [r for r in rows if not r["ok"]]
    return {
        "rbac_checks": len(rows), "rbac_matrix_pass_rate": round(1 - len(failed) / max(1, len(rows)), 4),
        "cross_tenant_access_violations": sum(r["cross_tenant_violation"] for r in rows),
        "privilege_escalation_attempts": len(escalations),
        "privilege_escalation_successes": sum(e["succeeded"] for e in escalations),
        "rbac_failures": [f"{r['check']} as {r['actor']}: expected {r['expected']}, got {r['got']} ({r['status']})"
                          for r in failed],
        "escalations": escalations, "matrix": rows,
    }


async def _escalation_attempts(c, tokens: dict, ctx: dict, issue_token, Principal) -> list[dict]:
    """Things an attacker would try. `succeeded` means the escalation worked; every one must fail."""
    import jwt
    A, B = ctx["A"], ctx["B"]
    owner, viewer, am = tokens["owner_a"], tokens["viewer_a"], tokens["am_a"]
    out = []

    async def attempt(name: str, coro, success) -> None:
        r = await coro
        out.append({"attempt": name, "status": r.status_code, "succeeded": bool(success(r))})

    for role in ("platform_admin", "account_manager"):
        await attempt(f"founder creates a {role}", c.post(f"/api/tenants/{A}/members", headers=owner, json={
            "email": f"esc.{role}@ledgerloop.example", "name": "E", "password": "a long password 1", "role": role}),
            lambda r: r.status_code < 300)
        await attempt(f"founder promotes the viewer to {role}", c.patch(
            f"/api/tenants/{A}/members/{ctx['VIEWER']}", headers=owner, json={"role": role}), lambda r: r.status_code < 300)
    await attempt("viewer promotes themself to owner", c.patch(f"/api/tenants/{A}/members/{ctx['VIEWER']}", headers=viewer,
                                                                json={"role": "client_owner"}), lambda r: r.status_code < 300)
    await attempt("account manager creates an account manager", c.post("/api/admin/account-managers", headers=am, json={
        "email": "esc.am@agency.example", "name": "E", "password": "a long password 1"}), lambda r: r.status_code < 300)
    await attempt("account manager assigns themself a client", c.post("/api/admin/assignments", headers=am, json={
        "user_id": ctx["AM_A"], "tenant_id": B}), lambda r: r.status_code < 300)
    await attempt("founder adds a member to another workspace", c.post(f"/api/tenants/{B}/members", headers=owner, json={
        "email": "esc.x@b.example", "name": "E", "password": "a long password 1", "role": "client_owner"}),
        lambda r: r.status_code < 300)
    r = await c.get(f"/api/tenants/{A}/members", headers=owner)
    me = next(m["user_id"] for m in r.json() if m["is_me"])
    others = [m for m in r.json() if m["role"] == "client_owner" and m["active"] and not m["is_me"]]
    if not others:
        await attempt("last owner demotes themself", c.patch(f"/api/tenants/{A}/members/{me}", headers=owner,
                                                             json={"role": "client_viewer"}), lambda r: r.status_code < 300)
    await attempt("signup asks for the platform_admin role", c.post("/api/auth/signup", json={
        "name": "Eve", "email": "eve@attacker.example", "password": "a long password 1", "role": "platform_admin"}),
        lambda r: r.status_code < 300 and r.json().get("role") != "client_owner")
    forged = jwt.encode({"sub": "x", "role": "platform_admin", "tenants": [A, B], "exp": 4102444800}, "guessed-secret",
                        algorithm="HS256")
    await attempt("token forged with a guessed secret", c.get("/api/admin/overview",
                                                              headers={"Authorization": f"Bearer {forged}"}),
                  lambda r: r.status_code < 300)
    stale = issue_token(Principal("ghost-user-id", "Ghost", "platform_admin", frozenset([A, B])))
    await attempt("validly signed token for a user that does not exist", c.get(
        "/api/admin/overview", headers={"Authorization": f"Bearer {stale}"}), lambda r: r.status_code < 300)
    await attempt("deactivated user's old token", c.get(f"/api/tenants/{A}/kpis", headers=tokens["deactivated"]),
                  lambda r: r.status_code < 300)
    await attempt("removed member's old token", c.get(f"/api/tenants/{A}/kpis", headers=tokens["removed"]),
                  lambda r: r.status_code < 300)
    return out
