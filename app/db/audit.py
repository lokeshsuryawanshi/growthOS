"""Audit and guardrail-incident helpers. Both write inside the caller's session and scope."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import require_tenant_id
from app.db.models import AuditLog, GuardrailIncident


def audit(session: AsyncSession, actor: str, action: str, object_type: str = "", object_id: str = "",
          tenant_id: str | None = None, **detail: Any) -> None:
    session.add(AuditLog(tenant_id=tenant_id or require_tenant_id(), actor=actor, action=action,
                         object_type=object_type, object_id=object_id, detail=detail))


def incident(session: AsyncSession, layer: str, rule: str, severity: str, detail: str,
             object_type: str = "", object_id: str = "", run_id: str | None = None,
             tenant_id: str | None = None) -> None:
    session.add(GuardrailIncident(tenant_id=tenant_id or require_tenant_id(), layer=layer, rule=rule,
                                  severity=severity, detail=detail[:2000], object_type=object_type,
                                  object_id=object_id, run_id=run_id))
