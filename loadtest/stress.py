"""Multi-tenant stress & performance test (mock LLM with injected latency and failures).

Runs discovery + one weekly cycle for N tenants concurrently through the real
orchestrator, scheduler, gateway, guardrails and SQLite (WAL). Then checks
business targets per tenant and cross-tenant integrity, and load-tests the
approval API.

Usage:
  python -m loadtest.stress --tenants 12
  python -m loadtest.stress --tenants 100 --latency 100 600 --rate-limit 0.05 --transient 0.02
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path

os.environ["APP_ENV"] = "test"
os.environ["LLM_MODE"] = "mock"
import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.fixtures.clients import CLIENTS, DEMO_PASSWORD  # noqa: E402
from app.agents.orchestrator.engine import Orchestrator, set_orchestrator  # noqa: E402
from app.agents.orchestrator.scheduler import FairScheduler  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.tenancy import system_scope  # noqa: E402
from app.db.models import (CalendarSlot, Lead, LLMCall, Membership, OutreachMessage, Post, Run, RunNode,  # noqa: E402
                           Tenant, User)
from app.db.session import Database, set_db  # noqa: E402
from app.llm.gateway import LLMGateway, set_gateway  # noqa: E402
from app.llm.providers import MockConfig  # noqa: E402
from app.fixtures.seed import seed  # noqa: E402
from app.services.kpis import tenant_kpis  # noqa: E402

PREFIXES = ["Nova", "Apex", "Orbit", "Vega", "Zenith", "Polar", "Quanta", "Helix", "Arbor"]


def clone_clients(n: int) -> list[dict]:
    """N tenants from the 12 templates, renamed so the leakage guard stays meaningful."""
    out = []
    for i in range(n):
        base = CLIENTS[i % len(CLIENTS)]
        if i < len(CLIENTS):
            out.append(base)
            continue
        generation = i // len(CLIENTS) - 1
        prefix = PREFIXES[generation % len(PREFIXES)] + (str(generation // len(PREFIXES)) if generation >= len(PREFIXES) else "")
        new_name = prefix + base["name"].replace(" ", "").replace("&", "And")
        last = base["founder_name"].split()[-1]
        blob = json.dumps(base).replace(base["name"], new_name).replace(base["founder_name"], f"{prefix} {last}")
        c = json.loads(blob)
        c["slug"] = f"{prefix.lower()}-{base['slug']}"
        out.append(c)
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def run(args) -> dict:
    s = get_settings()
    tmp = tempfile.mkdtemp(prefix="growthos-stress-")
    db = Database(f"sqlite+aiosqlite:///{tmp}/stress.db")
    set_db(db)
    mock = MockConfig(latency_ms=(args.latency[0], args.latency[1]), rate_limit_rate=args.rate_limit,
                      transient_rate=args.transient, seed=7)
    gw = LLMGateway(settings=s.model_copy(update={"provider_max_concurrency": args.provider_concurrency}), mock_config=mock)
    set_gateway(gw)
    sched = FairScheduler(args.global_concurrency, s.per_tenant_max_concurrency)
    orch = Orchestrator(sched)
    set_orchestrator(orch)

    clients = clone_clients(args.tenants)
    ids = await seed(db, clients=clients)

    t0 = time.monotonic()
    disc = await asyncio.gather(*[orch.execute(tid, await orch.create_run(
        tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}, budget_usd=5)) for tid in ids.values()])
    t_disc = time.monotonic() - t0
    t1 = time.monotonic()
    weekly = await asyncio.gather(*[orch.execute(tid, await orch.create_run(tid, "weekly", {}, budget_usd=5))
                                    for tid in ids.values()])
    t_week = time.monotonic() - t1
    wall = time.monotonic() - t0

    # ---- collect metrics (platform-wide read, explicitly cross-tenant) ----
    with system_scope("stress test metrics"):
        async with db.reader() as sess:
            nodes = (await sess.execute(select(RunNode))).scalars().all()
            calls = (await sess.execute(select(LLMCall))).scalars().all()
            runs = (await sess.execute(select(Run))).scalars().all()
            posts = (await sess.execute(select(Post))).scalars().all()
            slots = {x.id: x for x in (await sess.execute(select(CalendarSlot))).scalars().all()}
            leads = {x.id: x for x in (await sess.execute(select(Lead))).scalars().all()}
            msgs = (await sess.execute(select(OutreachMessage))).scalars().all()
            tenants = (await sess.execute(select(Tenant))).scalars().all()

    durations = [(n.finished_at - n.started_at).total_seconds() for n in nodes if n.started_at and n.finished_at]
    waits_by_tenant = {t: statistics.mean(w) for t, w in sched.waits.items() if w}
    all_waits = [w for ws in sched.waits.values() for w in ws]
    db_lock_errors = sum("locked" in (n.error or "").lower() for n in nodes) + \
        sum("locked" in (r.error or "").lower() for r in runs)

    # ---- integrity: every child row belongs to the same tenant as its parent; no foreign names in outputs ----
    names = {t.id: [t.name, t.founder_name] for t in tenants}
    violations = 0
    for p in posts:
        if p.slot_id and slots[p.slot_id].tenant_id != p.tenant_id:
            violations += 1
    for m in msgs:
        if leads[m.lead_id].tenant_id != m.tenant_id:
            violations += 1
    import re
    foreign_mentions = 0
    for p in posts + msgs:
        text = p.text
        for tid, terms in names.items():
            if tid == p.tenant_id:
                continue
            if any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) for term in terms if len(term) > 3):
                foreign_mentions += 1
                break

    # ---- business targets per tenant ----
    kpis = {tid: await tenant_kpis(tid) for tid in ids.values()}
    posts_ok = sum(1 for k in kpis.values() if k["posts_per_week"] and min(k["posts_per_week"].values()) >= s.posts_per_week_target)
    cal_ok = sum(1 for k in kpis.values() if k["calendar_weeks_planned"] >= s.calendar_weeks)
    leads_ok = sum(1 for k in kpis.values() if s.leads_per_week_min <= k["leads_latest_week"] <= s.leads_per_week_max)

    # ---- API load: founders hammering their inbox + calendar concurrently ----
    api = await api_load(db, ids, args.api_requests)

    status = Counter(disc) + Counter(f"weekly:{w}" for w in weekly)
    return {
        "config": {"tenants": args.tenants, "latency_ms": args.latency, "rate_limit_rate": args.rate_limit,
                   "transient_rate": args.transient, "global_concurrency": args.global_concurrency,
                   "per_tenant_concurrency": s.per_tenant_max_concurrency,
                   "provider_concurrency": args.provider_concurrency},
        "wall_seconds": round(wall, 1), "discovery_seconds": round(t_disc, 1), "weekly_seconds": round(t_week, 1),
        "run_status": dict(status),
        "nodes": len(nodes), "nodes_failed": sum(n.status == "failed" for n in nodes),
        "node_seconds_p50": round(pct(durations, 50), 3), "node_seconds_p95": round(pct(durations, 95), 3),
        "queue_wait_p50": round(pct(all_waits, 50), 3), "queue_wait_p95": round(pct(all_waits, 95), 3),
        "fairness_mean_wait_max_over_min": round(max(waits_by_tenant.values()) / max(1e-6, min(waits_by_tenant.values())), 2)
        if waits_by_tenant else None,
        "scheduler_peak_concurrency": sched.peak_running,
        "llm_calls": len(calls), "llm_calls_per_second": round(len(calls) / wall, 1),
        "llm_retries": sum(c.retries for c in calls), "llm_errors": sum(c.status == "error" for c in calls),
        "injected_failures": mock.failures,
        "db_lock_errors": db_lock_errors,
        "posts": len(posts), "posts_blocked": sum(p.status == "blocked" for p in posts),
        "leads": len(leads), "outreach": len(msgs),
        "tenants_meeting_posts_target": f"{posts_ok}/{len(ids)}",
        "tenants_meeting_calendar_target": f"{cal_ok}/{len(ids)}",
        "tenants_meeting_leads_target": f"{leads_ok}/{len(ids)}",
        "cross_tenant_fk_violations": violations, "cross_tenant_name_mentions": foreign_mentions,
        "cost_usd_total": round(sum(c.cost_usd for c in calls), 4),
        "cost_usd_per_tenant": round(sum(c.cost_usd for c in calls) / len(ids), 4),
        "api": api,
    }


async def api_load(db, ids: dict[str, str], n_requests: int) -> dict:
    """In-process ASGI load on read-heavy founder endpoints, with one founder user per tenant."""
    from app.main import app
    from app.core.security import Principal, issue_token
    async with db.session() as s:
        pw = hash_password(DEMO_PASSWORD)
        tokens = {}
        for slug, tid in ids.items():
            u = User(email=f"founder@{slug}.stress.example", name=f"Founder {slug}", role="client_owner", password_hash=pw)
            s.add(u)
            await s.flush()
            s.add(Membership(user_id=u.id, tenant_id=tid))
            tokens[tid] = issue_token(Principal(u.id, u.name, "client_owner", frozenset({tid})))
    lat: list[float] = []
    errors = Counter()
    tids = list(ids.values())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stress") as c:
        sem = asyncio.Semaphore(50)

        async def one(i: int):
            tid = tids[i % len(tids)]
            path = [f"/api/tenants/{tid}/posts?status=pending_approval", f"/api/tenants/{tid}/calendar",
                    f"/api/tenants/{tid}/leads", f"/api/tenants/{tid}/kpis"][i % 4]
            async with sem:
                t = time.monotonic()
                r = await c.get(path, headers={"Authorization": f"Bearer {tokens[tid]}"})
                lat.append(time.monotonic() - t)
                if r.status_code != 200:
                    errors[r.status_code] += 1
            # cross-tenant probe on every 10th request must be 404
            if i % 10 == 0 and len(tids) > 1:
                other = tids[(i + 1) % len(tids)]
                r = await c.get(f"/api/tenants/{other}/posts", headers={"Authorization": f"Bearer {tokens[tid]}"})
                if r.status_code != 404:
                    errors["cross_tenant_not_404"] += 1

        t0 = time.monotonic()
        await asyncio.gather(*[one(i) for i in range(n_requests)])
        wall = time.monotonic() - t0
    return {"requests": n_requests, "rps": round(n_requests / wall, 1), "p50_ms": round(pct(lat, 50) * 1000, 1),
            "p95_ms": round(pct(lat, 95) * 1000, 1), "p99_ms": round(pct(lat, 99) * 1000, 1), "errors": dict(errors)}


def to_markdown(r: dict) -> str:
    rows = "\n".join(f"| {k} | {json.dumps(v) if isinstance(v, (dict, list)) else v} |" for k, v in r.items())
    return f"# Stress test: {r['config']['tenants']} tenants\n\n| Metric | Value |\n|---|---|\n{rows}\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", type=int, default=12)
    ap.add_argument("--latency", type=int, nargs=2, default=[100, 600], metavar=("MIN_MS", "MAX_MS"))
    ap.add_argument("--rate-limit", type=float, default=0.05)
    ap.add_argument("--transient", type=float, default=0.02)
    ap.add_argument("--global-concurrency", type=int, default=48)
    ap.add_argument("--provider-concurrency", type=int, default=64)
    ap.add_argument("--api-requests", type=int, default=2000)
    args = ap.parse_args()
    report = asyncio.run(run(args))
    out = Path(__file__).parent / "reports"
    out.mkdir(exist_ok=True)
    (out / f"stress-{args.tenants}.json").write_text(json.dumps(report, indent=2))
    (out / f"stress-{args.tenants}.md").write_text(to_markdown(report))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
