"""Strategy agents: content strategy, ICP, and the 8-week calendar.

The calendar planner is checked against business rules in code (>= N posts on
distinct days each week, known pillars, pillar mix, unique topics). Violations
are fed back for a repair round: the model proposes, deterministic code disposes.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import delete, select

from app.agents.blackboard import compact, latest, primary_goal, profile, tenant_goals
from app.agents.runtime.loop import AgentContext, AgentError, structured
from app.agents.schemas import FORMATS, ICP, CalendarPlan, ContentStrategy
from app.core.tenancy import tenant_scope
from app.db.models import CalendarSlot
from app.db.session import get_db

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def strategist(tenant_id: str, ctx: AgentContext) -> ContentStrategy:
    intake = (await profile(tenant_id)).intake
    inputs = {k: await latest(tenant_id, k) for k in ("voice_profile", "market_report", "competitor_matrix", "trend_report")}
    goals = tenant_goals(intake)
    user = (f"Client: {intake.get('company_name')}. Primary goal: {primary_goal(intake)}. "
            f"Brand tone: {intake.get('tone', '')}. "
            f"Topics to own: {intake.get('topics_to_own', [])}. Topics to avoid: {intake.get('topics_to_avoid', [])}.\n"
            f"Voice: {compact(inputs['voice_profile'], 900)}\nMarket: {compact(inputs['market_report'], 1500)}\n"
            f"Competitors: {compact(inputs['competitor_matrix'], 1500)}\nTrends: {compact(inputs['trend_report'], 1500)}")
    res = await structured("strategist", "big",
                           "You are a B2B content strategist for founder-led LinkedIn. Define positioning against the "
                           f"competitors' whitespace, 3-5 content pillars whose share_pct sums to 100, a format mix and a "
                           f"cadence of at least {goals['posts_per_week']} posts per week.",
                           user, ContentStrategy, ctx, max_tokens=3000, reasoning="medium",
                           metadata={"intake": intake, **inputs})
    return res.output


async def icp_builder(tenant_id: str, ctx: AgentContext) -> ICP:
    intake = (await profile(tenant_id)).intake
    market, comp = await latest(tenant_id, "market_report"), await latest(tenant_id, "competitor_matrix")
    user = (f"Client: {intake.get('company_name')}: {intake.get('company')}\nOfferings: {intake.get('offerings', [])}\n"
            f"Stated target customers: {intake.get('target_customers')}\nMarket: {compact(market, 1500)}\n"
            f"Competitors: {compact(comp, 800)}")
    res = await structured("icp_builder", "big",
                           "Define the ideal customer profile for outbound prospecting: 1-3 segments with job titles, "
                           "industries, company size and geography, their pains and buying triggers. Exclude the client's "
                           "competitors and their staff.", user, ICP, ctx, max_tokens=2000, metadata={"intake": intake})
    return res.output


def next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def validate_calendar(plan: CalendarPlan, weeks: list[int], pillars: dict[str, int], per_week: int) -> list[str]:
    problems = []
    by_week: dict[int, list] = {w: [] for w in weeks}
    for s in plan.slots:
        if s.week not in by_week:
            problems.append(f"slot in week {s.week} is outside requested weeks {weeks}")
            continue
        by_week[s.week].append(s)
        if s.pillar not in pillars:
            problems.append(f"unknown pillar '{s.pillar}' (use exactly one of {list(pillars)})")
        if s.day not in DAYS:
            problems.append(f"bad day '{s.day}'")
    for w, slots in by_week.items():
        days = {s.day for s in slots}
        if len(days) < per_week:
            problems.append(f"week {w} has posts on {len(days)} distinct days; need at least {per_week}")
    topics = Counter(s.topic.strip().lower() for s in plan.slots)
    dupes = [t for t, n in topics.items() if n > 1]
    if dupes:
        problems.append(f"duplicate topics: {dupes[:3]}")
    total = max(1, len(plan.slots))
    for name, share in pillars.items():
        actual = 100 * sum(1 for s in plan.slots if s.pillar == name) / total
        if abs(actual - share) > 20:
            problems.append(f"pillar '{name}' is {actual:.0f}% of slots, target {share}%")
    return problems


async def calendar_planner(tenant_id: str, ctx: AgentContext, start: date, weeks: list[int]) -> int:
    """Plan the given weeks (1-based from `start`), replace unplanned slots there, return slots written."""
    strategy = await latest(tenant_id, "content_strategy")
    if not strategy:
        raise AgentError("no content strategy yet")
    trends = await latest(tenant_id, "trend_report")
    pillars = {p["name"]: p["share_pct"] for p in strategy["pillars"]}
    per_week = tenant_goals((await profile(tenant_id)).intake)["posts_per_week"]   # the client's own target
    with tenant_scope(tenant_id):
        async with get_db().reader() as sess:
            used = [t for (t,) in (await sess.execute(select(CalendarSlot.topic)))]

    written = 0
    for chunk in [weeks[i:i + 4] for i in range(0, len(weeks), 4)]:
        user = (f"Plan LinkedIn posts for weeks {chunk}. At least {per_week} posts per week on distinct days "
                f"(prefer Mon-Fri). Pillars and target share: {pillars}. Formats: {FORMATS}.\n"
                f"Strategy: {compact(strategy, 1800)}\nTrends: {compact(trends, 1500)}\n"
                f"Topics already used (don't repeat): {used[-60:]}")
        meta = {"weeks": chunk, "pillars": pillars, "per_week": per_week, "strategy": strategy, "trends": trends}
        plan, problems = None, []
        for attempt in range(3):
            prompt = user if not problems else user + "\n\nYour previous plan broke these rules, fix them:\n- " + "\n- ".join(problems)
            plan = (await structured("calendar_planner", "big",
                                     "You are a content calendar planner. Give every slot a specific topic and angle.",
                                     prompt, CalendarPlan, ctx, max_tokens=4000,
                                     metadata={**meta, "attempt": attempt, "problems": problems})).output
            problems = validate_calendar(plan, chunk, pillars, per_week)
            ctx.event("calendar_planner", "calendar_validation", weeks=chunk, attempt=attempt, problems=problems)
            if not problems:
                break
        if problems:
            raise AgentError(f"calendar still invalid after repairs: {problems[:3]}")
        assert plan is not None
        with tenant_scope(tenant_id):
            async with get_db().session() as sess:
                first = start + timedelta(weeks=min(chunk) - 1)
                last = start + timedelta(weeks=max(chunk))
                await sess.execute(delete(CalendarSlot).where(
                    CalendarSlot.status == "planned", CalendarSlot.publish_date >= first, CalendarSlot.publish_date < last))
                for sl in plan.slots:
                    sess.add(CalendarSlot(
                        week=sl.week, publish_date=start + timedelta(weeks=sl.week - 1, days=DAYS.index(sl.day)),
                        pillar=sl.pillar, format=sl.format, topic=sl.topic, angle=sl.angle, trend_ref=sl.trend_ref))
                    written += 1
        used += [sl.topic for sl in plan.slots]
    return written
