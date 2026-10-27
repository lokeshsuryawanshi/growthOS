"""Strategy and calendar rules enforced in code."""
from app.agents.schemas import CalendarPlan
from app.agents.strategy.agents import canon, plain, validate_calendar


def _plan(pillar: str, topics: list[str]) -> CalendarPlan:
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    return CalendarPlan(slots=[{"week": 1, "day": days[i], "pillar": pillar, "format": "story", "topic": t, "angle": "a"}
                               for i, t in enumerate(topics)])


def test_unicode_lookalikes_in_pillar_names_do_not_fail_validation():
    """Regression (live run 2026-10-27): the strategist named a pillar with a non-breaking hyphen (U+2011), the
    planner wrote a plain hyphen, and every slot was rejected as an unknown pillar."""
    pillars = {"Seamless Move‑In Experience": 100}
    plan = _plan("Seamless Move-In  experience", [f"topic {i}" for i in range(5)])
    assert validate_calendar(plan, [1], pillars, 5) == []
    assert {s.pillar for s in plan.slots} == {"Seamless Move‑In Experience"}      # stored under the canonical name
    assert plain("Smart — Spending Now") == "Smart - Spending Now"
    assert canon("Move‑In") == canon("move-in")


def test_topics_used_elsewhere_in_the_calendar_are_rejected():
    plan = _plan("Pillar", ["Our pricing story", "topic 2", "topic 3", "topic 4", "topic 5"])
    problems = validate_calendar(plan, [1], {"Pillar": 100}, 5, used=["our pricing  story"])
    assert any("already used" in p for p in problems)


# ---------------------------------------------------------------- "Suggest changes" on the strategy
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator  # noqa: E402
from app.core.security import Principal  # noqa: E402
from app.core.tenancy import tenant_scope  # noqa: E402
from app.db.models import Artifact, Run  # noqa: E402
from app.fixtures.seed import seed  # noqa: E402
from app.llm.gateway import LLMGateway, set_gateway  # noqa: E402
from app.llm.providers import MOCK_RESPONDERS  # noqa: E402
from app.services import strategy as strategy_service  # noqa: E402


@pytest.fixture
async def researched(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    tid = (await seed(db))["ledgerloop"]
    rid = await orch.create_run(tid, "discovery", {"start_date": "2026-11-02"})
    assert await orch.execute(tid, rid) == "waiting_human"
    return db, tid, rid, orch, Principal("u-owner", "Founder", "client_owner", frozenset({tid}))


async def versions(db, tid, kind):
    with tenant_scope(tid):
        async with db.reader() as s:
            return sorted(a.version for a in (await s.execute(select(Artifact).where(Artifact.kind == kind))).scalars())


async def test_suggested_changes_revise_the_strategy_and_wait_for_approval_again(researched, monkeypatch):
    db, tid, rid, orch, owner = researched
    seen = {}
    real = MOCK_RESPONDERS["strategist"]
    monkeypatch.setitem(MOCK_RESPONDERS, "strategist", lambda req: (seen.update(req.metadata), real(req))[1])
    before = await versions(db, tid, "content_strategy")
    assert await strategy_service.suggest_changes(owner, tid, "Drop the humour pillar and add corporate relocation.",
                                                  ["strategy"]) == rid
    assert await orch._tasks[rid] == "waiting_human"                      # the revised version needs approval again
    assert seen["feedback"] == ["Drop the humour pillar and add corporate relocation."]
    assert len(await versions(db, tid, "content_strategy")) == len(before) + 1
    assert await versions(db, tid, "icp") == [1]                          # only the parts asked for are redone
    history = await strategy_service.suggestion_history(tid)
    assert history[0]["note"].startswith("Drop the humour") and history[0]["by"] == "u-owner"


async def test_suggestions_are_refused_while_a_run_is_busy_or_before_research(researched, db):
    _, tid, rid, orch, owner = researched
    with tenant_scope(tid):
        async with db.session() as s:
            (await s.execute(select(Run).where(Run.id == rid))).scalars().one().status = "running"
    with pytest.raises(strategy_service.SuggestionRefused, match="in progress"):
        await strategy_service.suggest_changes(owner, tid, "Add a pillar on pricing transparency.", ["strategy"])
    viewer = Principal("u-v", "Viewer", "client_viewer", frozenset({tid}))
    from app.core.security import ForbiddenError
    with pytest.raises(ForbiddenError):
        await strategy_service.suggest_changes(viewer, tid, "Add a pillar on pricing transparency.", ["strategy"])
