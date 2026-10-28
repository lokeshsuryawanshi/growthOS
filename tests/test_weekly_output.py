"""A weekly run delivers the client's targets or says exactly why not (P0-C), and leads need evidence."""
import httpx
import pytest
from sqlalchemy import select

from app.agents.leads import agents as leads_mod
from app.agents.leads.agents import grounded_evidence
from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.config import get_settings
from app.core.security import Principal
from app.core.tenancy import tenant_scope
from app.db.models import CalendarSlot, Post, Run, RunNode
from app.fixtures.seed import seed
from app.integrations import http
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import MOCK_RESPONDERS
from app.services import runs as run_service


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    ids = await seed(db)
    tid = ids["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
    return db, tid, orch, Principal("u-owner", "Founder", tenant_ids=frozenset({tid}))


async def rows(db, tid, model, *where):
    with tenant_scope(tid):
        async with db.reader() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())


async def node(db, tid, rid, key):
    return (await rows(db, tid, RunNode, RunNode.run_id == rid, RunNode.key == key))[0]


async def test_a_blocked_draft_is_redrafted_and_the_target_is_still_met(world, monkeypatch):
    db, tid, orch, owner = world
    real = MOCK_RESPONDERS["writer"]
    attempt: dict[str, int] = {}

    def writer(req):
        out = real(req)
        sid = req.metadata["slot"]["id"]
        if req.metadata.get("round") == 0:
            attempt[sid] = attempt.get(sid, 0) + 1
        if attempt[sid] == 1:   # the whole first attempt keeps an invented figure, even through the guard's auto-fixes
            out["content"]["text"] += "\n\nWe cut costs by 47% for 2,300 customers last year."
            out["content"]["claims"] = []
        return out
    monkeypatch.setitem(MOCK_RESPONDERS, "writer", writer)
    rid = await run_service.start_run(owner, tid, "content")
    assert await orch._tasks[rid] == "succeeded"
    out = (await node(db, tid, rid, "draft_week")).output
    assert out["ready"] >= out["target"] == 5 and out["shortfall"] == []
    posts = await rows(db, tid, Post, Post.run_id == rid)
    ready = [p for p in posts if p.status == "pending_approval"]
    assert len(ready) >= 5 and not any("47%" in p.text for p in ready)
    assert any(p.status == "blocked" and "47%" in p.text for p in posts)              # the audit trail is kept
    slots = {s.post_id for s in await rows(db, tid, CalendarSlot, CalendarSlot.status == "drafted")}
    assert {p.id for p in ready} <= slots                                              # slots point at the delivered post


async def test_drafts_below_the_editors_bar_are_labelled_and_the_run_reports_a_shortfall(world, monkeypatch):
    db, tid, orch, owner = world
    monkeypatch.setitem(MOCK_RESPONDERS, "critic", lambda req: {"content": {
        "voice_match": 3, "specificity": 3, "opening": 3, "insight": 3, "ending": 3,
        "must_fix": ["Make the point sharper"], "verdict": "Generic."}})
    rid = await run_service.start_run(owner, tid, "content")
    assert await orch._tasks[rid] == "succeeded_with_shortfall"
    run = (await rows(db, tid, Run, Run.id == rid))[0]
    assert "0 of 5 posts ready" in run.error and "editor's 4.0 bar" in run.error
    shown = await rows(db, tid, Post, Post.run_id == rid, Post.status == "pending_approval")
    assert shown and all(any(w["rule"] == "below_quality_bar" for w in p.guardrail_report["warnings"]) for p in shown)


async def test_a_week_with_too_few_planned_slots_says_so(world):
    db, tid, orch, owner = world
    with tenant_scope(tid):
        async with db.session() as s:
            first = (await s.execute(select(CalendarSlot).order_by(CalendarSlot.publish_date))).scalars().first()
            week = [x for x in (await s.execute(select(CalendarSlot))).scalars()
                    if (x.publish_date - first.publish_date).days < 7]
            for x in week[2:]:
                x.status = "skipped"
    rid = await run_service.start_run(owner, tid, "content")
    assert await orch._tasks[rid] == "succeeded_with_shortfall"
    out = (await node(db, tid, rid, "draft_week")).output
    assert any("only 2 slots were planned" in r for r in out["shortfall"])


def test_lead_evidence_must_quote_the_prospects_own_data():
    p = {"company": "Acme Rentals", "industry": "Furniture rental", "signal": "Opened 3 new warehouses in Pune",
         "why_fit": "Mid-size rental company expanding", "company_domain": "acmerentals.in"}
    assert grounded_evidence(["Opened 3 new warehouses in Pune", "Furniture rental company"], p) == \
        ["Opened 3 new warehouses in Pune", "Furniture rental company"]
    assert grounded_evidence(["Raised $40M Series B last month"], p) == []          # a figure the data never mentions
    assert grounded_evidence(["Strong overall fit for the ICP"], p) == []           # generic, about nothing in the data


