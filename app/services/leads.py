"""Leads the founder adds by hand.

Why it exists: while the Google OAuth app is in testing mode, Gmail can only send to the founder and listed test users,
so the honest way to demonstrate sending is a lead the founder adds themselves (for example their own address).
The address is verified by Hunter.io when it is configured; otherwise the founder must explicitly confirm it, and it
stays labelled as unverified. Nothing else about the send gate changes: approval, role, do-not-contact list, daily cap,
kill switch and a connected Gmail are still required.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.agents.leads.agents import draft_outreach
from app.agents.runtime.loop import AgentContext
from app.core.config import get_settings
from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import Lead, OutreachMessage, SuppressionEntry
from app.db.session import get_db
from app.integrations.errors import IntegrationError
from app.integrations.hunter import HunterClient, clean_domain
from app.llm.gateway import get_gateway
from app.llm.types import Budget, CallContext
from app.services.accounts import EMAIL_RE, AccountError

log = logging.getLogger(__name__)
FOUNDER_CONFIRMED = "founder_confirmed"


class LeadRefused(ValueError):
    """The lead cannot be added (409): duplicate, do-not-contact, or an address Hunter says is invalid."""


class NeedsConfirmation(AccountError):
    """The address could not be verified; the founder must confirm it explicitly (422)."""


async def add_manual_lead(p: Principal, tenant_id: str, full_name: str, email: str, company: str, title: str = "",
                          company_domain: str = "", note: str = "", confirm_unverified: bool = False) -> Lead:
    authorize(p, "lead:add", tenant_id)
    email, full_name, company = (email or "").strip().lower(), (full_name or "").strip(), (company or "").strip()
    if not EMAIL_RE.match(email):
        raise AccountError("Please enter a valid email address.")
    if not 1 <= len(full_name) <= 200 or not 2 <= len(company) <= 200:
        raise AccountError("Please enter the person's name and their company.")
    domain = clean_domain(company_domain) or email.split("@")[-1]
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            if (await s.execute(select(Lead.id).where(Lead.dedupe_key == "em:" + email))).first():
                raise LeadRefused("That person is already in your leads.")
            sup = (await s.execute(select(SuppressionEntry))).scalars().all()
    if any((x.kind == "email" and x.value.lower() == email) or (x.kind == "domain" and x.value.lower() == domain)
           or (x.kind == "company" and x.value.lower() == company.lower()) for x in sup):
        raise LeadRefused("That address is on your do-not-contact list.")

    status, confidence, why = "", 0, "Hunter.io is not connected"
    hunter = HunterClient()
    if hunter.configured:
        try:
            status, confidence = await hunter.verify(email)
        except IntegrationError as e:
            why = f"Hunter.io could not check it ({e.message})"
        if status in ("invalid", "disposable"):
            raise LeadRefused(f"Hunter.io says {email} is {status}; it would bounce.")
        if status and status != "valid":
            why = f"Hunter.io could not confirm it (status: {status})"
    if status != "valid":
        if not confirm_unverified:
            raise NeedsConfirmation(f"{why}. Tick 'I know this address is correct' to add it unverified.")
        status = FOUNDER_CONFIRMED

    today = date.today()
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            lead = Lead(week_of=today - timedelta(days=today.weekday()), source_id=f"manual:{email}",
                        dedupe_key="em:" + email, full_name=full_name[:200], title=(title or "").strip()[:200],
                        company=company, company_domain=domain, email=email, signal=(note or "").strip()[:500],
                        fit_score=0, evidence=["Added by hand by the founder"], segment="Added by hand",
                        status="qualified", source_provider="manual", contact_source="manual",
                        email_status=status, email_confidence=int(confidence or 0))
            s.add(lead)
            await s.flush()
            audit(s, p.user_id, "lead.added_manually", "lead", lead.id, email_status=status, domain=domain)
            lid = lead.id
    if get_gateway().mode != "unconfigured":
        asyncio.create_task(draft_for_lead(tenant_id, lid))    # the email draft appears on the Leads page shortly
    return lead


async def draft_for_lead(tenant_id: str, lead_id: str) -> OutreachMessage | None:
    """Draft (or redraft) the outreach email for one lead, outside a run, with a small budget of its own."""
    ctx = AgentContext(CallContext(tenant_id=tenant_id, node_key="draft_email",
                                   budget=Budget(max_usd=get_settings().default_run_budget_usd / 4)))
    try:
        return await draft_outreach(tenant_id, lead_id, ctx)
    except Exception as e:  # noqa: BLE001 - recorded in the trace; the founder can retry from the Leads page
        log.warning("drafting outreach for lead %s failed: %s", lead_id, e)
        ctx.event("outreach_writer", "lead_failed", lead=lead_id, error=str(e)[:300])
        return None
    finally:
        await ctx.flush()


async def request_draft(p: Principal, tenant_id: str, lead_id: str) -> None:
    authorize(p, "outreach:edit", tenant_id)
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalars().first()
            has = (await s.execute(select(OutreachMessage.id).where(OutreachMessage.lead_id == lead_id))).first()
    if lead is None:
        raise LookupError("not found")
    if not lead.email:
        raise LeadRefused("This lead has no email address, so there is nothing to draft.")
    if has:
        raise LeadRefused("An email is already drafted for this lead.")
    get_gateway().require_configured()
    asyncio.create_task(draft_for_lead(tenant_id, lead_id))


def is_sendable_address(lead: Lead) -> bool:
    """Verified by Hunter, or added by hand and explicitly confirmed by the founder."""
    return bool(lead.email) and (lead.email_status == "valid" or
                                 (lead.contact_source == "manual" and lead.email_status == FOUNDER_CONFIRMED))

