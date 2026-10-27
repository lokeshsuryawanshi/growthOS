"""Record paid non-model calls (search credits, contact lookups, sends) in the tenant's cost ledger."""
from __future__ import annotations

import logging

from app.core.config import EXTERNAL_PRICES
from app.core.tenancy import tenant_scope
from app.db.models import ExternalCall
from app.db.session import get_db

log = logging.getLogger(__name__)


async def record_external(tenant_id: str | None, provider: str, operation: str, run_id: str | None = None,
                          node_key: str = "", status: str = "ok", units: float | None = None) -> None:
    if not tenant_id:
        return
    per_call, usd = EXTERNAL_PRICES.get((provider, operation), (1.0, 0.0))
    units = per_call if units is None else units
    try:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                s.add(ExternalCall(run_id=run_id, node_key=node_key, provider=provider, operation=operation, units=units,
                                   cost_usd=round(units * usd, 6), status=status))
    except Exception as e:  # noqa: BLE001 - bookkeeping must never break the work it measures
        log.warning("could not record %s/%s usage: %s", provider, operation, e)
