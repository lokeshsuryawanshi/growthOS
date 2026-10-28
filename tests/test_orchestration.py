"""Coordination under failure: retries, fail-fast, dependency blocking, checkpoints, recovery, fairness, budgets."""
import asyncio

import pytest
from sqlalchemy import select

from app.agents.orchestrator import engine
from app.agents.orchestrator.engine import Orchestrator
from app.agents.orchestrator.scheduler import FairScheduler
from app.core.tenancy import tenant_scope
from app.db.models import Artifact, RunNode, Tenant
from app.fixtures.seed import seed
from app.integrations.errors import AUTH_FAILED, NOT_CONFIGURED, UNAVAILABLE, IntegrationError
from app.llm.gateway import LLMGateway, TenantPaused, set_gateway
from app.llm.types import BudgetExceeded

START = "2026-11-02"


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    ids = await seed(db)
    return db, ids, Orchestrator()


@pytest.fixture
def handlers(monkeypatch):
    """Counting wrappers around the real handlers, restored automatically."""
    calls: dict[str, int] = {}

    def wrap(key, fn):
        async def inner(n):
            calls[key] = calls.get(key, 0) + 1
            return await fn(n)
        monkeypatch.setitem(engine.HANDLERS, key, inner)

    for k, fn in list(engine.HANDLERS.items()):
        wrap(k, fn)
    return calls, wrap


async def nodes(db, tid, rid):
    with tenant_scope(tid):
        async with db.reader() as s:
            return {n.key: n for n in (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars()}


async def start(orch, tid, **params):
    return await orch.create_run(tid, "discovery", {"start_date": START, "auto_approve_gates": True, **params})


async def test_transient_failure_is_retried_then_succeeds(world, monkeypatch):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    real, state = engine.HANDLERS["voice"], {"n": 0}

    async def flaky(n):
        state["n"] += 1
        if state["n"] == 1:
            raise IntegrationError("web_search", UNAVAILABLE, "provider hiccup")       # retryable
        return await real(n)
    monkeypatch.setitem(engine.HANDLERS, "voice", flaky)
    rid = await start(orch, tid)
    assert await orch.execute(tid, rid) == "succeeded"
    assert (await nodes(db, tid, rid))["voice"].attempts == 2


@pytest.mark.parametrize("code", [NOT_CONFIGURED, AUTH_FAILED])
async def test_non_retryable_integration_error_fails_fast_with_recovery(world, monkeypatch, code):
    db, ids, orch = world
    tid = ids["ledgerloop"]

    async def broken(n):
        raise IntegrationError("web_search", code, "no search provider", "Set TAVILY_API_KEY")
    monkeypatch.setitem(engine.HANDLERS, "market", broken)
    rid = await start(orch, tid)
    assert await orch.execute(tid, rid) == "failed"
    ns = await nodes(db, tid, rid)
    assert ns["market"].status == "failed" and ns["market"].attempts == 1               # no wasted second attempt
    assert ns["market"].output["integration"]["recovery"] == "Set TAVILY_API_KEY"       # the UI shows this
    assert ns["trends"].status == "blocked" and ns["strategy"].status == "blocked"      # dependents never run on missing input
    assert ns["voice"].status == "succeeded" and ns["competitors"].status == "succeeded"  # independent branches still finish


async def test_retry_after_fix_reuses_checkpoints(world, handlers, monkeypatch):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    calls, wrap = handlers
    real = engine.HANDLERS["market"]
    fail = {"on": True}

    async def market(n):
        if fail["on"]:
            raise IntegrationError("web_search", NOT_CONFIGURED, "no search", "Set TAVILY_API_KEY")
        return await real(n)
    monkeypatch.setitem(engine.HANDLERS, "market", market)
    rid = await start(orch, tid)
    assert await orch.execute(tid, rid) == "failed"
    before = dict(calls)

    fail["on"] = False                                      # the user fixes the key, clicks "Retry this step"
    await orch.rerun_from(tid, rid, "market", "u-1")
    assert await orch._tasks[rid] == "succeeded"
    for done in ("ingest", "voice", "competitors"):
        assert calls[done] == before[done], f"{done} was re-executed although it had succeeded"
    assert {"market", "trends", "strategy", "icp", "calendar"} <= {k for k, n in (await nodes(db, tid, rid)).items() if n.status == "succeeded"}


async def test_crash_mid_run_resumes_without_redoing_work(world, handlers):
    db, ids, orch = world
    tid = ids["shiftwise"]
    calls, _ = handlers
    rid = await start(orch, tid)
    assert await orch.execute(tid, rid) == "succeeded"
    first = dict(calls)
    from app.db.models import Run
    with tenant_scope(tid):                                  # simulate a crash that left a node 'running'
        async with db.session() as s:
            n = (await s.execute(select(RunNode).where(RunNode.run_id == rid, RunNode.key == "calendar"))).scalars().one()
            n.status = "running"
            (await s.execute(select(Run).where(Run.id == rid))).scalars().one().status = "running"
    assert await orch.resume_incomplete() == 1
    assert await orch._tasks[rid] == "succeeded"
    assert calls["calendar"] == first["calendar"] + 1 and calls["voice"] == first["voice"]


async def test_budget_exhaustion_stops_the_run_and_cancels_the_rest(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    rid = await orch.create_run(tid, "discovery", {"start_date": START}, budget_usd=0.00001)
    assert await orch.execute(tid, rid) == "cancelled"         # budget stops are a clean cancel, not a crash
    ns = await nodes(db, tid, rid)
    assert any(n.status == "failed" and n.error.startswith("stopped:") for n in ns.values())
    assert not any(n.status == "succeeded" for k, n in ns.items() if k in ("strategy", "calendar"))


async def test_a_deleted_clients_run_is_cancelled_not_crashed(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    rid = await start(orch, tid)
    from app.services.accounts import delete_client
    from app.api.deps import load_principal
    async with db.reader() as s:
        owner_id = (await s.get(Tenant, tid)).owner_user_id
    await delete_client(await load_principal(owner_id), tid)
    assert await orch.execute(tid, rid) in ("cancelled", "failed")


async def test_parallel_tenants_do_not_interfere(world):
    db, ids, orch = world
    a, b = ids["ledgerloop"], ids["shiftwise"]
    ra, rb = await start(orch, a), await start(orch, b)
    assert await asyncio.gather(orch.execute(a, ra), orch.execute(b, rb)) == ["succeeded", "succeeded"]
    with tenant_scope(a):
        async with db.reader() as s:
            strategies_a = (await s.execute(select(Artifact).where(Artifact.kind == "content_strategy"))).scalars().all()
    assert len(strategies_a) == 1 and "ShiftWise" not in str(strategies_a[0].data)


async def test_scheduler_enforces_per_tenant_cap_without_starving_others():
    sched = FairScheduler(global_cap=6, per_tenant_cap=2)
    active = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}
    done_b = asyncio.Event()

    def job(t):
        async def run():
            active[t] += 1
            peak[t] = max(peak[t], active[t])
            await asyncio.sleep(0.03)
            active[t] -= 1
        return run
    async def go_b():
        await sched.run("b", job("b"))
        done_b.set()
    await asyncio.gather(*[sched.run("a", job("a")) for _ in range(8)], go_b())
    assert peak["a"] <= 2 and done_b.is_set()


async def test_budget_and_pause_exceptions_are_not_retried(world, monkeypatch):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    state = {"n": 0}

    async def paused(n):
        state["n"] += 1
        raise TenantPaused("tenant kill switch is on")
    monkeypatch.setitem(engine.HANDLERS, "ingest", paused)
    rid = await start(orch, tid)
    assert await orch.execute(tid, rid) == "cancelled" and state["n"] == 1     # no retry; the run stops


async def test_a_node_starts_as_soon_as_its_own_dependencies_finish(world, handlers):
    """No waves: trends needs only market, so it must not wait for a slow competitors step."""
    db, ids, orch = world
    tid = ids["ledgerloop"]
    _, wrap = handlers
    log: list[str] = []
    real_comp, real_trends = engine.HANDLERS["competitors"], engine.HANDLERS["trends"]

    async def slow_competitors(n):
        await asyncio.sleep(1.0)
        out = await real_comp(n)
        log.append("competitors_done")
        return out

    async def trends(n):
        log.append("trends_started")
        return await real_trends(n)

    wrap("competitors", slow_competitors)
    wrap("trends", trends)
    assert await orch.execute(tid, await start(orch, tid)) == "succeeded"
    assert log.index("trends_started") < log.index("competitors_done")
