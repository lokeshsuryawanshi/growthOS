"""Leads added by hand: verified by Hunter or explicitly confirmed, labelled, and still behind the full send gate."""
import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.config import get_settings
from app.core.security import Principal
from app.core.tenancy import tenant_scope
from app.db.models import Lead, OutreachMessage, SuppressionEntry
from app.fixtures.seed import seed
from app.integrations import http
from app.llm.gateway import LLMGateway, set_gateway
from app.services import approvals, leads as lead_service
from app.services.accounts import AccountError
from tests.helpers import FakeGoogle, connect_fake_gmail


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    set_orchestrator(Orchestrator())
    ids = await seed(db)
    tid = ids["ledgerloop"]
    return db, tid, Principal("u-owner", "Founder", "client_owner", frozenset({tid}))


def hunter(status: str):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"status": status, "score": 91}})
    http.set_transport(httpx.MockTransport(handler))


async def test_unverifiable_address_needs_explicit_confirmation(world):
    db, tid, owner = world
    with pytest.raises(lead_service.NeedsConfirmation, match="Hunter.io is not connected"):
        await lead_service.add_manual_lead(owner, tid, "Me Myself", "me@founder.example", "My Co")
    lead = await lead_service.add_manual_lead(owner, tid, "Me Myself", "me@founder.example", "My Co", confirm_unverified=True)
    assert lead.email_status == "founder_confirmed" and lead.contact_source == "manual" and lead.status == "qualified"
    with pytest.raises(lead_service.LeadRefused, match="already"):
        await lead_service.add_manual_lead(owner, tid, "Me Again", "me@founder.example", "My Co", confirm_unverified=True)


async def test_hunter_verifies_or_rejects_the_address(world, monkeypatch):
    db, tid, owner = world
    monkeypatch.setattr(get_settings(), "hunter_api_key", "hk")
    hunter("valid")
    lead = await lead_service.add_manual_lead(owner, tid, "Ana Real", "ana@real.example", "Real Co")
    assert lead.email_status == "valid" and lead.email_confidence == 91
    hunter("invalid")
    with pytest.raises(lead_service.LeadRefused, match="invalid"):
        await lead_service.add_manual_lead(owner, tid, "Bo Gus", "bo@bogus.example", "Bogus Co", confirm_unverified=True)
    hunter("accept_all")
    with pytest.raises(lead_service.NeedsConfirmation, match="accept_all"):
        await lead_service.add_manual_lead(owner, tid, "Cy Maybe", "cy@maybe.example", "Maybe Co")


async def test_do_not_contact_and_bad_input_are_refused(world, db):
    _, tid, owner = world
    with tenant_scope(tid):
        async with db.session() as s:
            s.add(SuppressionEntry(kind="domain", value="blocked.example", reason="existing customer"))
    with pytest.raises(lead_service.LeadRefused, match="do-not-contact"):
        await lead_service.add_manual_lead(owner, tid, "No Way", "x@blocked.example", "Blocked", confirm_unverified=True)
    with pytest.raises(AccountError):
        await lead_service.add_manual_lead(owner, tid, "No Mail", "not-an-email", "Co", confirm_unverified=True)


async def test_a_confirmed_manual_lead_goes_through_the_whole_send_gate(world, db):
    _, tid, owner = world
    lead = await lead_service.add_manual_lead(owner, tid, "Me Myself", "me@founder.example", "My Co", note="testing the flow",
                                              confirm_unverified=True)
    msg = await lead_service.draft_for_lead(tid, lead.id)          # the background draft, awaited here
    assert msg is not None and msg.status in ("pending_approval", "blocked")
    if msg.status == "blocked":
        pytest.skip("mock draft blocked by guardrails")
    with pytest.raises(approvals.SendRefused, match="approved"):
        await approvals.send_outreach(owner, tid, msg.id)        # still needs approval
    await approvals.decide_outreach(owner, tid, msg.id, "approve")
    with pytest.raises(Exception, match="not connected|Gmail"):
        await approvals.send_outreach(owner, tid, msg.id)        # still needs a connected Gmail
    google = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    sent = await approvals.send_outreach(owner, tid, msg.id)
    assert sent.status == "sent" and len(google.sent) == 1


async def test_founder_confirmation_only_counts_for_leads_added_by_hand(world, db):
    """Tampering a searched lead's status to 'founder_confirmed' does not get it past the gate."""
    _, tid, owner = world
    lead = await lead_service.add_manual_lead(owner, tid, "Me Myself", "me@founder.example", "My Co", confirm_unverified=True)
    with tenant_scope(tid):
        async with db.session() as s:
            row = (await s.execute(select(Lead).where(Lead.id == lead.id))).scalars().one()
            row.contact_source = "hunter"
    assert not lead_service.is_sendable_address(row)
