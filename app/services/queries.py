"""Read models shared by the JSON API and the HTML UI. All tenant-scoped."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select

from app.core.tenancy import system_scope, tenant_scope
from app.db.models import (Artifact, AuditLog, CalendarSlot, GuardrailIncident, Lead, LeadRejection, LLMCall,
                           OutreachMessage, Post, Run, RunNode, Tenant, TraceEvent)
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


async def incidents(tenant_id: str, limit: int = 100) -> list[GuardrailIncident]:
    return await _all(tenant_id, select(GuardrailIncident).order_by(GuardrailIncident.created_at.desc()).limit(limit))


async def audit_log(tenant_id: str, limit: int = 100) -> list[AuditLog]:
    return await _all(tenant_id, select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))


async def cost_breakdown(tenant_id: str) -> dict[str, Any]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            by_agent = (await s.execute(select(LLMCall.agent, LLMCall.model, func.count(LLMCall.id),
                                               func.sum(LLMCall.prompt_tokens), func.sum(LLMCall.completion_tokens),
                                               func.sum(LLMCall.cost_usd), func.sum(case((LLMCall.cached, 1), else_=0)),
                                               func.sum(LLMCall.retries), func.avg(LLMCall.latency_ms))
                                        .group_by(LLMCall.agent, LLMCall.model)
                                        .order_by(func.sum(LLMCall.cost_usd).desc()))).all()
    return {"rows": [dict(agent=a, model=m, calls=n, prompt=int(pt or 0), completion=int(ct or 0), cost=float(c or 0),
                          cached=int(ca or 0), retries=int(r or 0), latency=int(l or 0))
                     for a, m, n, pt, ct, c, ca, r, l in by_agent]}


async def portfolio(tenant_ids: list[str]) -> list[dict[str, Any]]:
    """Ops overview: one row per client. Cross-tenant read, scoped to what the caller may see."""
    from app.services.kpis import tenant_kpis
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    async with get_db().reader() as s:
        tenants = {t.id: t for t in (await s.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars()}
    with system_scope("ops portfolio overview"):
        async with get_db().reader() as s:
            last_runs = {}
            for r in (await s.execute(select(Run).where(Run.tenant_id.in_(tenant_ids))
                                      .order_by(Run.created_at.desc()))).scalars():
                last_runs.setdefault(r.tenant_id, r)
    for tid in tenant_ids:
        t = tenants.get(tid)
        if t is None:
            continue
        k = await tenant_kpis(tid)
        out.append({"tenant": t, "kpis": k, "last_run": last_runs.get(tid), "month_start": month_start})
    return out


async def run_usage(tenant_id: str, run_id: str) -> dict[str, Any]:
    """Live AI usage for one run: tokens, calls, cost and elapsed seconds. Safe to show to the founder."""
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            row = (await s.execute(select(func.count(LLMCall.id), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
                                          func.coalesce(func.sum(LLMCall.completion_tokens), 0),
                                          func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                                          func.coalesce(func.sum(LLMCall.retries), 0))
                                   .where(LLMCall.run_id == run_id))).one()
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().first()
    end = (run.finished_at if run and run.finished_at else datetime.now(timezone.utc))
    start = run.created_at if run else end
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return {"calls": int(row[0]), "tokens_in": int(row[1]), "tokens_out": int(row[2]), "cost": float(row[3]),
            "retries": int(row[4]), "elapsed_s": max(0, int((end - start).total_seconds()))}
