import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agents.runtime.loop import AgentContext, AgentError, Tool, run_agent, structured
from app.core.tenancy import tenant_scope
from app.db.models import LLMCall, Tenant, TraceEvent
from app.llm.gateway import LLMGateway, TenantPaused, set_gateway
from app.llm.providers import MOCK_RESPONDERS, MockConfig
from app.llm.types import Budget, BudgetExceeded, CallContext, LLMRequest


class Out(BaseModel):
    answer: str
    score: int


@pytest.fixture
async def tenant(db):
    async with db.session() as s:
        s.add(Tenant(id="t1", slug="t1", name="T1", monthly_budget_usd=5.0))
    return "t1"


def ctx(tid, **kw):
    return AgentContext(CallContext(tenant_id=tid, **kw))


async def test_retry_on_429_then_success_and_ledger(db, tenant):
    gw = LLMGateway(mock_config=MockConfig(rate_limit_rate=0.0))
    set_gateway(gw)
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    calls = {"n": 0}
    orig = gw.providers["mock"].complete

    async def flaky(model, req):
        calls["n"] += 1
        if calls["n"] < 3:
            from app.llm.providers import RateLimited
            raise RateLimited("429", retry_after=0.01)
        return await orig(model, req)

    gw.providers["mock"].complete = flaky
    res = await structured("t_echo", "small", "sys", "user", Out, ctx(tenant))
    assert res.output.answer == "hi"
    with tenant_scope(tenant):
        async with db.reader() as s:
            row = (await s.execute(select(LLMCall))).scalars().one()
    assert row.retries == 2 and row.cost_usd > 0 and row.status == "ok"


async def test_repair_loop_fixes_invalid_json(db, tenant):
    set_gateway(LLMGateway())
    MOCK_RESPONDERS["t_repair"] = lambda req: (
        {"content": "not json at all"} if req.metadata["attempt"] == 0
        else {"content": {"answer": "fixed", "score": 5}})
    c = ctx(tenant)
    res = await structured("t_repair", "big", "sys", "user", Out, c)
    assert res.output.answer == "fixed" and res.repairs == 1
    assert any(e["type"] == "repair" for e in c.trace)


async def test_repair_gives_up(db, tenant):
    set_gateway(LLMGateway())
    MOCK_RESPONDERS["t_bad"] = lambda req: {"content": {"answer": "x"}}  # missing score forever
    with pytest.raises(AgentError):
        await structured("t_bad", "big", "sys", "user", Out, ctx(tenant), max_repairs=2)


async def test_run_budget_stops_calls(db, tenant):
    set_gateway(LLMGateway())
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    budget = Budget(max_usd=0.0)
    with pytest.raises(BudgetExceeded):
        await structured("t_echo", "big", "sys", "user", Out, ctx(tenant, budget=budget))


async def test_tenant_monthly_budget_and_kill_switch(db, tenant):
    set_gateway(gw := LLMGateway())
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    async with db.session() as s:
        (await s.get(Tenant, tenant)).kill_switch = True
    with pytest.raises(TenantPaused):
        await structured("t_echo", "big", "sys", "user", Out, ctx(tenant))
    async with db.session() as s:
        t = await s.get(Tenant, tenant)
        t.kill_switch, t.monthly_budget_usd = False, 0.0
    gw._tenants.clear()
    with pytest.raises(BudgetExceeded):
        await structured("t_echo", "big", "sys", "user", Out, ctx(tenant))


async def test_cache_hit_costs_nothing(db, tenant):
    set_gateway(gw := LLMGateway())
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    req = LLMRequest(agent="t_echo", tier="small", messages=[{"role": "user", "content": "same"}], cacheable=True)
    first = await gw.complete(req, CallContext(tenant_id=tenant))
    second = await gw.complete(req, CallContext(tenant_id=tenant))
    assert not first.cached and second.cached and second.cost_usd == 0


async def test_circuit_breaker_opens(db, tenant):
    from app.llm.gateway import LLMUnavailable
    gw = LLMGateway(mock_config=MockConfig(transient_rate=1.0))
    gw.s = gw.s.model_copy(update={"llm_max_retries": 0, "circuit_failure_threshold": 2})
    gw.breakers["mock"].threshold = 2
    set_gateway(gw)
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    req = LLMRequest(agent="t_echo", tier="small", messages=[{"role": "user", "content": "x"}])
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await gw.complete(req, CallContext(tenant_id=tenant))
    assert gw.breakers["mock"].is_open
    with pytest.raises(LLMUnavailable, match="circuit open"):
        await gw.complete(req, CallContext(tenant_id=tenant))


class SearchArgs(BaseModel):
    query: str


async def test_tool_loop_guard_and_submit(db, tenant):
    set_gateway(LLMGateway())

    def script(req):
        step = req.metadata["step"]
        if step == 1:
            return {"tool_calls": [{"name": "search", "arguments": {"query": "x"}},
                                   {"name": "delete_everything", "arguments": {}},
                                   {"name": "search", "arguments": {"query": "y", "tenant_id": "other"}}]}
        return {"tool_calls": [{"name": "submit_result", "arguments": {"answer": "done", "score": 4}}]}

    MOCK_RESPONDERS["t_tools"] = script

    async def search(args, c):
        return {"results": [f"result for {args.query}"]}

    c = ctx(tenant, run_id="r1", node_key="n1")
    res = await run_agent("t_tools", "big", "sys", "go", Out, [Tool("search", "search", SearchArgs, search)], c)
    assert res.output.answer == "done"
    types = [e["type"] for e in c.trace]
    assert types.count("tool_denied") == 2 and "tool_call" in types
    await c.flush()
    with tenant_scope(tenant):
        async with db.reader() as s:
            assert len((await s.execute(select(TraceEvent))).scalars().all()) == len(types)


async def test_tool_loop_step_cap(db, tenant):
    set_gateway(LLMGateway())
    MOCK_RESPONDERS["t_loop"] = lambda req: {"tool_calls": [{"name": "search", "arguments": {"query": "again"}}]}

    async def search(args, c):
        return "same"

    with pytest.raises(AgentError):
        await run_agent("t_loop", "big", "sys", "go", Out, [Tool("search", "s", SearchArgs, search)],
                        ctx(tenant), max_steps=3)
