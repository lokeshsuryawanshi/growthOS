"""The founder's 'Suggest changes' on the strategy: additions, edits or removals before approving the revised version.

A suggestion re-runs only the affected steps of the research run (strategy and calendar, and/or the ideal customer
profile) with the founder's note, keeps every other checkpoint, and parks the run at the approval gate again.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.orchestrator.engine import get_orchestrator
from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Feedback, Run
from app.db.session import get_db
from app.services.accounts import AccountError
from app.services.runs import preflight

PARTS = {"strategy": "Content strategy and calendar", "icp": "Ideal customer profile"}


class SuggestionRefused(ValueError):
    """Not possible right now (409): no research yet, or a run is in progress."""


async def suggest_changes(p: Principal, tenant_id: str, note: str, parts: list[str]) -> str:
    authorize(p, "strategy:review", tenant_id)
    note = (note or "").strip()
    if not 10 <= len(note) <= 2000:
        raise AccountError("Describe the change in a sentence or two (10 to 2,000 characters).")
    parts = [x for x in dict.fromkeys(parts or []) if x in PARTS] or ["strategy"]
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            runs = (await s.execute(select(Run).order_by(Run.created_at.desc()))).scalars().all()
    busy = next((r for r in runs if r.status in ("pending", "running")), None)
    if busy:
        raise SuggestionRefused(f"A {busy.kind} run is in progress. Suggest changes when it has finished.")
    disc = next((r for r in runs if r.kind == "discovery" and r.status in
                 ("waiting_human", "succeeded", "succeeded_with_shortfall")), None)
    if disc is None:
        raise SuggestionRefused("Run research first: there is no strategy to change yet.")
    preflight("rerun")                       # an honest error now if the AI provider is not configured
    entry = {"note": note, "parts": parts, "by": p.user_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    history = list((disc.params or {}).get("strategy_feedback", [])) + [entry]
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            s.add(Feedback(object_type="strategy", object_id=disc.id, action="changes_requested", note=note))
            audit(s, p.user_id, "strategy.changes_requested", "run", disc.id, parts=parts)
    keys = ["strategy" if x == "strategy" else "icp" for x in parts]
    await get_orchestrator().rerun_from(tenant_id, disc.id, keys, p.user_id, params={"strategy_feedback": history})
    return disc.id


async def suggestion_history(tenant_id: str) -> list[dict]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            run = (await s.execute(select(Run).where(Run.kind == "discovery").order_by(Run.created_at.desc()))).scalars().first()
    return list(reversed((run.params or {}).get("strategy_feedback", []))) if run else []
