"""Long-horizon orchestration: DAG runs with durable node checkpoints.

* A run is a DAG of nodes persisted in run_nodes. Ready nodes (all deps done)
  execute concurrently through the FairScheduler.
* Checkpointing: a succeeded node is never re-executed; on restart, nodes left
  'running' are reset to 'pending' and the run resumes.
* Transient failures retry with backoff; exhausted nodes fail, their dependents
  are 'blocked', and the failure lands in the ops console (dead-letter view).
* Budget/kill-switch failures stop the run immediately: no retries.
* Human gates are nodes that park the run in 'waiting_human' until approved.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from app.agents.blackboard import latest, latest_meta, put_artifact
from app.agents.content.agents import draft_slot, reflect_on_feedback
from app.agents.leads.agents import draft_outreach, find_leads
from app.agents.research.agents import (competitor_analyst, ingest_website, market_analyst, provenance, trend_scout,
                                        voice_analyst)
from app.agents.runtime.loop import AgentContext
from app.agents.strategy.agents import calendar_planner, icp_builder, next_monday, strategist
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import CalendarSlot, KnowledgeDoc, LLMCall, Lead, Run, RunNode, Tenant
from app.db.session import get_db
from app.guardrails.input_guard import scan
from app.integrations.errors import IntegrationError
from app.llm.gateway import TenantPaused
from app.llm.types import Budget, BudgetExceeded, CallContext
from app.agents.orchestrator.scheduler import FairScheduler

if get_settings().fixtures_allowed:      # offline responders exist only outside production
    from app.fixtures import mocks  # noqa: F401

log = logging.getLogger(__name__)

DAGS: dict[str, dict[str, list[str]]] = {
    "discovery": {
        "ingest": [],
        "voice": ["ingest"],
        "market": ["ingest"],
        "competitors": ["ingest"],
        "trends": ["market"],
        "strategy": ["voice", "market", "competitors", "trends"],
        "icp": ["market", "competitors"],
        "calendar": ["strategy"],
        "strategy_review": ["calendar", "icp"],   # human gate: account manager signs off the brief
    },
    "weekly": {
        "reflect": [],                            # learn from last week's edits first
        "trends_refresh": [],
        "draft_week": ["reflect", "trends_refresh"],
        "calendar_extend": ["draft_week"],        # keep 8 weeks planned ahead
        "leads": [],
        "outreach": ["leads"],
        "kpi_snapshot": ["calendar_extend", "outreach"],
    },
}
HUMAN_GATES = {"strategy_review"}
TERMINAL = {"succeeded", "failed", "blocked", "skipped"}
MAX_ATTEMPTS = 2


@dataclass
class NodeCtx:
    tenant_id: str
    run_id: str
    key: str
    params: dict[str, Any]
    budget: Budget
    cancel: asyncio.Event
    agent: AgentContext = field(init=False)

    def __post_init__(self):
        self.agent = AgentContext(CallContext(tenant_id=self.tenant_id, run_id=self.run_id, node_key=self.key,
                                              budget=self.budget, cancel=self.cancel))


Handler = Callable[[NodeCtx], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(key: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[key] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Node handlers
# ---------------------------------------------------------------------------
@handler("ingest")
async def _ingest(n: NodeCtx) -> dict:
    with tenant_scope(n.tenant_id):
        async with get_db().reader() as s:
            docs = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.kind.in_(["founder_sample", "intake_note"])))).scalars().all()
    flagged = 0
    for d in docs:
        v = await scan(d.content, n.agent.call)
        if v.flagged:
            flagged += 1
            with tenant_scope(n.tenant_id):
                async with get_db().session() as s:
                    row = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.id == d.id))).scalars().one()
                    row.injection_score, row.quarantined = v.score, v.quarantined
    return {"docs_scanned": len(docs), "flagged": flagged, "website": await ingest_website(n.tenant_id, n.agent)}


async def _artifact(n: NodeCtx, kind: str, fn) -> dict:
    out = await fn(n.tenant_id, n.agent)
    data = {**out.model_dump(), "_provenance": provenance(n.agent)}
    art = await put_artifact(n.tenant_id, kind, data, n.run_id)
    return {"artifact": kind, "version": art.version}


@handler("voice")
async def _voice(n): return await _artifact(n, "voice_profile", voice_analyst)


@handler("market")
async def _market(n): return await _artifact(n, "market_report", market_analyst)


@handler("competitors")
async def _competitors(n): return await _artifact(n, "competitor_matrix", competitor_analyst)


@handler("trends")
async def _trends(n): return await _artifact(n, "trend_report", trend_scout)


@handler("strategy")
async def _strategy(n): return await _artifact(n, "content_strategy", strategist)


@handler("icp")
async def _icp(n): return await _artifact(n, "icp", icp_builder)


@handler("calendar")
async def _calendar(n: NodeCtx) -> dict:
    s = get_settings()
    start = date.fromisoformat(n.params["start_date"]) if n.params.get("start_date") else next_monday(date.today())
    written = await calendar_planner(n.tenant_id, n.agent, start, list(range(1, s.calendar_weeks + 1)))
    await put_artifact(n.tenant_id, "calendar_meta", {"start": start.isoformat(), "weeks": s.calendar_weeks}, n.run_id)
    return {"slots": written, "start": start.isoformat()}


@handler("trends_refresh")
async def _trends_refresh(n: NodeCtx) -> dict:
    meta = await latest_meta(n.tenant_id, "trend_report")
    if meta and datetime.now(timezone.utc) - meta.created_at.replace(tzinfo=timezone.utc) < timedelta(days=6):
        return {"skipped": "trend report is fresh", "version": meta.version}  # cost lever: don't redo fresh research
    return await _artifact(n, "trend_report", trend_scout)


async def week_to_draft(tenant_id: str) -> date | None:
    """The Monday of the earliest week that still has planned slots."""
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            first = (await s.execute(select(func.min(CalendarSlot.publish_date))
                                     .where(CalendarSlot.status == "planned"))).scalar()
    return first - timedelta(days=first.weekday()) if first else None


@handler("draft_week")
async def _draft_week(n: NodeCtx) -> dict:
    week = date.fromisoformat(n.params["week_start"]) if n.params.get("week_start") else await week_to_draft(n.tenant_id)
    if week is None:
        return {"drafted": 0, "note": "no planned slots"}
    n.params["week_start"] = week.isoformat()
    with tenant_scope(n.tenant_id):
        async with get_db().reader() as s:
            slots = (await s.execute(select(CalendarSlot.id).where(
                CalendarSlot.status == "planned", CalendarSlot.publish_date >= week,
                CalendarSlot.publish_date < week + timedelta(days=7)))).scalars().all()
    sem = asyncio.Semaphore(3)  # fan-out inside one node, bounded

    async def one(slot_id: str):
        async with sem:
            try:
                post = await draft_slot(n.tenant_id, slot_id, n.agent)
                if post.status == "blocked":      # one fresh attempt, told why the first was refused
                    why = "; ".join(v["rule"] for v in (post.guardrail_report or {}).get("violations", []))
                    n.agent.event("writer", "redraft_after_block", slot=slot_id, rules=why)
                    post = await draft_slot(n.tenant_id, slot_id, n.agent,
                                            note=f"The previous draft was blocked ({why}). Write a new post with no statistics or invented stories.")
                return post.status
            except (BudgetExceeded, TenantPaused):
                raise
            except Exception as e:  # one bad slot shouldn't sink the week
                n.agent.event("writer", "slot_failed", slot=slot_id, error=str(e)[:300])
                return "failed"

    statuses = await asyncio.gather(*[one(sid) for sid in slots])
    counts = {st: statuses.count(st) for st in set(statuses)}
    if statuses and counts.get("failed", 0) == len(statuses):
        raise RuntimeError("every slot in the week failed to draft")
    return {"week_start": week.isoformat(), "slots": len(slots), **counts}


@handler("calendar_extend")
async def _calendar_extend(n: NodeCtx) -> dict:
    s = get_settings()
    meta = await latest(n.tenant_id, "calendar_meta")
    if not meta:
        return {"skipped": "no calendar"}
    start = date.fromisoformat(meta["start"])
    week = date.fromisoformat(n.params["week_start"]) if n.params.get("week_start") else date.today()
    with tenant_scope(n.tenant_id):
        async with get_db().reader() as sess:
            horizon = (await sess.execute(select(func.max(CalendarSlot.publish_date)))).scalar() or start
    target_end = week + timedelta(weeks=s.calendar_weeks + 1)
    if horizon >= target_end - timedelta(days=7):
        return {"skipped": "8 weeks already planned", "horizon": horizon.isoformat()}
    first_missing = (horizon - start).days // 7 + 2
    last_needed = (target_end - start).days // 7
    weeks = list(range(first_missing, last_needed + 1))
    written = await calendar_planner(n.tenant_id, n.agent, start, weeks)
    return {"weeks_added": weeks, "slots": written}


@handler("leads")
async def _leads(n: NodeCtx) -> dict:
    week = date.fromisoformat(n.params["week_start"]) if n.params.get("week_start") else \
        (await week_to_draft(n.tenant_id) or next_monday(date.today()))
    n.params.setdefault("week_start", week.isoformat())
    return await find_leads(n.tenant_id, n.agent, week)


@handler("outreach")
async def _outreach(n: NodeCtx) -> dict:
    with tenant_scope(n.tenant_id):
        async with get_db().reader() as s:
            ids = (await s.execute(select(Lead.id).where(Lead.status == "qualified", Lead.email != ""))).scalars().all()
    sem = asyncio.Semaphore(3)

    async def one(lid):
        async with sem:
            try:
                return (await draft_outreach(n.tenant_id, lid, n.agent)).status
            except (BudgetExceeded, TenantPaused):
                raise
            except Exception as e:
                n.agent.event("outreach_writer", "lead_failed", lead=lid, error=str(e)[:300])
                return "failed"

    statuses = await asyncio.gather(*[one(i) for i in ids])
    return {"drafted": len(ids), **{st: statuses.count(st) for st in set(statuses)}}


@handler("reflect")
async def _reflect(n: NodeCtx) -> dict:
    return {"feedback_used": await reflect_on_feedback(n.tenant_id, n.agent)}


@handler("kpi_snapshot")
async def _kpi(n: NodeCtx) -> dict:
    from app.services.kpis import tenant_kpis
    k = await tenant_kpis(n.tenant_id)
    await put_artifact(n.tenant_id, "kpi_snapshot", k, n.run_id)
    return {"posts_ready": k["posts_pending"] + k["posts_approved_total"], "leads_this_week": k["leads_latest_week"]}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, scheduler: FairScheduler | None = None):
        s = get_settings()
        self.scheduler = scheduler or FairScheduler(s.global_max_concurrency, s.per_tenant_max_concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}

    async def create_run(self, tenant_id: str, kind: str, params: dict | None = None,
                         actor: str = "system:scheduler", budget_usd: float | None = None) -> str:
        dag = DAGS[kind]
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                run = Run(kind=kind, params=params or {}, budget_usd=budget_usd or get_settings().default_run_budget_usd)
                s.add(run)
                await s.flush()
                for key, deps in dag.items():
                    s.add(RunNode(run_id=run.id, key=key, deps=deps))
                audit(s, actor, "run.created", "run", run.id, kind=kind)
                return run.id

    def start(self, tenant_id: str, run_id: str) -> asyncio.Task:
        task = self._tasks.get(run_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.execute(tenant_id, run_id))
        self._tasks[run_id] = task
        return task

    def cancel(self, run_id: str) -> None:
        if run_id in self._cancels:
            self._cancels[run_id].set()

    async def execute(self, tenant_id: str, run_id: str) -> str:
        cancel = self._cancels.setdefault(run_id, asyncio.Event())
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().one()
                if run.status in ("succeeded", "failed", "cancelled"):
                    return run.status
                run.status = "running"
                for node in (await s.execute(select(RunNode).where(RunNode.run_id == run_id))).scalars():
                    if node.status == "running":  # crashed mid-node: resume from checkpoint
                        node.status = "pending"
                params, budget = dict(run.params), Budget(max_usd=max(0.0, run.budget_usd - run.cost_usd))
        async with get_db().reader() as s:
            tenant = await s.get(Tenant, tenant_id)
        if tenant is None or tenant.kill_switch:
            return await self._finish(tenant_id, run_id, "cancelled", "tenant kill switch is on")

        while True:
            nodes = await self._nodes(tenant_id, run_id)
            by_key = {n.key: n for n in nodes}
            for n in nodes:  # propagate failures downstream
                if n.status == "pending" and any(by_key[d].status in ("failed", "blocked") for d in n.deps):
                    await self._set_node(tenant_id, n.id, status="blocked", error="upstream failed")
                    n.status = "blocked"
            ready = [n for n in nodes if n.status == "pending"
                     and all(by_key[d].status in ("succeeded", "skipped") for d in n.deps)]
            if cancel.is_set():
                return await self._finish(tenant_id, run_id, "cancelled", "cancelled")
            if not ready:
                if any(n.status == "waiting_human" for n in nodes):
                    await self._set_run(tenant_id, run_id, status="waiting_human")
                    return "waiting_human"
                break
            await asyncio.gather(*[self._run_node(tenant_id, run_id, n, params, budget, cancel) for n in ready])

        nodes = await self._nodes(tenant_id, run_id)
        failed = [n.key for n in nodes if n.status in ("failed", "blocked")]
        return await self._finish(tenant_id, run_id, "failed" if failed else "succeeded",
                                  f"failed nodes: {failed}" if failed else "")

    async def _run_node(self, tenant_id: str, run_id: str, node: RunNode, params: dict, budget: Budget,
                        cancel: asyncio.Event) -> None:
        if node.key in HUMAN_GATES:
            if params.get("auto_approve_gates"):
                await self._set_node(tenant_id, node.id, status="succeeded", output={"approved_by": "auto (params)"},
                                     finished_at=_now())
            else:
                await self._set_node(tenant_id, node.id, status="waiting_human")
            return
        fn = HANDLERS[node.key]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._set_node(tenant_id, node.id, status="running", attempts=node.attempts + attempt,
                                 queued_at=_now(), error="")
            nctx = NodeCtx(tenant_id, run_id, node.key, params, budget, cancel)

            async def work():
                await self._set_node(tenant_id, node.id, started_at=_now())
                return await fn(nctx)

            try:
                out = await self.scheduler.run(tenant_id, work)
                await nctx.agent.flush()
                await self._set_node(tenant_id, node.id, status="succeeded", output=out, finished_at=_now())
                return
            except (BudgetExceeded, TenantPaused) as e:
                await nctx.agent.flush()
                cancel.set()  # no point continuing other nodes
                await self._set_node(tenant_id, node.id, status="failed", error=f"stopped: {e}", finished_at=_now())
                return
            except Exception as e:  # noqa: BLE001 - recorded on the node
                await nctx.agent.flush()
                log.warning("node %s/%s attempt %d failed: %s", run_id, node.key, attempt, e)
                if isinstance(e, IntegrationError) and not e.retryable:
                    # Missing key, bad credentials, exhausted quota: another attempt cannot help. Say so and stop.
                    await self._set_node(tenant_id, node.id, status="failed", error=str(e)[:2000],
                                         output={"integration": e.as_dict()}, finished_at=_now())
                    return
                if attempt == MAX_ATTEMPTS:
                    if isinstance(e, IntegrationError):
                        await self._set_node(tenant_id, node.id, output={"integration": e.as_dict()})
                    await self._set_node(tenant_id, node.id, status="failed", error=f"{type(e).__name__}: {e}"[:2000],
                                         finished_at=_now())
                    return
                await asyncio.sleep(0.5 * attempt)

    async def approve_gate(self, tenant_id: str, run_id: str, key: str, actor: str) -> None:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                node = (await s.execute(select(RunNode).where(RunNode.run_id == run_id, RunNode.key == key))).scalars().one()
                if node.status != "waiting_human":
                    raise ValueError(f"node {key} is not waiting for approval")
                node.status, node.output, node.finished_at = "succeeded", {"approved_by": actor}, _now()
                audit(s, actor, "run.gate_approved", "run", run_id, node=key)
        self.start(tenant_id, run_id)

    async def rerun_from(self, tenant_id: str, run_id: str, key: str, actor: str) -> None:
        """Invalidate a node and everything downstream of it, then resume."""
        dag = None
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().one()
                dag = DAGS[run.kind]
                stale, frontier = {key}, [key]
                while frontier:
                    k = frontier.pop()
                    for child, deps in dag.items():
                        if k in deps and child not in stale:
                            stale.add(child)
                            frontier.append(child)
                for node in (await s.execute(select(RunNode).where(RunNode.run_id == run_id))).scalars():
                    if node.key in stale:
                        node.status, node.output, node.error = "pending", {}, ""
                run.status, run.finished_at = "pending", None
                audit(s, actor, "run.rerun_from", "run", run_id, node=key, stale=sorted(stale))
        self.start(tenant_id, run_id)

    async def resume_incomplete(self) -> int:
        """On boot: restart runs that were mid-flight when the process died."""
        from app.core.tenancy import system_scope
        with system_scope("orchestrator: resume runs after restart"):
            async with get_db().reader() as s:
                rows = (await s.execute(select(Run.tenant_id, Run.id).where(Run.status.in_(["running", "pending"])))).all()
        for tid, rid in rows:
            self.start(tid, rid)
        return len(rows)

    # -- persistence helpers -----------------------------------------------------
    async def _nodes(self, tenant_id: str, run_id: str) -> list[RunNode]:
        with tenant_scope(tenant_id):
            async with get_db().reader() as s:
                return list((await s.execute(select(RunNode).where(RunNode.run_id == run_id))).scalars().all())

    async def _set_node(self, tenant_id: str, node_id: str, **fields) -> None:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                node = (await s.execute(select(RunNode).where(RunNode.id == node_id))).scalars().one()
                for k, v in fields.items():
                    setattr(node, k, v)

    async def _set_run(self, tenant_id: str, run_id: str, **fields) -> None:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().one()
                for k, v in fields.items():
                    setattr(run, k, v)

    async def _finish(self, tenant_id: str, run_id: str, status: str, error: str) -> str:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                cost = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                        .where(LLMCall.run_id == run_id))).scalar_one()
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalars().one()
                run.status, run.error, run.cost_usd, run.finished_at = status, error, float(cost), _now()
                audit(s, "system:orchestrator", f"run.{status}", "run", run_id, cost_usd=round(float(cost), 5))
        self._cancels.pop(run_id, None)
        return status


def _now() -> datetime:
    return datetime.now(timezone.utc)


_orch: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch


def set_orchestrator(o: Orchestrator) -> None:
    global _orch
    _orch = o
