"""Search/contact credits in the cost ledger, and the run progress estimate shown to the founder."""
import pytest
from sqlalchemy import select

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.tenancy import tenant_scope
from app.db.models import ExternalCall, Run, RunNode, Tenant
from app.db.usage import record_external
from app.fixtures.seed import seed
from app.llm.gateway import LLMGateway, set_gateway
from app.services import queries
from app.services.kpis import tenant_kpis
from app.services.progress import run_progress


async def test_external_calls_are_priced_and_reported(db):
    async with db.session() as s:
        s.add(Tenant(id="t1", slug="t1", name="T1"))
    await record_external("t1", "tavily", "search", run_id="r1", node_key="market")
    await record_external("t1", "hunter", "domain_search", run_id="r1", node_key="leads")
    await record_external("t1", "hunter", "verify", run_id="r1", node_key="leads")
    with tenant_scope("t1"):
        async with db.reader() as s:
            rows = (await s.execute(select(ExternalCall))).scalars().all()
    assert {(r.provider, r.operation) for r in rows} == {("tavily", "search"), ("hunter", "domain_search"), ("hunter", "verify")}
    assert all(r.cost_usd > 0 for r in rows)
    k = await tenant_kpis("t1")
    assert k["external_cost_month_usd"] == pytest.approx(sum(r.cost_usd for r in rows), abs=1e-4)
    costs = await queries.cost_breakdown("t1")
    assert {(e["provider"], e["operation"]) for e in costs["external"]} == {("tavily", "search"), ("hunter", "domain_search"),
                                                                           ("hunter", "verify")}


async def test_fixture_searches_cost_nothing_and_are_not_recorded(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    tid = (await seed(db))["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
    with tenant_scope(tid):
        async with db.reader() as s:
            assert (await s.execute(select(ExternalCall))).scalars().all() == []


async def test_progress_estimates_time_left_along_the_critical_path(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    tid = (await seed(db))["ledgerloop"]
    rid = await orch.create_run(tid, "discovery", {"start_date": "2026-11-02"})
    with tenant_scope(tid):
        async with db.session() as s:
            (await s.execute(select(Run).where(Run.id == rid))).scalars().one().status = "running"
        async with db.reader() as s:
            run = (await s.execute(select(Run).where(Run.id == rid))).scalars().one()
            nodes = (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars().all()
    pg = await run_progress(tid, run, nodes)
    # nothing has run yet: ingest -> competitors -> strategy -> calendar is the longest path with the default timings
    assert pg["eta_s"] >= 2 + 30 + 5 + 35
    assert {s["key"] for s in pg["steps"]} == {n.key for n in nodes}
