"""Starting runs: one place for the checks the API and the UI share."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.agents.blackboard import latest
from app.agents.orchestrator.engine import DAGS, get_orchestrator
from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.models import Run, Tenant
from app.db.session import get_db
from app.llm.gateway import get_gateway


class RunRefused(ValueError):
    """The run cannot start right now; the message says why."""


async def preflight(kind: str, tenant_id: str) -> None:
    """Fail before spending anything if the account has no AI key or no search key (shown with the fix)."""
    await get_gateway().require_ready(tenant_id)
    if not get_settings().uses_fixture_sources:
        from app.integrations.search import NO_SEARCH_KEY
        from app.services import keys
        if (await keys.for_client(tenant_id, "search"))[1] is None:
            raise NO_SEARCH_KEY


def _conflicts(active_kind: str, kind: str) -> bool:
    """Content and lead runs are independent and may overlap; everything else is exclusive."""
    pair = {active_kind, kind}
    if pair == {"content", "leads"}:
        return False
    return True


async def active_run(tenant_id: str, kind: str | None = None) -> Run | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            rows = (await s.execute(select(Run).where(Run.status.in_(["pending", "running", "waiting_human"]))
                                    .order_by(Run.created_at.desc()))).scalars().all()
    return next((r for r in rows if kind is None or _conflicts(r.kind, kind)), None)


async def start_run(p: Principal, tenant_id: str, kind: str, params: dict | None = None) -> str:
    authorize(p, tenant_id)
    if kind not in DAGS:
        raise RunRefused(f"unknown run kind '{kind}'")
    async with get_db().reader() as s:
        tenant = await s.get(Tenant, tenant_id)
    if tenant is None:
        raise RunRefused("this client no longer exists")
    if kind == "content" and not await latest(tenant_id, "content_strategy"):
        raise RunRefused("Run research and approve your strategy first: posts are written from it.")
    if kind == "leads" and not await latest(tenant_id, "icp"):
        raise RunRefused("Run research and approve your strategy first: lead search needs your ideal customer profile.")
    if (cur := await active_run(tenant_id, kind)) is not None:
        raise RunRefused(f"a {cur.kind} run is already {cur.status.replace('_', ' ')}")
    await preflight(kind, tenant_id)
    limiter.check(f"run:{tenant_id}", 6, 3600)
    params = dict(params or {})
    if kind == "leads":                 # lead weeks follow the calendar week we are in, not the content calendar
        today = date.today()
        params.setdefault("week_start", (today - timedelta(days=today.weekday())).isoformat())
    orch = get_orchestrator()
    rid = await orch.create_run(tenant_id, kind, params, actor=p.user_id)
    orch.start(tenant_id, rid)
    return rid
