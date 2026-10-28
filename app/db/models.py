"""ORM models.

Global tables: User (an operator account) and Tenant (a client profile owned by one account).
Every other table mixes in TenantScoped; the session interceptor refuses to
query or write those without a valid tenant scope.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TenantScoped:
    """Marker mixin: rows belong to exactly one tenant."""

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(String(32), ForeignKey("tenants.id"), index=True, nullable=False)


# --------------------------------------------------------------------------
# Global tables
# --------------------------------------------------------------------------
class User(Base):
    """One operator account. Signs in with Google only; identified by Google's stable `sub`, never by email."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    google_sub: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200), default="")
    monthly_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # optional, set by the operator
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tenant(Base):
    """A client profile: one founder the operator works for. Belongs to exactly one account."""
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(200), default="")
    founder_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")  # active
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)   # synthetic fixture client; the UI labels it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountSecret(Base):
    """An operator's own API key for one kind of service. Owned by the account, not by a client. The ciphertext is read
    only by app/services/keys.py; the UI only ever sees provider, last four characters and status."""
    __tablename__ = "account_secrets"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)     # one AI provider, one search key, one contacts key
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))        # llm | search | contacts
    provider: Mapped[str] = mapped_column(String(16))    # groq | openai | anthropic | tavily | hunter
    ciphertext: Mapped[str] = mapped_column(Text)        # Fernet({"u": user_id, "k": kind, "p": provider, "v": key})
    last4: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(16), default="valid")    # valid | invalid (rejected after saving)
    status_detail: Mapped[str] = mapped_column(String(300), default="")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccountEvent(Base):
    """Account-level audit trail (keys added, replaced, deleted; caps changed). Never contains a key."""
    __tablename__ = "account_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Tenant-scoped tables
# --------------------------------------------------------------------------
class ClientProfile(TenantScoped, Base):
    """Onboarding intake: the only thing the founder fills in."""
    __tablename__ = "client_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    intake: Mapped[dict[str, Any]] = mapped_column(default=dict)
    policy: Mapped[dict[str, Any]] = mapped_column(default=dict)  # banned topics, disclaimers, competitors...
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeDoc(TenantScoped, Base):
    __tablename__ = "knowledge_docs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))  # founder_sample|website|intake_note|web_page
    provider: Mapped[str] = mapped_column(String(32), default="client")  # client|web_fetch|fixture
    source: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    content: Mapped[str] = mapped_column(Text)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)  # client-provided vs scraped
    injection_score: Mapped[float] = mapped_column(Float, default=0.0)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(TenantScoped, Base):
    """Blackboard entry: a typed, versioned agent output."""
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifact_kind_ver", "tenant_id", "kind", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(48))  # voice_profile|market_report|competitor_matrix|trend_report|content_strategy|icp
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(TenantScoped, Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))  # discovery|weekly
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|waiting_human|succeeded|failed|cancelled
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget_usd: Mapped[float] = mapped_column(Float, default=2.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunNode(TenantScoped, Base):
    """One DAG node. Node-level checkpoint: succeeded nodes are never re-executed on resume."""
    __tablename__ = "run_nodes"
    __table_args__ = (UniqueConstraint("run_id", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    deps: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|succeeded|failed|blocked|waiting_human|skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMCall(TenantScoped, Base):
    """Cost ledger + trace for every model call."""
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    node_key: Mapped[str] = mapped_column(String(64), default="")
    agent: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(100))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)       # provider response time
    pace_wait_ms: Mapped[int] = mapped_column(Integer, default=0)     # waiting in our per-model pacing before the call
    retry_wait_ms: Mapped[int] = mapped_column(Integer, default=0)    # sleeping after 429s / transient errors
    route_note: Mapped[str] = mapped_column(String(300), default="")  # spillover or skipped models, if any
    retries: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|fallback
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ExternalCall(TenantScoped, Base):
    """Cost ledger for paid non-model services: web search credits, contact lookups, sends."""
    __tablename__ = "external_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    node_key: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(32))        # tavily|gemini_search|hunter|gmail
    operation: Mapped[str] = mapped_column(String(32))       # search|domain_search|verify|send
    units: Mapped[float] = mapped_column(Float, default=1.0) # provider credits used
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)  # at the list price in config (EXTERNAL_PRICES)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LLMCache(TenantScoped, Base):
    __tablename__ = "llm_cache"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceEvent(TenantScoped, Base):
    """Tool calls, guardrail decisions, loop steps: the agent's visible reasoning trail."""
    __tablename__ = "trace_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    node_key: Mapped[str] = mapped_column(String(64), default="")
    agent: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(32))  # tool_call|tool_denied|repair|budget_stop|loop_end|critic_round
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalendarSlot(TenantScoped, Base):
    __tablename__ = "calendar_slots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    week: Mapped[int] = mapped_column(Integer)          # 1..8 relative to calendar start
    publish_date: Mapped[date] = mapped_column(Date)
    pillar: Mapped[str] = mapped_column(String(120))
    format: Mapped[str] = mapped_column(String(48))     # story|how_to|opinion|data_insight|carousel_outline|case_study
    topic: Mapped[str] = mapped_column(String(300))
    angle: Mapped[str] = mapped_column(Text, default="")
    trend_ref: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(32), default="planned")  # planned|drafted|approved|published|skipped
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Post(TenantScoped, Base):
    __tablename__ = "posts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slot_id: Mapped[str | None] = mapped_column(ForeignKey("calendar_slots.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pillar: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[list[Any]] = mapped_column(default=list)
    sources: Mapped[list[Any]] = mapped_column(default=list)
    claims: Mapped[list[Any]] = mapped_column(default=list)   # each figure with its verbatim source quote
    rationale: Mapped[str] = mapped_column(Text, default="")
    critic_score: Mapped[float] = mapped_column(Float, default=0.0)
    critic_feedback: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32), default="drafting")
    approved_text: Mapped[str] = mapped_column(Text, default="")
    edit_distance: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0 = untouched, 1 = rewritten
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # operator confirmed it was posted
    # The operator's own rating (the human bar; same questions as the blind review kit in evals/voice.py)
    rating_voice: Mapped[int | None] = mapped_column(Integer, nullable=True)            # sounds like the founder, 1-5
    rating_would_post: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rating_template: Mapped[bool | None] = mapped_column(Boolean, nullable=True)        # reads like a template
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lead(TenantScoped, Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    week_of: Mapped[date] = mapped_column(Date)
    source_id: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200))
    company_domain: Mapped[str] = mapped_column(String(200), default="")
    linkedin_url: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    signal: Mapped[str] = mapped_column(Text, default="")
    fit_score: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    segment: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(32), default="qualified")  # qualified|prospect|outreach_drafted|contacted|replied|rejected
    source_url: Mapped[str] = mapped_column(String(500), default="")      # page the company/signal came from
    source_provider: Mapped[str] = mapped_column(String(32), default="")  # gemini_search|tavily|fixture
    contact_source: Mapped[str] = mapped_column(String(32), default="")   # hunter|fixture|""
    email_status: Mapped[str] = mapped_column(String(24), default="")     # valid|accept_all|unknown|invalid|""
    email_confidence: Mapped[int] = mapped_column(Integer, default=0)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeadRejection(TenantScoped, Base):
    """Prospects we deliberately did not keep, with the reason (dedupe, suppression, low fit...)."""
    __tablename__ = "lead_rejections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id: Mapped[str] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(48))  # duplicate|suppressed|low_fit|competitor|missing_contact
    detail: Mapped[str] = mapped_column(Text, default="")
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutreachMessage(TenantScoped, Base):
    __tablename__ = "outreach_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    subject: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text)
    alt_text: Mapped[str] = mapped_column(Text, default="")
    personalization: Mapped[str] = mapped_column(Text, default="")
    critic_score: Mapped[float] = mapped_column(Float, default=0.0)
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")
    approved_text: Mapped[str] = mapped_column(Text, default="")
    edit_distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)   # operator: "I sent it"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SuppressionEntry(TenantScoped, Base):
    """Do-not-contact list: existing customers, opted-out people, competitors' staff."""
    __tablename__ = "suppression_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # email|domain|company
    value: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(200), default="")


class Feedback(TenantScoped, Base):
    """Human edits and rejections; fuels the voice-profile reflection loop and online evals."""
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    object_type: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))  # approved|edited|changes_requested|rejected
    original: Mapped[str] = mapped_column(Text, default="")
    revised: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GuardrailIncident(TenantScoped, Base):
    __tablename__ = "guardrail_incidents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    layer: Mapped[str] = mapped_column(String(16))  # input|tool|output|process|cost
    rule: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))  # low|medium|high|critical
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(TenantScoped, Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(64))  # user id or "system:<component>"
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


TENANT_TABLES = [m.__tablename__ for m in Base.__subclasses__() if issubclass(m, TenantScoped)]
