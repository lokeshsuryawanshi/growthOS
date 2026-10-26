"""End-to-end pipeline in mock mode: discovery -> weekly cycles -> approvals -> learning loop."""
from collections import Counter
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.fixtures.sources import dataset_case
from app.agents.orchestrator.engine import Orchestrator
from app.core.config import get_settings
from app.core.security import ForbiddenError, Principal
from app.core.tenancy import tenant_scope
from app.db.models import (Artifact, CalendarSlot, Feedback, GuardrailIncident, KnowledgeDoc, Lead, LeadRejection,
                           OutreachMessage, Post, RunNode, Tenant)
from app.guardrails.state import IllegalTransition
from app.llm.gateway import LLMGateway, set_gateway
from app.fixtures.seed import seed
from tests.helpers import FakeGoogle, connect_fake_gmail, make_sendable
from app.services import approvals
from app.services.kpis import tenant_kpis

START = "2026-11-02"


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    ids = await seed(db)
    return db, ids, Orchestrator()


async def discover(orch, tid, **params):
    rid = await orch.create_run(tid, "discovery", {"start_date": START, **params})
    return rid, await orch.execute(tid, rid)


async def rows(db, tid, model, *where):
    with tenant_scope(tid):
        async with db.reader() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())


def owner(tid):
    return Principal("u-owner", "Founder", "client_owner", frozenset({tid}))


async def test_discovery_builds_strategy_and_8_week_calendar(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    rid, status = await discover(orch, tid, auto_approve_gates=True)
    assert status == "succeeded"
    kinds = {a.kind for a in await rows(db, tid, Artifact)}
    assert {"voice_profile", "market_report", "competitor_matrix", "trend_report", "content_strategy", "icp",
            "calendar_meta"} <= kinds
    slots = await rows(db, tid, CalendarSlot)
    per_week = Counter(s.publish_date - timedelta(days=s.publish_date.weekday()) for s in slots)
    assert len(per_week) == 8 and all(n >= 5 for n in per_week.values())
    assert len({s.topic for s in slots}) == len(slots)  # no repeated topics


async def test_human_gate_parks_and_resumes(world):
    db, ids, orch = world
    tid = ids["shiftwise"]
    rid, status = await discover(orch, tid)
    assert status == "waiting_human"
    gate = (await rows(db, tid, RunNode, RunNode.run_id == rid, RunNode.key == "strategy_review"))[0]
    assert gate.status == "waiting_human"
    await orch.approve_gate(tid, rid, "strategy_review", "u-am")
    assert await orch._tasks[rid] == "succeeded"


async def test_injection_pages_handled(world):
    db, ids, orch = world
    for slug in ("ledgerloop", "shiftwise"):
        await discover(orch, ids[slug], auto_approve_gates=True)
    tid = ids["ledgerloop"]
    docs = {d.source: d for d in await rows(db, tid, KnowledgeDoc, KnowledgeDoc.kind == "web_page")}
    ct = docs["https://www.cleartally.example/pricing"]
    assert not ct.quarantined and "ignore all previous" not in ct.content.lower()
    incidents = {(i.layer, i.rule) for i in await rows(db, tid, GuardrailIncident)}
    assert ("input", "hidden_injection_stripped") in incidents
    sw = {d.source: d for d in await rows(db, ids["shiftwise"], KnowledgeDoc, KnowledgeDoc.kind == "web_page")}
    assert sw["https://www.carestaffingnews.example/trends-2026"].quarantined
    # Nothing an agent produced may carry the injected instructions
    for a in await rows(db, ids["shiftwise"], Artifact):
        assert "developer mode" not in str(a.data).lower()


async def test_weekly_cycles_hit_targets_and_filter_adversarial_leads(world):
    db, ids, orch = world
    s = get_settings()
    tid = ids["ledgerloop"]
    await discover(orch, tid, auto_approve_gates=True)
    for _ in range(4):
        rid = await orch.create_run(tid, "weekly", {})
        assert await orch.execute(tid, rid) == "succeeded"

    k = await tenant_kpis(tid)
    assert all(n >= s.posts_per_week_target for n in k["posts_per_week"].values()) and len(k["posts_per_week"]) == 4
    assert k["calendar_weeks_planned"] >= 8
    assert all(s.leads_per_week_min <= n <= s.leads_per_week_max for n in k["leads_per_week"].values())

    leads = await rows(db, tid, Lead)
    cases = Counter(dataset_case(l.source_id) for l in leads)
    # Suppressed, competitor, low-fit and contactless prospects never become leads. A reformatted
    # duplicate may be the copy that's kept (if seen first), but each person is kept only once.
    assert set(cases) <= {"fit", "injection", "duplicate"}, cases
    assert len({l.dedupe_key for l in leads}) == len(leads)
    names = Counter(l.full_name.lower() for l in leads)
    assert max(names.values()) == 1
    rejected = Counter(r.reason for r in await rows(db, tid, LeadRejection))
    assert rejected["duplicate"] and rejected["suppressed"]
    for r in await rows(db, tid, LeadRejection):
        assert dataset_case(r.source_id, "ledgerloop") != "fit" or r.reason == "duplicate"

    # outreach for every lead, never sent automatically
    msgs = await rows(db, tid, OutreachMessage)
    assert len(msgs) == len(leads) and not any(m.status == "sent" for m in msgs)
    for l in leads:
        if dataset_case(l.source_id, "ledgerloop") == "injection":
            assert l.signal == "" and l.fit_score < 100


async def test_writer_critic_loop_and_guard_autofix(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    await discover(orch, tid, auto_approve_gates=True)
    for _ in range(3):
        await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    posts = await rows(db, tid, Post)
    assert any(p.rounds > 1 for p in posts)  # critic sent drafts back
    assert all(p.critic_score >= get_settings().critic_pass_score for p in posts if p.status == "pending_approval")
    assert not any("83%" in p.text for p in posts)  # planted uncited stat was removed by the guard auto-fix


async def test_approval_flow_rbac_and_learning(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    await discover(orch, tid, auto_approve_gates=True)
    await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    posts = await rows(db, tid, Post, Post.status == "pending_approval")
    am = Principal("u-am", "AM", "account_manager", frozenset({tid}))
    viewer = Principal("u-v", "Viewer", "client_viewer", frozenset({tid}))
    stranger = Principal("u-x", "Other founder", "client_owner", frozenset({ids["shiftwise"]}))

    with pytest.raises(ForbiddenError):
        await approvals.decide_post(am, tid, posts[0].id, "approve")       # only the founder approves
    with pytest.raises(ForbiddenError):
        await approvals.decide_post(viewer, tid, posts[0].id, "edit", "x")
    with pytest.raises(ForbiddenError):
        await approvals.decide_post(stranger, tid, posts[0].id, "approve")  # other tenant

    edited = posts[0].text.split("\n\n")[0] + "\n\nShorter, punchier version written by the founder herself. " * 6
    p = await approvals.decide_post(owner(tid), tid, posts[0].id, "approve", text=edited)
    assert p.status == "scheduled" and p.edit_distance > 0.3
    with pytest.raises(IllegalTransition):
        await approvals.decide_post(owner(tid), tid, posts[0].id, "approve")  # can't approve twice
    await approvals.decide_post(owner(tid), tid, posts[1].id, "approve")
    await approvals.decide_post(owner(tid), tid, posts[2].id, "reject", note="Too generic")
    new = await approvals.regenerate(tid, posts[3].slot_id, "Use a customer story")  # what request_changes triggers
    assert new.status in ("pending_approval", "blocked")

    # cross-tenant leakage in a human edit is refused
    with pytest.raises(approvals.GuardrailRefusal):
        await approvals.decide_post(owner(tid), tid, posts[4].id, "edit", text=posts[4].text + " Ask ShiftWise.")

    # next weekly cycle learns from the edits
    await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    voice = sorted(await rows(db, tid, Artifact, Artifact.kind == "voice_profile"), key=lambda a: a.version)[-1]
    assert voice.version >= 2 and voice.data["learned_rules"]
    assert all(f.consumed for f in await rows(db, tid, Feedback))

    k = await tenant_kpis(tid)
    assert k["approval_rate"] is not None and k["mean_edit_distance"] > 0
    scheduled = await rows(db, tid, Post, Post.status == "scheduled")
    assert scheduled
    with pytest.raises(ForbiddenError):
        await approvals.mark_posted(am, tid, scheduled[0].id)          # only the founder confirms a post went out
    assert (await approvals.mark_posted(owner(tid), tid, scheduled[0].id)).status == "published"
    with pytest.raises(IllegalTransition):
        await approvals.mark_posted(owner(tid), tid, scheduled[0].id)  # cannot be marked twice


async def test_send_gate(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    await discover(orch, tid, auto_approve_gates=True)
    await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    msgs = await rows(db, tid, OutreachMessage, OutreachMessage.status == "pending_approval")
    google = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    with pytest.raises(ForbiddenError):
        await approvals.send_outreach(owner(tid), tid, msgs[0].id)  # not approved yet
    await approvals.decide_outreach(owner(tid), tid, msgs[0].id, "approve")
    with pytest.raises(ForbiddenError, match="synthetic"):
        await approvals.send_outreach(owner(tid), tid, msgs[0].id)  # fixture leads can never be emailed
    await make_sendable(db, tid)
    sent = await approvals.send_outreach(owner(tid), tid, msgs[0].id)
    assert sent.status == "sent" and sent.sent_at and sent.external_id == "gmail-1"
    assert len(google.sent) == 1                                      # exactly one Gmail call
    with pytest.raises(ForbiddenError):
        await approvals.send_outreach(owner(tid), tid, msgs[0].id)    # cannot be sent twice
    assert len(google.sent) == 1

    await approvals.decide_outreach(owner(tid), tid, msgs[1].id, "approve")
    async with db.session() as s:
        (await s.get(Tenant, tid)).kill_switch = True
    with pytest.raises(ForbiddenError):
        await approvals.send_outreach(owner(tid), tid, msgs[1].id)


async def test_resume_after_crash_skips_finished_nodes(world):
    db, ids, orch = world
    tid = ids["gridmint"]
    rid = await orch.create_run(tid, "discovery", {"start_date": START, "auto_approve_gates": True})
    assert await orch.execute(tid, rid) == "succeeded"
    calls_before = len(await rows(db, tid, __import__("app.db.models", fromlist=["LLMCall"]).LLMCall))
    # simulate a crash in the middle of 'calendar' and resume
    with tenant_scope(tid):
        async with db.session() as s:
            for n in (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars():
                if n.key in ("calendar", "strategy_review"):
                    n.status = "running" if n.key == "calendar" else "pending"
            from app.db.models import Run
            (await s.execute(select(Run).where(Run.id == rid))).scalars().one().status = "running"
    assert await orch.execute(tid, rid) == "succeeded"
    from app.db.models import LLMCall
    new_calls = await rows(db, tid, LLMCall)
    assert {c.node_key for c in new_calls[calls_before:]} == {"calendar"}  # only the interrupted node re-ran
