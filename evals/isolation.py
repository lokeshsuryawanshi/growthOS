"""Isolation eval: one account must never read or change another account's data.

  python -m evals.isolation                # the matrix, printed
  python -m evals.isolation --mutations    # break isolation on purpose; the eval must notice every break

World: two accounts. A owns a researched client with posts, leads and emails plus an empty client; B owns its own
researched client. Then:

1. Cross-account matrix. Every route of the app whose path names a client is found automatically (so a new route
   cannot be forgotten), filled in with A's real ids (client, post, email, run), and called as B. Each must answer 404
   and A's data must be byte-for-byte unchanged afterwards. The same routes called signed-out must go to /login.
2. Sessions. Forged, unsigned (alg=none), tampered, expired and stale (deleted account) cookies must all be signed out.
3. Leaks. Every page B can open is searched for A's client names, post text and lead companies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest import mock

FORM = {"action": "approve", "kind": "discovery", "text": "edited", "note": "a note long enough", "subject": "s",
        "confirm": "", "cap": "1", "parts": "strategy"}


async def _world():
    from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
    from app.fixtures.clients import CLIENTS
    from app.fixtures.seed import seed
    from tests.helpers import weekly
    orch = Orchestrator()
    set_orchestrator(orch)
    by_slug = {c["slug"]: c for c in CLIENTS}
    a = await seed(clients=[by_slug["ledgerloop"]], owner={"email": "a@iso.example", "name": "Account A"})
    b = await seed(clients=[by_slug["shiftwise"]], owner={"email": "b@iso.example", "name": "Account B"})
    for tid in (a["ledgerloop"], b["shiftwise"]):
        rid = await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True})
        await orch.execute(tid, rid)
        await weekly(orch, tid)
    return a["ledgerloop"], b["shiftwise"]


async def _ids(tid: str) -> dict:
    from sqlalchemy import select
    from app.core.tenancy import tenant_scope
    from app.db.models import OutreachMessage, Post, Run
    from app.db.session import get_db
    with tenant_scope(tid):
        async with get_db().reader() as s:
            return {"tid": tid, "pid": (await s.execute(select(Post.id))).scalars().first(),
                    "mid": (await s.execute(select(OutreachMessage.id))).scalars().first(),
                    "rid": (await s.execute(select(Run.id).where(Run.kind == "discovery"))).scalars().first()}


async def _fingerprint(tid: str) -> str:
    """A hash of every row of every tenant table for this client: any change by another account shows up."""
    from sqlalchemy import select
    from app.core.tenancy import tenant_scope
    from app.db.models import TENANT_TABLES, Base, Tenant
    from app.db.session import get_db
    h = hashlib.sha256()
    async with get_db().reader() as s:
        t = await s.get(Tenant, tid)
        h.update(repr((t.name, t.owner_user_id) if t else None).encode())
    for name in sorted(TENANT_TABLES):
        model = next(c for c in Base.__subclasses__() if getattr(c, "__tablename__", None) == name)
        with tenant_scope(tid):
            async with get_db().reader() as s:
                for row in (await s.execute(select(model).order_by(model.id))).scalars():
                    h.update(json.dumps({c.name: getattr(row, c.key) for c in model.__table__.columns}, default=str,
                                        sort_keys=True).encode())
    return h.hexdigest()


def client_routes():
    """(method, path template) for every app route that names a client: found from the app, not hand-listed."""
    from fastapi.routing import APIRoute
    from app.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            inner = getattr(r, "original_router", None) or getattr(r, "router", None)   # included routers
            if inner is not None and hasattr(inner, "routes"):
                yield from walk(inner.routes)
    out = sorted({(m, r.path) for r in walk(app.routes) if "{tid}" in r.path for m in r.methods - {"HEAD"}})
    assert out, "no client routes found: the eval would prove nothing"
    return out


def _fill(path: str, ids: dict) -> str:
    key = "strategy_review" if path.endswith("/approve/{key}") else "market"
    return path.format(tid=ids["tid"], pid=ids["pid"] or "x", mid=ids["mid"] or "x", rid=ids["rid"] or "x", key=key)


def _token(sub: str, secret: str, minutes: int = 60, alg: str = "HS256") -> str:
    import jwt
    now = datetime.now(timezone.utc)
    payload = {"sub": sub, "iat": now, "exp": now + timedelta(minutes=minutes)}
    if alg == "none":
        return jwt.encode(payload, None, algorithm="none")
    return jwt.encode(payload, secret, algorithm=alg)


async def _signed_in(email: str):
    from app.main import app
    from tests.helpers import client_for, sign_in
    c = client_for(app, raise_errors=False)
    await sign_in(c, email)
    return c


async def run_checks(a_tid: str, b_tid: str) -> dict:
    import jwt
    from app.core.config import get_settings
    from app.db.models import Tenant
    from app.db.session import get_db
    from app.main import app
    from tests.helpers import client_for
    secret = get_settings().jwt_secret
    ids_a = await _ids(a_tid)
    async with get_db().reader() as s:
        a_name, a_owner = (await s.get(Tenant, a_tid)).name, (await s.get(Tenant, a_tid)).owner_user_id
        b_owner = (await s.get(Tenant, b_tid)).owner_user_id
    before = await _fingerprint(a_tid)
    failures: list[str] = []
    checks = 0

    b = await _signed_in("b@iso.example")
    anon = client_for(app, raise_errors=False)
    try:
        # 1. every client route, as B and signed out
        for method, path in client_routes():
            url = _fill(path, ids_a)
            data = {**FORM, "confirm": a_name} if method == "POST" else None
            r = await b.request(method, url, data=data)
            checks += 1
            if r.status_code != 404:
                failures.append(f"B {method} {path} -> {r.status_code} (want 404)")
            r = await anon.request(method, url, data=data)
            checks += 1
            if not (r.status_code == 303 and r.headers.get("location") == "/login"):
                failures.append(f"signed-out {method} {path} -> {r.status_code} (want 303 to /login)")
        # account-level actions only ever touch the caller's own account
        r = await b.post("/account/delete", data={"confirm": "a@iso.example"})
        checks += 1
        async with get_db().reader() as s:
            if await s.get(Tenant, a_tid) is None:
                failures.append("B closed A's account")
        after = await _fingerprint(a_tid)
        checks += 1
        if after != before:
            failures.append("A's data changed during B's requests")

        # 2. sessions
        escalations: list[str] = []
        tampered = b.cookies["growthos_token"].split(".")
        tampered[1] = jwt.utils.base64url_encode(json.dumps({"sub": a_owner, "exp": 4102444800}).encode()).decode()
        sessions = {"forged with a guessed secret": _token(a_owner, "guessed-" * 6),
                    "unsigned (alg=none)": _token(a_owner, "", alg="none"),
                    "B's cookie with A's id swapped in": ".".join(tampered),
                    "expired": _token(a_owner, secret, minutes=-1),
                    "stale: account deleted": _token("deleted-account", secret)}
        for label, tok in sessions.items():
            c = client_for(app, raise_errors=False)
            c.cookies.set("growthos_token", tok)
            for url in (f"/portal/{a_tid}", "/clients", "/account"):
                r = await c.get(url)
                checks += 1
                if r.status_code == 200:
                    escalations.append(f"{label}: GET {url} -> 200")
            await c.aclose()

        # 3. nothing of A's appears on any page B can open
        markers = [a_name] + await _markers(a_tid)
        pages = ["/clients", "/account"] + [f"/portal/{b_tid}?tab={t}" for t in
                                            ("overview", "content", "calendar", "leads", "research", "activity", "settings")]
        leaks = []
        for url in pages:
            text = (await b.get(url)).text
            checks += 1
            for mk in markers:
                if mk and mk in text:
                    leaks.append(f"{url} shows A's '{mk[:40]}'")
    finally:
        await b.aclose()
        await anon.aclose()
    return {"isolation_checks": checks, "isolation_routes": len(client_routes()),
            "cross_account_access_violations": len(failures), "session_escalations": len(escalations),
            "cross_account_page_leaks": len(leaks), "isolation_failures": failures + escalations + leaks,
            "owners_distinct": a_owner != b_owner}


async def _markers(tid: str) -> list[str]:
    from sqlalchemy import select
    from app.core.tenancy import tenant_scope
    from app.db.models import Lead, Post
    from app.db.session import get_db
    with tenant_scope(tid):
        async with get_db().reader() as s:
            posts = (await s.execute(select(Post.text).limit(5))).scalars().all()
            companies = (await s.execute(select(Lead.company).limit(5))).scalars().all()
    return [re.sub(r"\s+", " ", p)[:60] for p in posts] + list(companies)


async def eval_isolation(mutations: bool = False) -> dict:
    from app.db.session import Database, get_db, set_db
    from app.llm.gateway import LLMGateway, set_gateway
    from app.core.ratelimit import limiter
    previous = get_db()
    db = Database(f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='growthos-iso-')}/iso.db")
    set_db(db)
    set_gateway(LLMGateway())
    limiter.reset()
    try:
        await db.create_all()
        a, b = await _world()
        report = await run_checks(a, b)
        if mutations:
            report["mutations"] = await run_mutations(a, b)
        return report
    finally:
        set_db(previous)
        await db.dispose()


# ---------------------------------------------------------------- mutation test: does the eval notice a broken wall?
def _mutations():
    import jwt
    from fastapi import HTTPException
    from app.api import deps
    from app.core.security import Principal
    from app.db.models import Tenant
    from app.db.session import get_db
    from app.web import routes

    async def no_owner_check(p, tenant_id):
        async with get_db().reader() as s:
            t = await s.get(Tenant, tenant_id)
        if t is None:
            raise HTTPException(404, "not found")
        return t

    def unverified(token):
        return jwt.decode(token, options={"verify_signature": False, "verify_exp": False})["sub"]

    original_load = deps.load_principal

    async def trusts_the_token(user_id):
        return await original_load(user_id) or Principal(user_id, "ghost")

    async def every_client(user_id):
        from sqlalchemy import select
        p = await original_load(user_id)
        if p is None:
            return None
        async with get_db().reader() as s:
            tids = (await s.execute(select(Tenant.id))).scalars().all()
        return Principal(p.user_id, p.name, p.email, frozenset(tids))

    async def owner_check_without_service_check(p, tenant_id):   # route check off; the service check is the backstop
        return await no_owner_check(p, tenant_id)
    return {
        "route ownership check removed": [mock.patch.object(routes, "tenant_or_404", no_owner_check)],
        "session signature and expiry not checked": [mock.patch.object(deps, "decode_token", unverified)],
        "deleted accounts keep their sessions": [mock.patch.object(deps, "load_principal", trusts_the_token)],
        "every account owns every client": [mock.patch.object(deps, "load_principal", every_client),
                                            mock.patch.object(routes, "tenant_or_404", owner_check_without_service_check)],
    }


async def run_mutations(a: str, b: str) -> dict:
    out = {}
    for name, patches in _mutations().items():
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            r = await run_checks(a, b)
        caught = r["cross_account_access_violations"] + r["session_escalations"] + r["cross_account_page_leaks"] > 0
        out[name] = {"caught": caught, "violations": r["cross_account_access_violations"],
                     "escalations": r["session_escalations"], "leaks": r["cross_account_page_leaks"]}
    return {"mutations_total": len(out), "mutations_caught": sum(v["caught"] for v in out.values()), "detail": out}


def main() -> None:
    import os
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("LLM_MODE", "mock")
    from app.core.config import Settings
    Settings.model_config["env_file"] = None
    mutations = "--mutations" in sys.argv
    r = asyncio.run(eval_isolation(mutations))
    print(json.dumps(r, indent=2))
    ok = r["cross_account_access_violations"] == r["session_escalations"] == r["cross_account_page_leaks"] == 0
    if mutations:
        ok = ok and r["mutations"]["mutations_caught"] == r["mutations"]["mutations_total"]
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
