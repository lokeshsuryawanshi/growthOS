"""Honest progress for a run: what each step is doing, how long it has taken, and a time-left estimate.

The estimate is the remaining critical path through the run's DAG, using the median duration of each step in this
deployment's recent successful runs (any client: only durations are read), or the live measurements below until
there is history. It is labelled as an estimate in the UI.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.agents.orchestrator.engine import DAGS
from app.core.config import get_settings
from app.core.tenancy import system_scope, tenant_scope
from app.db.models import Lead, Post, RunNode
from app.db.session import get_db

# Seconds per step measured on real providers (Groq + Gemini free tiers, 2026-10-27); used until there is history.
DEFAULT_STEP_S = {"ingest": 2, "voice": 3, "market": 20, "competitors": 30, "trends": 20, "strategy": 5, "icp": 3,
                  "calendar": 35, "strategy_review": 0, "reflect": 1, "trends_refresh": 1, "draft_week": 150,
                  "calendar_extend": 30, "kpi_snapshot": 1, "leads": 60, "outreach": 40}
_cache: dict = {}


async def typical_durations(max_age_s: float = 300) -> dict[str, float]:
    if _cache and time.monotonic() - _cache["at"] < max_age_s:
        return _cache["d"]
    with system_scope("progress estimate: median step durations (timings only, no content)"):
        async with get_db().reader() as s:
            rows = (await s.execute(select(RunNode.key, RunNode.started_at, RunNode.finished_at)
                                    .where(RunNode.status == "succeeded", RunNode.started_at.is_not(None),
                                           RunNode.finished_at.is_not(None))
                                    .order_by(RunNode.finished_at.desc()).limit(400))).all()
    by_key: dict[str, list[float]] = {}
    for key, a, b in rows:
        by_key.setdefault(key, []).append((b - a).total_seconds())
    d = {**DEFAULT_STEP_S, **{k: statistics.median(v) for k, v in by_key.items() if len(v) >= 2}}
    _cache.update(at=time.monotonic(), d=d)
    return d


def _utc(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


async def run_progress(tenant_id: str, run, nodes: list) -> dict:
    """Per-step timing, a remaining-time estimate, and live counts for the long steps."""
    now = datetime.now(timezone.utc)
    typical = await typical_durations()
    dag = DAGS.get(run.kind, {})
    by_key = {n.key: n for n in nodes}
    finish: dict[str, float] = {}           # seconds from now until each step should be done

    def eta(key: str) -> float:
        if key in finish:
            return finish[key]
        n = by_key.get(key)
        deps_done = max([eta(d) for d in dag.get(key, [])] or [0.0])
        if n is None or n.status in ("succeeded", "skipped", "failed", "blocked", "waiting_human"):
            finish[key] = 0.0 if (n is None or n.status != "waiting_human") else deps_done
        elif n.status == "running" and n.started_at:
            spent = (now - _utc(n.started_at)).total_seconds()
            finish[key] = max(5.0, typical.get(key, 30) - spent)
        else:
            finish[key] = deps_done + typical.get(key, 30)
        return finish[key]

    steps = []
    for key in dag:
        n = by_key.get(key)
        if n is None:
            continue
        started, ended = _utc(n.started_at), _utc(n.finished_at)
        steps.append({"key": key, "status": n.status,
                      "seconds": int(((ended or now) - started).total_seconds()) if started else None,
                      "typical": int(typical.get(key, 30))})
    active = run.status in ("pending", "running")
    remaining = max([eta(k) for k in dag] or [0.0]) if active else 0.0
    live: dict = {}
    if active and by_key.get("draft_week") and by_key["draft_week"].status == "running":
        bar = get_settings().critic_pass_score
        with tenant_scope(tenant_id):
            async with get_db().reader() as s:
                ready = (await s.execute(select(func.count(Post.id)).where(
                    Post.run_id == run.id, Post.status == "pending_approval", Post.critic_score >= bar))).scalar_one()
        live["posts_ready"] = int(ready)
    if active and by_key.get("leads") and by_key["leads"].status in ("running", "succeeded"):
        with tenant_scope(tenant_id):
            async with get_db().reader() as s:
                live["leads_found"] = int((await s.execute(select(func.count(Lead.id)).where(Lead.run_id == run.id))).scalar_one())
    return {"steps": steps, "eta_s": int(remaining), "live": live}
