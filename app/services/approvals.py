"""Human-in-the-loop decisions. The only path to publishing or sending anything.

Each action: RBAC check -> tenant access check -> state-machine transition ->
feedback row (fuels learning + online evals) -> audit row.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.agents.blackboard import policy_context
from app.agents.blackboard import intake_of
from app.agents.content.agents import draft_slot, edit_distance_ratio
from app.agents.leads.agents import email_footer
from app.agents.runtime.loop import AgentContext
from app.core.config import get_settings
from app.core.security import ForbiddenError, Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.audit import audit, incident
from app.db.models import CalendarSlot, Feedback, Lead, OutreachMessage, Post, SuppressionEntry, Tenant
from app.db.session import get_db
from app.guardrails.output_guard import check_outreach, check_post
from app.guardrails.state import OUTREACH_TRANSITIONS, POST_TRANSITIONS, transition
from app.integrations import gmail
from app.integrations.errors import IntegrationError
from app.llm.types import Budget, CallContext


class NotFound(Exception):
    pass


class GuardrailRefusal(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _post(s, post_id: str) -> Post:
    post = (await s.execute(select(Post).where(Post.id == post_id))).scalars().first()
    if post is None:
        raise NotFound("post not found")
    return post


async def decide_post(p: Principal, tenant_id: str, post_id: str, action: str, text: str | None = None,
                      note: str = "") -> Post:
    perm = {"approve": "content:approve", "reject": "content:approve", "request_changes": "content:edit",
            "edit": "content:edit"}.get(action)
    if perm is None:
        raise ValueError(f"unknown action {action}")
    authorize(p, perm, tenant_id)
    policy = await policy_context(tenant_id, include_previous_posts=False) if text else None
    regenerate_slot = None
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            post = await _post(s, post_id)
            original = post.text
            if text is not None and text.strip() != original.strip():
                report = check_post(text, post.hashtags, policy)  # human edits are checked too (no claim bookkeeping asked of the founder)
                if report.critical:
                    incident(s, "output", "human_edit_blocked", "critical", report.instructions(), "post", post.id)
                    raise GuardrailRefusal(report.instructions())
                post.guardrail_report = report.model_dump()
            final = (text or original).strip()
            if action == "edit":
                if post.status != "pending_approval":
                    raise ValueError("only drafts awaiting approval can be edited")
                post.text = final
            elif action == "approve":
                transition(post, "approved", POST_TRANSITIONS)
                post.approved_text = final
                post.edit_distance = edit_distance_ratio(original, final)
                transition(post, "scheduled", POST_TRANSITIONS)
                if post.slot_id:
                    slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == post.slot_id))).scalars().one()
                    slot.status = "approved"
            elif action == "reject":
                transition(post, "rejected", POST_TRANSITIONS)
                if post.slot_id:
                    slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == post.slot_id))).scalars().one()
                    slot.status = "skipped"
            elif action == "request_changes":
                transition(post, "changes_requested", POST_TRANSITIONS)
                regenerate_slot = post.slot_id
            if action != "edit" or final != original:
                post.decided_by, post.decided_at, post.reviewer_note = p.user_id, _now(), note
            fb_action = "edited" if (action == "approve" and final != original) or action == "edit" else \
                {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested"}[action]
            s.add(Feedback(object_type="post", object_id=post.id, action=fb_action, original=original,
                           revised=final if final != original else "", note=note))
            audit(s, p.user_id, f"post.{action}", "post", post.id, edit_distance=post.edit_distance)
    if regenerate_slot:
        asyncio.create_task(regenerate(tenant_id, regenerate_slot, note))
    return post


async def regenerate(tenant_id: str, slot_id: str, note: str) -> Post:
    """Redraft a slot after 'request changes', carrying the reviewer's note into the writer brief."""
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot_id))).scalars().one()
            slot.status = "planned"
    ctx = AgentContext(CallContext(tenant_id=tenant_id, node_key="regenerate",
                                   budget=Budget(max_usd=get_settings().default_run_budget_usd / 4)))
    try:
        return await draft_slot(tenant_id, slot_id, ctx, note=note)
    finally:
        await ctx.flush()


async def decide_outreach(p: Principal, tenant_id: str, msg_id: str, action: str, text: str | None = None,
                          note: str = "", subject: str | None = None) -> OutreachMessage:
    perm = {"approve": "outreach:approve", "reject": "outreach:approve", "request_changes": "outreach:edit",
            "edit": "outreach:edit"}.get(action)
    if perm is None:
        raise ValueError(f"unknown action {action}")
    authorize(p, perm, tenant_id)
    policy = await policy_context(tenant_id, include_previous_posts=False) if text else None
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            msg = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == msg_id))).scalars().first()
            if msg is None:
                raise NotFound("message not found")
            lead = (await s.execute(select(Lead).where(Lead.id == msg.lead_id))).scalars().one()
            original = msg.text
            if text is not None and text.strip() != original.strip():
                report = check_outreach(text, lead.company, lead.signal, policy)
                if report.critical:
                    raise GuardrailRefusal(report.instructions())
                msg.guardrail_report = report.model_dump()
            final = (text or original).strip()
            if subject is not None and subject.strip():
                msg.subject = subject.strip()[:300]
            if action == "edit":
                if msg.status != "pending_approval":
                    raise ValueError("only messages awaiting approval can be edited")
                msg.text = final
            elif action == "approve":
                transition(msg, "approved", OUTREACH_TRANSITIONS)
                msg.approved_text, msg.edit_distance = final, edit_distance_ratio(original, final)
            elif action == "reject":
                transition(msg, "rejected", OUTREACH_TRANSITIONS)
                lead.status = "rejected"
            elif action == "request_changes":
                transition(msg, "changes_requested", OUTREACH_TRANSITIONS)
            msg.decided_by, msg.decided_at = p.user_id, _now()
            s.add(Feedback(object_type="outreach", object_id=msg.id,
                           action="edited" if final != original else
                           {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested",
                            "edit": "edited"}[action],
                           original=original, revised=final if final != original else "", note=note))
            audit(s, p.user_id, f"outreach.{action}", "outreach", msg.id)
            return msg


async def send_outreach(p: Principal, tenant_id: str, msg_id: str) -> OutreachMessage:
    """Process guard for the one irreversible action.

    Requires: founder role, approved message, kill switch off, a verified recipient address, not suppressed, under the
    daily cap, Gmail connected. The message is locked as 'sending' before Gmail is called, so a double click or a crash
    can never send twice; a failure puts it back to 'approved' with the reason.
    """
    authorize(p, "outreach:send", tenant_id)
    async with get_db().reader() as s:
        tenant = await s.get(Tenant, tenant_id)
    if tenant is None or tenant.kill_switch:
        raise ForbiddenError("sending is paused for this client")
    intake = await intake_of(tenant_id)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            msg = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == msg_id))).scalars().first()
            if msg is None:
                raise NotFound("message not found")
            if msg.status != "approved":
                raise ForbiddenError("only approved messages can be sent")
            lead = (await s.execute(select(Lead).where(Lead.id == msg.lead_id))).scalars().one()
            if lead.is_synthetic:
                raise ForbiddenError("this lead is synthetic demo data and can never be emailed")
            if not lead.email or lead.email_status != "valid":
                raise ForbiddenError("the recipient's address is not verified (Hunter.io status: "
                                     f"{lead.email_status or 'none'}); only verified addresses can be emailed")
            sup = (await s.execute(select(SuppressionEntry))).scalars().all()
            if any((x.kind == "email" and x.value.lower() == lead.email.lower()) or
                   (x.kind == "domain" and x.value.lower() == lead.company_domain.lower()) or
                   (x.kind == "company" and x.value.lower() == lead.company.lower()) for x in sup):
                incident(s, "process", "suppressed_send_blocked", "high", f"{lead.full_name or lead.company} is on the suppression list",
                         "outreach", msg.id)
                raise ForbiddenError("recipient is on the do-not-contact list")
            sent_today = (await s.execute(select(func.count(OutreachMessage.id))
                                          .where(OutreachMessage.sent_at >= today))).scalar_one()
            in_flight = (await s.execute(select(func.count(OutreachMessage.id))
                                         .where(OutreachMessage.status == "sending"))).scalar_one()
            if sent_today + in_flight >= get_settings().outreach_daily_send_cap:
                raise ForbiddenError("daily send cap reached; protects the founder's mailbox reputation")
            to, subject = lead.email, msg.subject or f"Quick question for {lead.company}"
            body = (msg.approved_text or msg.text) + email_footer(intake)
            transition(msg, "sending", OUTREACH_TRANSITIONS)
            msg.send_error = ""
            audit(s, p.user_id, "outreach.send_started", "outreach", msg.id, to_domain=to.split("@")[-1])
    try:
        external_id = await gmail.send_email(tenant_id, to, subject, body)
    except IntegrationError as e:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                m = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == msg_id))).scalars().one()
                transition(m, "approved", OUTREACH_TRANSITIONS)
                m.send_error = f"{e.message}. {e.recovery}".strip()
                audit(s, p.user_id, "outreach.send_failed", "outreach", m.id, code=e.code, integration=e.integration)
        raise
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            m = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == msg_id))).scalars().one()
            ld = (await s.execute(select(Lead).where(Lead.id == m.lead_id))).scalars().one()
            transition(m, "sent", OUTREACH_TRANSITIONS)
            m.sent_at, m.external_id = _now(), external_id
            ld.status = "contacted"
            audit(s, p.user_id, "outreach.sent", "outreach", m.id, gmail_id=external_id)
            return m


async def recover_interrupted_sends() -> int:
    """At startup: a message left in 'sending' (process died mid-send) goes back to 'approved' with a warning.
    It is never re-sent automatically, because Gmail may already have delivered it."""
    from sqlalchemy import update
    from app.core.tenancy import system_scope
    with system_scope("startup: recover interrupted sends"):
        async with get_db().session() as s:
            res = await s.execute(update(OutreachMessage).where(OutreachMessage.status == "sending").values(
                status="approved", send_error="Interrupted while sending. Check your Gmail Sent folder before sending again."))
            return res.rowcount or 0


async def mark_posted(p: Principal, tenant_id: str, post_id: str) -> Post:
    """The founder confirms they posted an approved post on LinkedIn (no auto-publishing is integrated)."""
    authorize(p, "content:approve", tenant_id)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            post = await _post(s, post_id)
            transition(post, "published", POST_TRANSITIONS)
            post.published_at = _now()
            if post.slot_id:
                slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == post.slot_id))).scalars().one()
                slot.status = "published"
            audit(s, p.user_id, "post.marked_posted", "post", post.id)
            return post
