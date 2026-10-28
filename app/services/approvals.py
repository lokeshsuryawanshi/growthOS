"""Human-in-the-loop decisions. Nothing is published or sent by the system; the operator does it and says so.

Each action: the client belongs to this account -> state-machine transition ->
feedback row (fuels learning + online evals) -> audit row.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.blackboard import policy_context
from app.agents.content.agents import draft_slot, edit_distance_ratio
from app.agents.runtime.loop import AgentContext
from app.core.config import get_settings
from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.audit import audit, incident
from app.db.models import CalendarSlot, Feedback, Lead, OutreachMessage, Post, SuppressionEntry
from app.db.session import get_db
from app.guardrails.output_guard import check_outreach, check_post
from app.guardrails.state import OUTREACH_TRANSITIONS, POST_TRANSITIONS, IllegalTransition, transition
from app.llm.types import Budget, CallContext


class NotFound(Exception):
    pass


class GuardrailRefusal(Exception):
    pass


class SendRefused(ValueError):
    """'I sent it' refused for a business reason (do-not-contact, synthetic lead)."""


POST_ACTIONS = {"approve", "reject", "request_changes", "edit"}
OUTREACH_ACTIONS = {"approve", "reject", "request_changes", "edit"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _post(s, post_id: str) -> Post:
    post = (await s.execute(select(Post).where(Post.id == post_id))).scalars().first()
    if post is None:
        raise NotFound("post not found")
    return post


async def decide_post(p: Principal, tenant_id: str, post_id: str, action: str, text: str | None = None,
                      note: str = "") -> Post:
    if action not in POST_ACTIONS:
        raise ValueError(f"unknown action {action}")
    authorize(p, tenant_id)
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
    if action not in OUTREACH_ACTIONS:
        raise ValueError(f"unknown action {action}")
    authorize(p, tenant_id)
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


async def mark_sent(p: Principal, tenant_id: str, msg_id: str) -> OutreachMessage:
    """The operator confirms they sent an approved email themselves (from Gmail or any mail client). Nothing is sent
    from here. The do-not-contact list is checked once more, in case the person opted out after approval."""
    authorize(p, tenant_id)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            msg = (await s.execute(select(OutreachMessage).where(OutreachMessage.id == msg_id))).scalars().first()
            if msg is None:
                raise NotFound("message not found")
            if msg.status != "approved":
                raise IllegalTransition(f"only approved emails can be marked as sent (this one is {msg.status.replace('_', ' ')})")
            lead = (await s.execute(select(Lead).where(Lead.id == msg.lead_id))).scalars().one()
            if lead.is_synthetic:
                raise SendRefused("this lead is synthetic demo data and must never be contacted")
            if suppressed(lead, (await s.execute(select(SuppressionEntry))).scalars().all()):
                incident(s, "process", "suppressed_send_blocked", "high",
                         f"{lead.full_name or lead.company} is on the do-not-contact list", "outreach", msg.id)
                raise SendRefused("this person or company is on the do-not-contact list")
            transition(msg, "sent", OUTREACH_TRANSITIONS)
            msg.sent_at = _now()
            lead.status = "contacted"
            audit(s, p.user_id, "outreach.marked_sent", "outreach", msg.id)
            return msg


def suppressed(lead: Lead, entries) -> bool:
    return any((x.kind == "email" and lead.email and x.value.lower() == lead.email.lower()) or
               (x.kind == "domain" and lead.company_domain and x.value.lower() == lead.company_domain.lower()) or
               (x.kind == "company" and x.value.lower() == lead.company.lower()) for x in entries)


async def mark_posted(p: Principal, tenant_id: str, post_id: str) -> Post:
    """The operator confirms the approved post went out on LinkedIn (nothing is published automatically)."""
    authorize(p, tenant_id)
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
