"""Strategy agents: content strategy, ICP, and the 8-week calendar.

The calendar planner is checked against business rules in code (>= N posts on
distinct days each week, known pillars, pillar mix, unique topics). Violations
are fed back for a repair round: the model proposes, deterministic code disposes.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
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


def plain(text: str) -> str:
    """Unicode look-alikes to plain text: models copy names back with ordinary hyphens and spaces (a live run named a
    pillar with a non-breaking hyphen and every calendar slot then failed validation)."""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)
    t = t.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", t).strip()


def canon(text: str) -> str:
    return plain(text).casefold()


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
                           user, ContentStrategy, ctx, max_tokens=1800, reasoning="medium",
                           metadata={"intake": intake, **inputs})
    out = res.output
    for p in out.pillars:
        p.name = plain(p.name)
    return out


async def icp_builder(tenant_id: str, ctx: AgentContext) -> ICP:
    intake = (await profile(tenant_id)).intake
    market, comp = await latest(tenant_id, "market_report"), await latest(tenant_id, "competitor_matrix")
    user = (f"Client: {intake.get('company_name')}: {intake.get('company')}\nOfferings: {intake.get('offerings', [])}\n"
            f"Stated target customers: {intake.get('target_customers')}\nMarket: {compact(market, 1500)}\n"
            f"Competitors: {compact(comp, 800)}")
    res = await structured("icp_builder", "big",
                           "Define the ideal customer profile for outbound prospecting: 1-3 segments with job titles, "
                           "industries, company size and geography, their pains and buying triggers. Exclude the client's "
                           "competitors and their staff.", user, ICP, ctx, max_tokens=1300, metadata={"intake": intake})
    return res.output


def next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def validate_calendar(plan: CalendarPlan, weeks: list[int], pillars: dict[str, int], per_week: int,
                      used: list[str] | None = None) -> list[str]:
    """Business rules for a planned chunk. Pillar names are matched after normalising look-alike characters and case,
    and slots are rewritten to the canonical name, so a stray Unicode hyphen is not a reason to re-plan."""
    problems = []
    by_name = {canon(n): n for n in pillars}
    by_week: dict[int, list] = {w: [] for w in weeks}
    for s in plan.slots:
        if s.week not in by_week:
            problems.append(f"slot in week {s.week} is outside requested weeks {weeks}")
            continue
        by_week[s.week].append(s)
        if canon(s.pillar) in by_name:
            s.pillar = by_name[canon(s.pillar)]
        else:
            problems.append(f"unknown pillar '{s.pillar}' (use exactly one of {list(pillars)})")
        if s.day not in DAYS:
            problems.append(f"bad day '{s.day}'")
    for w, slots in by_week.items():
        days = {s.day for s in slots}
        if len(days) < per_week:
            problems.append(f"week {w} has posts on {len(days)} distinct days; need at least {per_week}")
    topics = Counter(canon(s.topic) for s in plan.slots)
    dupes = [t for t, n in topics.items() if n > 1]
    if dupes:
        problems.append(f"duplicate topics: {dupes[:3]}")
    clash = sorted({canon(s.topic) for s in plan.slots} & {canon(t) for t in used or []})
    if clash:
        problems.append(f"topics already used elsewhere in the calendar: {clash[:3]}")
    total = max(1, len(plan.slots))
    for name, share in pillars.items():
        actual = 100 * sum(1 for s in plan.slots if s.pillar == name) / total
        if abs(actual - share) > 20:
            problems.append(f"pillar '{name}' is {actual:.0f}% of slots, target {share}%")
    return problems


async def _plan_chunk(ctx: AgentContext, chunk: list[int], pillars: dict[str, int], per_week: int, strategy: dict,
                      trends: dict | None, used: list[str]) -> CalendarPlan:
    user = (f"Plan LinkedIn posts for weeks {chunk}. At least {per_week} posts per week on distinct days "
            f"(prefer Mon-Fri). Pillars and target share (use these names exactly): {pillars}. Formats: {FORMATS}.\n"
            f"Strategy: {compact(strategy, 1800)}\nTrends: {compact(trends, 1500)}\n"
            f"Topics already used (don't repeat): {used[-60:]}")
    meta = {"weeks": chunk, "pillars": pillars, "per_week": per_week, "strategy": strategy, "trends": trends}
    plan, problems = None, []
    for attempt in range(3):
        prompt = user if not problems else user + "\n\nYour previous plan broke these rules, fix them:\n- " + "\n- ".join(problems)
        plan = (await structured("calendar_planner", "big",
                                 "You are a content calendar planner. Give every slot a specific topic and angle.",
                                 prompt, CalendarPlan, ctx, max_tokens=2400,
                                 metadata={**meta, "attempt": attempt, "problems": problems})).output
        problems = validate_calendar(plan, chunk, pillars, per_week, used)
        ctx.event("calendar_planner", "calendar_validation", weeks=chunk, attempt=attempt, problems=problems)
        if not problems:
            return plan
    raise AgentError(f"calendar still invalid after repairs: {problems[:3]}")


async def calendar_planner(tenant_id: str, ctx: AgentContext, start: date, weeks: list[int]) -> int:
    """Plan the given weeks (1-based from `start`), replace unplanned slots there, return slots written.

    Four-week chunks are planned concurrently (limits are per model, so they can land on different models); if two
    chunks happen to pick the same topic, the later chunk is re-planned against the earlier one's topics."""
    strategy = await latest(tenant_id, "content_strategy")
    if not strategy:
        raise AgentError("no content strategy yet")
    trends = await latest(tenant_id, "trend_report")
    pillars = {plain(p["name"]): p["share_pct"] for p in strategy["pillars"]}
    per_week = tenant_goals((await profile(tenant_id)).intake)["posts_per_week"]   # the client's own target
    lo, hi = start + timedelta(weeks=min(weeks) - 1), start + timedelta(weeks=max(weeks))
    with tenant_scope(tenant_id):
        async with get_db().reader() as sess:
            # Topics elsewhere in the calendar; planned slots inside the weeks being re-planned are replaced, not "used".
            used = [t for (t, st, d) in await sess.execute(select(CalendarSlot.topic, CalendarSlot.status,
                                                                     CalendarSlot.publish_date))
                    if not (st == "planned" and lo <= d < hi)]

    chunks = [weeks[i:i + 4] for i in range(0, len(weeks), 4)]
    plans = list(await asyncio.gather(*[_plan_chunk(ctx, c, pillars, per_week, strategy, trends, used) for c in chunks]))
    taken = list(used)
    for i, plan in enumerate(plans):
        mine = {canon(sl.topic) for sl in plan.slots}
        if i and mine & {canon(t) for t in taken}:
            ctx.event("calendar_planner", "cross_chunk_duplicates", weeks=chunks[i])
            plans[i] = plan = await _plan_chunk(ctx, chunks[i], pillars, per_week, strategy, trends, taken)
        taken += [sl.topic for sl in plan.slots]

    written = 0
    for chunk, plan in zip(chunks, plans):
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
    return written
