"""Starting runs: one place for the checks the API and the UI share."""
from __future__ import annotations

from sqlalchemy import select

from app.agents.blackboard import latest
from app.agents.orchestrator.engine import DAGS, get_orchestrator
from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.models import Run, Tenant
from app.db.session import get_db
from app.integrations.errors import NOT_CONFIGURED, IntegrationError
from app.llm.gateway import get_gateway


class RunRefused(ValueError):
    """The run cannot start right now; the message says why."""


def preflight(kind: str) -> None:
    """Fail before spending anything if a required integration is missing."""
    get_gateway().require_configured()
    s = get_settings()
    if not s.uses_fixture_sources and not (s.google_api_key or s.tavily_api_key):
        raise IntegrationError("web_search", NOT_CONFIGURED, "no web search provider is configured",
                               "Set TAVILY_API_KEY (free tier at tavily.com) or enable billing for the Google key, then restart.")


async def active_run(tenant_id: str) -> Run | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(Run).where(Run.status.in_(["pending", "running", "waiting_human"]))
                                    .order_by(Run.created_at.desc()))).scalars().first()


async def start_run(p: Principal, tenant_id: str, kind: str, params: dict | None = None) -> str:
    authorize(p, "run:start", tenant_id)
    if kind not in DAGS:
        raise RunRefused(f"unknown run kind '{kind}'")
    async with get_db().reader() as s:
        tenant = await s.get(Tenant, tenant_id)
    if tenant is None or tenant.kill_switch:
        raise RunRefused("this workspace is paused")
    if kind == "weekly" and not await latest(tenant_id, "icp"):
        kind = "discovery"              # weekly work needs a strategy first
    if (cur := await active_run(tenant_id)) is not None:
        raise RunRefused(f"a {cur.kind} run is already {cur.status.replace('_', ' ')}")
    preflight(kind)
    if not p.is_agency:
        limiter.check(f"run:{tenant_id}", 6, 3600)
    orch = get_orchestrator()
    rid = await orch.create_run(tenant_id, kind, params or {}, actor=p.user_id)
    orch.start(tenant_id, rid)
    return rid
