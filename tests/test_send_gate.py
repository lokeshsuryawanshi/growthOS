"""Sending email is the one irreversible action. Every way around the gate must fail, and every failure must say why."""
import asyncio

import pytest
from sqlalchemy import select, update

from app.agents.orchestrator.engine import Orchestrator
from app.core.config import get_settings
from app.core.security import ForbiddenError, Principal
from app.core.tenancy import tenant_scope
from app.db.models import AuditLog, Lead, OutreachMessage, SuppressionEntry
from app.fixtures.seed import seed
from app.integrations.errors import AUTH_FAILED, NOT_CONFIGURED, IntegrationError
from app.llm.gateway import LLMGateway, set_gateway
from app.services import approvals
from tests.helpers import FakeGoogle, connect_fake_gmail, make_sendable

START = "2026-11-02"


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    ids = await seed(db)
    orch, tid = Orchestrator(), ids["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": START, "auto_approve_gates": True}))
    await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    return db, tid, Principal("u-owner", "Founder", "client_owner", frozenset({tid}))


async def approved(db, tid, owner, n=1):
    with tenant_scope(tid):
        async with db.reader() as s:
            ids = list((await s.execute(select(OutreachMessage.id).where(OutreachMessage.status == "pending_approval"))).scalars())[:n]
    for i in ids:
        await approvals.decide_outreach(owner, tid, i, "approve")
    return ids


async def msg(db, tid, mid):
    with tenant_scope(tid):
        async with db.reader() as s:
            return (await s.execute(select(OutreachMessage).where(OutreachMessage.id == mid))).scalars().one()


async def test_unapproved_message_cannot_be_sent(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    with tenant_scope(tid):
        async with db.reader() as s:
            mid = (await s.execute(select(OutreachMessage.id).where(OutreachMessage.status == "pending_approval"))).scalars().first()
    with pytest.raises(ForbiddenError, match="approved"):
        await approvals.send_outreach(owner, tid, mid)
    assert g.sent == []


async def test_unverified_address_cannot_be_emailed(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    [mid] = await approved(db, tid, owner)
    with tenant_scope(tid):
        async with db.session() as s:
            await s.execute(update(Lead).values(is_synthetic=False, email_status="unknown"))
    with pytest.raises(ForbiddenError, match="not verified"):
        await approvals.send_outreach(owner, tid, mid)
    assert g.sent == []


async def test_only_the_founder_can_send(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    for role in ("account_manager", "client_viewer", "platform_admin"):
        with pytest.raises(ForbiddenError):
            await approvals.send_outreach(Principal("u-x", "X", role, frozenset({tid})), tid, mid)
    with pytest.raises(ForbiddenError):
        await approvals.send_outreach(Principal("u-y", "Y", "client_owner", frozenset({"other"})), tid, mid)
    assert g.sent == []


async def test_gmail_not_connected_is_reported_and_message_stays_approved(world):
    db, tid, owner = world
    FakeGoogle().install()
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    with pytest.raises(IntegrationError) as e:
        await approvals.send_outreach(owner, tid, mid)
    assert e.value.code == NOT_CONFIGURED and "Connect Gmail" in e.value.recovery
    m = await msg(db, tid, mid)
    assert m.status == "approved" and "Gmail is not connected" in m.send_error and m.sent_at is None


async def test_provider_failure_returns_to_approved_without_sending(world):
    db, tid, owner = world
    g = FakeGoogle(token_error="invalid_grant").install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    with pytest.raises(IntegrationError) as e:
        await approvals.send_outreach(owner, tid, mid)
    assert e.value.code == AUTH_FAILED
    m = await msg(db, tid, mid)
    assert m.status == "approved" and m.send_error and g.sent == []
    with tenant_scope(tid):
        async with db.reader() as s:
            actions = [a.action for a in (await s.execute(select(AuditLog))).scalars()]
    assert "outreach.send_failed" in actions and "outreach.sent" not in actions


async def test_double_click_sends_exactly_once(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    results = await asyncio.gather(*[approvals.send_outreach(owner, tid, mid) for _ in range(4)], return_exceptions=True)
    assert sum(1 for r in results if isinstance(r, OutreachMessage)) == 1
    assert len(g.sent) == 1                                                # one Gmail call, however many clicks
    assert (await msg(db, tid, mid)).status == "sent"


async def test_suppressed_recipient_is_refused_even_after_approval(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    m = await msg(db, tid, mid)
    with tenant_scope(tid):
        async with db.session() as s:
            lead = (await s.execute(select(Lead).where(Lead.id == m.lead_id))).scalars().one()
            s.add(SuppressionEntry(kind="email", value=lead.email.upper(), reason="opted out later"))
    with pytest.raises(ForbiddenError, match="do-not-contact"):
        await approvals.send_outreach(owner, tid, mid)
    assert g.sent == []


async def test_daily_cap_protects_the_mailbox(world, monkeypatch):
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    monkeypatch.setattr(get_settings(), "outreach_daily_send_cap", 2)
    ids = await approved(db, tid, owner, n=3)
    await approvals.send_outreach(owner, tid, ids[0])
    await approvals.send_outreach(owner, tid, ids[1])
    with pytest.raises(ForbiddenError, match="daily send cap"):
        await approvals.send_outreach(owner, tid, ids[2])
    assert len(g.sent) == 2


async def test_interrupted_send_is_released_not_resent(world):
    db, tid, owner = world
    g = FakeGoogle().install()
    [mid] = await approved(db, tid, owner)
    with tenant_scope(tid):
        async with db.session() as s:
            (await s.execute(select(OutreachMessage).where(OutreachMessage.id == mid))).scalars().one().status = "sending"
    assert await approvals.recover_interrupted_sends() == 1
    m = await msg(db, tid, mid)
    assert m.status == "approved" and "Check your Gmail Sent folder" in m.send_error and g.sent == []


async def test_footer_and_subject_are_applied_to_what_gmail_receives(world):
    import base64
    db, tid, owner = world
    g = FakeGoogle().install()
    await connect_fake_gmail(db, tid)
    await make_sendable(db, tid)
    [mid] = await approved(db, tid, owner)
    await approvals.send_outreach(owner, tid, mid)
    raw = base64.urlsafe_b64decode(g.sent[0]["raw"]).decode()
    assert "Subject: Quick question about" in raw and "reply \"no thanks\"" in raw       # opt-out line is always present
