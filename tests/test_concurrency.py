"""Three accounts with two clients each, all running at once (mock models): no cross-talk, no lock errors, every
client gets its own posts and leads, and every model call is booked to the client it was made for."""
import asyncio

from sqlalchemy import select

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.tenancy import system_scope, tenant_scope
from app.db.models import LLMCall, Lead, Post, Run, Tenant
from app.fixtures.clients import CLIENTS
from app.fixtures.seed import seed
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import MockConfig


async def test_three_accounts_two_clients_each_run_at_once(db):
    set_gateway(LLMGateway(mock_config=MockConfig(rate_limit_rate=0.05, transient_rate=0.02, seed=3)))
    set_orchestrator(orch := Orchestrator())
    by_slug = {c["slug"]: c for c in CLIENTS}
    plan = {"acct-a": ["ledgerloop", "shiftwise"], "acct-b": ["gridmint", "cargocompass"],
            "acct-c": ["lumencohort", "harborshield"]}
    clients = {}
    for acct, slugs in plan.items():
        ids = await seed(db, clients=[by_slug[s] for s in slugs], owner={"email": f"{acct}@example.com", "name": acct})
        clients.update({ids[s]: acct for s in slugs})

    async def one(tid: str) -> tuple[str, str, str]:
        disc = await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02",
                                                                                "auto_approve_gates": True}))
        content, leads = await asyncio.gather(orch.execute(tid, await orch.create_run(tid, "content", {})),
                                              orch.execute(tid, await orch.create_run(tid, "leads", {})))
        return disc, content, leads
    results = await asyncio.gather(*[one(t) for t in clients])
    assert all(r[0] in ("succeeded", "succeeded_with_shortfall") for r in results), results
    assert all(r[1] in ("succeeded", "succeeded_with_shortfall") and r[2] in ("succeeded", "succeeded_with_shortfall")
               for r in results), results

    async with db.reader() as s:
        owners = {t.id: t.owner_user_id for t in (await s.execute(select(Tenant))).scalars()}
    assert len(set(owners.values())) == 3
    with system_scope("test: check every row is booked to its own client"):
        async with db.reader() as s:
            runs = (await s.execute(select(Run))).scalars().all()
            calls = (await s.execute(select(LLMCall))).scalars().all()
            errors = [r.error for r in runs if "locked" in (r.error or "").lower()]
    assert errors == []
    run_tenant = {r.id: r.tenant_id for r in runs}
    assert all(c.run_id is None or run_tenant[c.run_id] == c.tenant_id for c in calls)    # no call booked elsewhere
    async with db.reader() as s:
        names = {t.id: t.name for t in (await s.execute(select(Tenant))).scalars()}
    for tid in clients:
        with tenant_scope(tid):
            async with db.reader() as s:
                posts = (await s.execute(select(Post))).scalars().all()
                leads = (await s.execute(select(Lead))).scalars().all()
        assert posts and leads, tid
        text = " ".join(p.text for p in posts).lower()
        for other, name in names.items():
            if other != tid:
                assert name.lower() not in text, (names[tid], "mentions", name)      # no cross-talk between clients
        assert all(l.tenant_id == tid for l in leads) and all(p.tenant_id == tid for p in posts)
