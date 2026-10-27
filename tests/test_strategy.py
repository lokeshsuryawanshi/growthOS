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
