"""Content/calendar and lead generation are two independent workflows."""
import pytest
from sqlalchemy import func, select

from app.agents.orchestrator.engine import DAGS, Orchestrator, set_orchestrator
from app.core.security import Principal
from app.core.tenancy import tenant_scope
from app.db.models import Lead, OutreachMessage, Post
from app.fixtures.seed import seed
from app.guardrails.output_guard import PolicyContext, check_outreach, check_post
from app.llm.gateway import LLMGateway, set_gateway
from app.services import runs as run_service
from app.services.kpis import tenant_kpis
from app.web.routes import workspace_state


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    ids = await seed(db)
    tid = ids["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
    return db, tid, orch, Principal("u-owner", "Founder", tenant_ids=frozenset({tid}))


async def count(db, tid, model):
    with tenant_scope(tid):
        async with db.reader() as s:
            return (await s.execute(select(func.count(model.id)))).scalar_one()


def test_dags_do_not_share_nodes():
    assert set(DAGS["content"]).isdisjoint(DAGS["leads"])
    assert "leads" not in DAGS["content"] and "draft_week" not in DAGS["leads"]


async def test_content_run_writes_posts_and_no_leads(world):
    db, tid, orch, owner = world
    rid = await run_service.start_run(owner, tid, "content")
    assert await orch._tasks[rid] == "succeeded"
    assert await count(db, tid, Post) >= 5
    assert await count(db, tid, Lead) == 0 and await count(db, tid, OutreachMessage) == 0


async def test_lead_run_finds_leads_and_writes_no_posts(world):
    db, tid, orch, owner = world
    rid = await run_service.start_run(owner, tid, "leads")
    assert await orch._tasks[rid] == "succeeded"
    assert await count(db, tid, Lead) >= 10 and await count(db, tid, OutreachMessage) >= 10
    assert await count(db, tid, Post) == 0
    k = await tenant_kpis(tid)
    assert k["leads_latest_week"] >= 10 and k["posts_pending"] == 0


async def test_content_and_lead_runs_can_overlap_but_nothing_else_can(world):
    db, tid, orch, owner = world
    assert run_service._conflicts("content", "leads") is False
    for a, b in [("content", "content"), ("leads", "leads"), ("discovery", "leads"), ("discovery", "content")]:
        assert run_service._conflicts(a, b) is True
    c = await orch.create_run(tid, "content", {})                # a pending content run is "active"
    ws = await workspace_state(tid, await tenant_kpis(tid))
    assert ws["can_content"] is False and ws["can_leads"] is True
    with pytest.raises(run_service.RunRefused, match="already"):
        await run_service.start_run(owner, tid, "content")
    leads_run = await run_service.start_run(owner, tid, "leads")  # allowed while content is active
    assert leads_run != c


async def test_content_and_leads_refuse_before_research(db):
    set_gateway(LLMGateway())
    set_orchestrator(Orchestrator())
    ids = await seed(db)
    tid = ids["shiftwise"]
    owner = Principal("u", "F", tenant_ids=frozenset({tid}))
    for kind in ("content", "leads"):
        with pytest.raises(run_service.RunRefused, match="research"):
            await run_service.start_run(owner, tid, kind)


def test_agency_name_never_appears_in_client_voice():
    p = PolicyContext("Rentomojo")
    post = check_post("At Growth OS we help founders like you amplify the story. " * 8, [], p)
    assert any(v.rule == "agency_name_leak" for v in post.violations) and not post.passed
    mail = check_outreach("Hi Khaja, I noticed Rentomojo customers face upfront costs. At Growth OS we help founders like you "
                          "amplify the story of cost-saving rentals, so would a short chat help?" * 2, "Rentomojo", "", p)
    assert any(v.rule == "agency_name_leak" for v in mail.violations) and not mail.passed
