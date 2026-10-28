"""Read models for the UI. Everything per client runs under that client's tenant scope."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select

from app.core.tenancy import tenant_scope
from app.db.models import (Artifact, CalendarSlot, ExternalCall, Lead, LeadRejection, LLMCall, OutreachMessage, Post, Run,
                           RunNode, Tenant, TraceEvent)
from app.db.session import get_db


async def _all(tenant_id: str, stmt) -> list:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return list((await s.execute(stmt)).scalars().all())


async def posts(tenant_id: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            q = select(Post, CalendarSlot).outerjoin(CalendarSlot, CalendarSlot.id == Post.slot_id)
            if statuses:
                q = q.where(Post.status.in_(statuses))
            rows = (await s.execute(q.order_by(CalendarSlot.publish_date, Post.created_at))).all()
    return [{"post": p, "slot": sl} for p, sl in rows]


async def calendar(tenant_id: str) -> list[dict[str, Any]]:
    slots = await _all(tenant_id, select(CalendarSlot).order_by(CalendarSlot.publish_date))
    weeks: dict[str, list] = defaultdict(list)
    for sl in slots:
        monday = sl.publish_date.toordinal() - sl.publish_date.weekday()
        weeks[datetime.fromordinal(monday).date().isoformat()].append(sl)
    return [{"week": w, "slots": v} for w, v in sorted(weeks.items())]


async def leads(tenant_id: str) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            rows = (await s.execute(select(Lead, OutreachMessage).outerjoin(OutreachMessage, OutreachMessage.lead_id == Lead.id)
                                    .order_by(Lead.week_of.desc(), Lead.fit_score.desc()))).all()
    return [{"lead": l, "msg": m} for l, m in rows]


async def outreach(tenant_id: str, statuses: list[str]) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            rows = (await s.execute(select(OutreachMessage, Lead).join(Lead, Lead.id == OutreachMessage.lead_id)
                                    .where(OutreachMessage.status.in_(statuses))
                                    .order_by(Lead.fit_score.desc()))).all()
    return [{"msg": m, "lead": l} for m, l in rows]


async def rejections(tenant_id: str, limit: int = 200) -> list[LeadRejection]:
    return await _all(tenant_id, select(LeadRejection).order_by(LeadRejection.created_at.desc()).limit(limit))


async def artifacts(tenant_id: str) -> dict[str, Artifact]:
    arts = await _all(tenant_id, select(Artifact).order_by(Artifact.version))
    return {a.kind: a for a in arts}  # latest version wins


async def runs(tenant_id: str, limit: int = 30) -> list[Run]:
    return await _all(tenant_id, select(Run).order_by(Run.created_at.desc()).limit(limit))


async def run_detail(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().first()
            if run is None:
                return None
            nodes = (await s.execute(select(RunNode).where(RunNode.run_id == run_id))).scalars().all()
            calls = (await s.execute(select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.created_at))).scalars().all()
            events = (await s.execute(select(TraceEvent).where(TraceEvent.run_id == run_id)
                                      .order_by(TraceEvent.created_at).limit(400))).scalars().all()
    by_node: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "tokens": 0})
    for c in calls:
        agg = by_node[c.node_key]
        agg["calls"] += 1
        agg["cost"] += c.cost_usd
        agg["tokens"] += c.prompt_tokens + c.completion_tokens
    from app.agents.orchestrator.engine import DAGS
    order = list(DAGS[run.kind])
    nodes = sorted(nodes, key=lambda n: order.index(n.key) if n.key in order else 99)
    return {"run": run, "nodes": nodes, "calls": calls, "events": events, "by_node": dict(by_node)}


async def cost_breakdown(tenant_id: str) -> dict[str, Any]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            by_agent = (await s.execute(select(LLMCall.agent, LLMCall.model, func.count(LLMCall.id),
                                               func.sum(LLMCall.prompt_tokens), func.sum(LLMCall.completion_tokens),
                                               func.sum(LLMCall.cost_usd), func.sum(case((LLMCall.cached, 1), else_=0)),
                                               func.sum(LLMCall.retries), func.avg(LLMCall.latency_ms))
                                        .group_by(LLMCall.agent, LLMCall.model)
                                        .order_by(func.sum(LLMCall.cost_usd).desc()))).all()
            external = (await s.execute(select(ExternalCall.provider, ExternalCall.operation, func.count(ExternalCall.id),
                                               func.sum(ExternalCall.units), func.sum(ExternalCall.cost_usd))
                                        .group_by(ExternalCall.provider, ExternalCall.operation))).all()
    return {"rows": [dict(agent=a, model=m, calls=n, prompt=int(pt or 0), completion=int(ct or 0), cost=float(c or 0),
                          cached=int(ca or 0), retries=int(r or 0), latency=int(l or 0))
                     for a, m, n, pt, ct, c, ca, r, l in by_agent],
            "external": [dict(provider=pv, operation=op, calls=n, units=float(u or 0), cost=float(c or 0))
                         for pv, op, n, u, c in external]}


async def clients_overview(tenants: list[Tenant]) -> list[dict[str, Any]]:
    """One row per client of the signed-in account: its KPIs and its latest run."""
    from app.services.kpis import tenant_kpis
    out = []
    for t in tenants:
        last = (await _all(t.id, select(Run).order_by(Run.created_at.desc()).limit(1)) or [None])[0]
        out.append({"tenant": t, "kpis": await tenant_kpis(t.id), "last_run": last})
    return out


async def account_spend(p) -> dict[str, Any]:
    """The operator's own spend this month and in total, per client: AI (from the ledger) and search/contact credits.
    Prices are the providers' list prices; free tiers bill nothing."""
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with get_db().reader() as s:
        tenants = (await s.execute(select(Tenant).where(Tenant.owner_user_id == p.user_id).order_by(Tenant.name))).scalars().all()
    rows = []
    for t in tenants:
        with tenant_scope(t.id):
            async with get_db().reader() as s:
                ai_m = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                        .where(LLMCall.created_at >= month_start))).scalar_one()
                ai_all = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)))).scalar_one()
                ext_m = (await s.execute(select(func.coalesce(func.sum(ExternalCall.cost_usd), 0.0))
                                         .where(ExternalCall.created_at >= month_start))).scalar_one()
                calls = (await s.execute(select(func.count(LLMCall.id)).where(LLMCall.created_at >= month_start))).scalar_one()
        rows.append({"tenant": t, "ai_month": float(ai_m), "ai_total": float(ai_all), "external_month": float(ext_m),
                     "calls_month": int(calls)})
    return {"rows": rows, "ai_month": sum(r["ai_month"] for r in rows), "external_month": sum(r["external_month"] for r in rows),
            "month": month_start.strftime("%B %Y")}


async def post(tenant_id: str, post_id: str) -> dict[str, Any] | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            row = (await s.execute(select(Post, CalendarSlot).outerjoin(CalendarSlot, CalendarSlot.id == Post.slot_id)
                                   .where(Post.id == post_id))).first()
    return {"post": row[0], "slot": row[1]} if row else None


async def run_usage(tenant_id: str, run_id: str) -> dict[str, Any]:
    """Live AI usage for one run: tokens, calls, cost and elapsed seconds."""
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            row = (await s.execute(select(func.count(LLMCall.id), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
                                          func.coalesce(func.sum(LLMCall.completion_tokens), 0),
                                          func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                                          func.coalesce(func.sum(LLMCall.retries), 0),
                                          func.coalesce(func.sum(LLMCall.pace_wait_ms), 0),
                                          func.coalesce(func.sum(LLMCall.latency_ms), 0))
                                   .where(LLMCall.run_id == run_id))).one()
            ext = dict((await s.execute(select(ExternalCall.provider, func.count(ExternalCall.id))
                                        .where(ExternalCall.run_id == run_id).group_by(ExternalCall.provider))).all())
            ext_cost = (await s.execute(select(func.coalesce(func.sum(ExternalCall.cost_usd), 0.0))
                                        .where(ExternalCall.run_id == run_id))).scalar_one()
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().first()
    end = (run.finished_at if run and run.finished_at else datetime.now(timezone.utc))
    start = run.created_at if run else end
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return {"calls": int(row[0]), "tokens_in": int(row[1]), "tokens_out": int(row[2]), "cost": float(row[3]),
            "retries": int(row[4]), "pace_wait_s": int(row[5]) // 1000, "model_s": int(row[6]) // 1000,
            "searches": int(ext.get("tavily", 0) + ext.get("gemini_search", 0)), "contact_lookups": int(ext.get("hunter", 0)),
            "external_cost": float(ext_cost), "elapsed_s": max(0, int((end - start).total_seconds()))}
