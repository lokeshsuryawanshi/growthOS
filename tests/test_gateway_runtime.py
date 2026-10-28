import json

import pytest

from tests.helpers import bare_tenants
from pydantic import BaseModel
from sqlalchemy import select

from app.agents.runtime.loop import AgentContext, AgentError, Tool, run_agent, structured
from app.core.tenancy import tenant_scope
from app.db.models import LLMCall, Tenant, TraceEvent
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import MOCK_RESPONDERS, MockConfig
from app.llm.types import Budget, BudgetExceeded, CallContext, LLMRequest


class Out(BaseModel):
    answer: str
    score: int


@pytest.fixture
async def tenant(db):
    await bare_tenants(db, "t1")
    return "t1"


def ctx(tid, **kw):
    return AgentContext(CallContext(tenant_id=tid, **kw))


async def test_retry_on_429_then_success_and_ledger(db, tenant):
    gw = LLMGateway(mock_config=MockConfig(rate_limit_rate=0.0))
    set_gateway(gw)
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    calls = {"n": 0}
    orig = gw.mock.complete

    async def flaky(model, req):
        calls["n"] += 1
        if calls["n"] < 3:
            from app.llm.providers import RateLimited
            raise RateLimited("429", retry_after=0.01)
        return await orig(model, req)

    gw.mock.complete = flaky
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


async def test_the_accounts_own_monthly_cap_stops_calls(db, tenant):
    set_gateway(gw := LLMGateway())
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    await structured("t_echo", "big", "sys", "user", Out, ctx(tenant))        # no cap set: allowed
    from app.db.models import User
    async with db.session() as s:
        (await s.get(User, "u-test")).monthly_cap_usd = 0.0
    gw._accounts.clear()
    with pytest.raises(BudgetExceeded, match="monthly AI spend cap"):
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
    set_gateway(gw)
    MOCK_RESPONDERS["t_echo"] = lambda req: {"content": {"answer": "hi", "score": 3}}
    req = LLMRequest(agent="t_echo", tier="small", messages=[{"role": "user", "content": "x"}])
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await gw.complete(req, CallContext(tenant_id=tenant))
    assert gw.breakers[("mock", "mock")].is_open
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


def test_retry_hint_parsing():
    from app.llm.providers import retry_hint
    assert retry_hint("Rate limit reached ... Please try again in 10.14s. Need more") == pytest.approx(10.14)
    assert retry_hint("Please try again in 450ms") == pytest.approx(0.45)
    assert retry_hint("Please try again in 1m2.5s") == pytest.approx(62.5)
    assert retry_hint("no hint") is None


async def test_token_bucket_paces_requests():
    import time
    from app.llm.providers import TokenBucket
    b = TokenBucket(tokens_per_minute=600)  # 10 tokens/s
    t0 = time.monotonic()
    await b.acquire(600)       # drains the minute
    waited = await b.acquire(5)  # needs ~0.5s of refill
    assert 0.4 <= time.monotonic() - t0 <= 1.5 and waited > 0


def test_loose_schema_strips_constraints():
    from app.agents.runtime.loop import _clean_schema, loose_schema
    from app.agents.schemas import CompetitorMatrix
    s = json.dumps(loose_schema(_clean_schema(CompetitorMatrix)))
    assert '"required"' not in s and "minItems" not in s and '"properties"' in s
