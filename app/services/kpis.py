"""Business KPIs per client: the numbers the agency is judged on."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.models import CalendarSlot, GuardrailIncident, Lead, LLMCall, OutreachMessage, Post
from app.db.session import get_db


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def tenant_kpis(tenant_id: str) -> dict:
    st = get_settings()
    today = date.today()
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            posts = (await s.execute(select(Post.status, Post.critic_score, Post.edit_distance, Post.slot_id,
                                            Post.rounds))).all()
            slot_dates = dict((await s.execute(select(CalendarSlot.id, CalendarSlot.publish_date))).all())
            slots = (await s.execute(select(CalendarSlot.publish_date, CalendarSlot.status))).all()
            leads = (await s.execute(select(Lead.week_of, Lead.fit_score))).all()
            msgs = (await s.execute(select(OutreachMessage.status, OutreachMessage.edit_distance))).all()
            cost_month = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                          .where(LLMCall.created_at >= month_start))).scalar_one()
            cost_total = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)))).scalar_one()
            incidents = (await s.execute(select(GuardrailIncident.layer, func.count(GuardrailIncident.id))
                                         .group_by(GuardrailIncident.layer))).all()

    status = Counter(p.status for p in posts)
    approved_like = {"approved", "scheduled", "published"}
    decided = [p for p in posts if p.status in approved_like | {"rejected", "changes_requested"}]
    approved = [p for p in posts if p.status in approved_like]
    untouched = [p for p in approved if (p.edit_distance or 0) < 0.05]

    # Posts ready per calendar week (approved or awaiting approval), to compare with the >=5/week target.
    per_week: Counter = Counter()
    for p in posts:
        if p.status in approved_like | {"pending_approval"} and p.slot_id in slot_dates:
            per_week[_monday(slot_dates[p.slot_id]).isoformat()] += 1
    weeks_planned = Counter(_monday(d).isoformat() for d, _ in slots if d >= _monday(today))
    horizon = sum(1 for w, n in weeks_planned.items() if n >= st.posts_per_week_target)

    lead_weeks = Counter(w.isoformat() for w, _ in leads)
    latest_week = max(lead_weeks) if lead_weeks else None
    eds = [p.edit_distance for p in approved if p.edit_distance is not None]
    return {
        "posts_pending": status.get("pending_approval", 0),
        "posts_blocked": status.get("blocked", 0),
        "posts_approved_total": len(approved),
        "posts_per_week": dict(sorted(per_week.items())),
        "calendar_weeks_planned": horizon,
        "calendar_slots_future": sum(weeks_planned.values()),
        "first_pass_approval_rate": round(len(untouched) / len(decided), 3) if decided else None,
        "approval_rate": round(len(approved) / len(decided), 3) if decided else None,
        "mean_edit_distance": round(sum(eds) / len(eds), 3) if eds else None,
        "avg_critic_score": round(sum(p.critic_score for p in posts) / len(posts), 2) if posts else None,
        "avg_critic_rounds": round(sum(p.rounds for p in posts) / len(posts), 2) if posts else None,
        "leads_per_week": dict(sorted(lead_weeks.items())),
        "leads_latest_week": lead_weeks.get(latest_week, 0) if latest_week else 0,
        "avg_fit_score": round(sum(f for _, f in leads) / len(leads), 1) if leads else None,
        "outreach_pending": sum(1 for m in msgs if m.status == "pending_approval"),
        "outreach_sent": sum(1 for m in msgs if m.status in ("sent", "replied")),
        "cost_month_usd": round(float(cost_month), 4),
        "cost_total_usd": round(float(cost_total), 4),
        "cost_per_ready_post": round(float(cost_total) / max(1, len(approved) + status.get("pending_approval", 0)), 4),
        "cost_per_lead": round(float(cost_total) / max(1, len(leads)), 4),
        "guardrail_incidents": dict(incidents),
        "targets": {"posts_per_week": st.posts_per_week_target, "calendar_weeks": st.calendar_weeks,
                    "leads_per_week": [st.leads_per_week_min, st.leads_per_week_max]},
    }
