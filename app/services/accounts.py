"""Accounts and client profiles: Google sign-in accounts, adding, editing and deleting clients. Server-side validation
for every input."""
from __future__ import annotations

import logging
import re
import secrets
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select

from app.core.security import Principal, authorize
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import TENANT_TABLES, Base, ClientProfile, KnowledgeDoc, Run, Tenant, User
from app.db.session import get_db
from app.integrations.hunter import clean_domain

log = logging.getLogger(__name__)
MAX_CLIENTS = 25
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,24}$")


class AccountError(ValueError):
    """User-facing validation problem."""


def lines(text: str | list[str] | None, limit: int = 20, maxlen: int = 200) -> list[str]:
    if text is None:
        return []
    items = text if isinstance(text, list) else re.split(r"[\n;]+", text)
    out = [re.sub(r"\s+", " ", i).strip() for i in items]
    return [i[:maxlen] for i in out if i][:limit]


def clean_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    host = re.sub(r"^https?://", "", raw, flags=re.I).split("/")[0].split(":")[0]
    if not clean_domain(host):
        raise ValueError(f"'{raw}' is not a valid website address")
    return raw[:300]


class OnboardingForm(BaseModel):
    company_name: str = Field(min_length=2, max_length=120)
    website: str = ""
    industry: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=20, max_length=1500, description="What the company does")
    target_customers: str = Field(min_length=10, max_length=800)
    geographies: list[str] = Field(default_factory=list)
    known_competitors: list[str] = Field(default_factory=list)   # "Name" or "Name | website"
    founder_name: str = Field(min_length=2, max_length=100)
    founder_title: str = Field(default="Founder", max_length=100)
    linkedin_url: str = ""
    samples: list[str] = Field(min_length=2, max_length=8)
    tone: str = Field(default="", max_length=400)
    topics_to_own: list[str] = Field(default_factory=list)
    topics_to_avoid: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    primary_goal: str = Field(default="", max_length=300)
    posts_per_week: int = Field(default=5, ge=3, le=7)
    leads_per_week_min: int = Field(default=10, ge=1, le=40)
    leads_per_week_max: int = Field(default=20, ge=1, le=60)

    @field_validator("website", "linkedin_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return clean_url(v)

    @field_validator("samples")
    @classmethod
    def _samples(cls, v: list[str]) -> list[str]:
        v = [s.strip() for s in v if s and s.strip()]
        if len(v) < 2:
            raise ValueError("add at least two of your own LinkedIn posts or writing samples")
        for s in v:
            if not 80 <= len(s) <= 3000:
                raise ValueError("each writing sample should be between 80 and 3000 characters")
        return v

    @model_validator(mode="after")
    def _range(self):
        if self.leads_per_week_max < self.leads_per_week_min:
            raise ValueError("maximum leads per week cannot be below the minimum")
        return self

    @classmethod
    def from_form(cls, f: dict[str, Any]) -> "OnboardingForm":
        def ml(key: str, **kw) -> list[str]:
            return lines(f.get(key), **kw)
        samples = [f.get(f"sample_{i}", "") for i in range(1, 6)]
        return cls(
            company_name=(f.get("company_name") or "").strip(), website=f.get("website", ""),
            industry=(f.get("industry") or "").strip(), company=(f.get("company") or "").strip(),
            target_customers=(f.get("target_customers") or "").strip(), geographies=ml("geographies", limit=10, maxlen=60),
            known_competitors=ml("known_competitors", limit=8), founder_name=(f.get("founder_name") or "").strip(),
            founder_title=(f.get("founder_title") or "Founder").strip(), linkedin_url=f.get("linkedin_url", ""),
            samples=samples, tone=(f.get("tone") or "").strip(), topics_to_own=ml("topics_to_own", limit=8),
            topics_to_avoid=ml("topics_to_avoid", limit=8), banned_phrases=ml("banned_phrases", limit=12),
            facts=ml("facts", limit=12, maxlen=300),
            primary_goal=(f.get("primary_goal") or "").strip(), posts_per_week=int(f.get("posts_per_week") or 5),
            leads_per_week_min=int(f.get("leads_per_week_min") or 10), leads_per_week_max=int(f.get("leads_per_week_max") or 20))


def _competitors(items: list[str]) -> tuple[list[str], list[str]]:
    names, urls = [], []
    for it in items:
        name, _, url = (x.strip() for x in it.partition("|"))
        if not url and re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/.*)?$", name, re.I):
            url, name = name, re.sub(r"^(https?://)?(www\.)?", "", name).split(".")[0].title()
        names.append(name[:100])
        if url:
            urls.append(clean_url(url))
    return names, urls


def intake_and_policy(f: OnboardingForm) -> tuple[dict, dict]:
    names, urls = _competitors(f.known_competitors)
    intake = {"company_name": f.company_name, "website": f.website, "industry": f.industry, "company": f.company,
              "target_customers": f.target_customers, "geographies": f.geographies, "known_competitors": names,
              "competitor_urls": urls, "founder_name": f.founder_name, "founder_title": f.founder_title,
              "linkedin_url": f.linkedin_url, "tone": f.tone, "topics_to_own": f.topics_to_own,
              "topics_to_avoid": f.topics_to_avoid, "facts": f.facts,
              "goals": {"primary_goal": f.primary_goal, "posts_per_week": f.posts_per_week,
                        "leads_per_week_min": f.leads_per_week_min, "leads_per_week_max": f.leads_per_week_max}}
    policy: dict[str, Any] = {"banned_topics": f.topics_to_avoid, "banned_phrases": f.banned_phrases}
    return intake, policy


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "client"


# ---------------------------------------------------------------- accounts
async def google_user(sub: str, email: str, name: str) -> Principal:
    """Find or create the account for a verified Google identity. Google's `sub` is the key; email and name follow
    whatever Google says now."""
    async with get_db().session() as s:
        user = (await s.execute(select(User).where(User.google_sub == sub))).scalars().first()
        if user is None:
            user = User(google_sub=sub, email=email, name=name)
            s.add(user)
            await s.flush()
            log.info("new account %s", user.id)
        else:
            user.email, user.name = email, name or user.name
        uid = user.id
    from app.api.deps import load_principal
    return await load_principal(uid)  # type: ignore[return-value]


async def monthly_cap(p: Principal) -> float | None:
    async with get_db().reader() as s:
        user = await s.get(User, p.user_id)
    return user.monthly_cap_usd if user else None


async def set_monthly_cap(p: Principal, raw: str) -> float | None:
    raw = (raw or "").strip().lstrip("$")
    try:
        cap = round(float(raw), 2) if raw else None
    except ValueError:
        raise AccountError("Enter the cap as a number of dollars, for example 5, or leave it empty for no cap.")
    if cap is not None and not 0 <= cap <= 10_000:
        raise AccountError("The monthly cap must be between $0 and $10,000.")
    async with get_db().session() as s:
        (await s.get(User, p.user_id)).monthly_cap_usd = cap
    return cap


async def create_client(p: Principal, form: OnboardingForm) -> Tenant:
    intake, policy = intake_and_policy(form)
    base = slugify(form.company_name)
    async with get_db().session() as s:
        if (await s.get(User, p.user_id)) is None:
            raise AccountError("Your session has ended. Please sign in again.")
        if len(p.tenant_ids) >= MAX_CLIENTS:
            raise AccountError(f"One account can hold up to {MAX_CLIENTS} clients.")
        slug = base
        while (await s.execute(select(Tenant.id).where(Tenant.slug == slug))).first():
            slug = f"{base}-{secrets.token_hex(2)}"
        t = Tenant(owner_user_id=p.user_id, slug=slug, name=form.company_name, industry=form.industry,
                   founder_name=form.founder_name, status="active")
        s.add(t)
        await s.flush()
        tid = t.id
    with tenant_scope(tid):
        async with get_db().session() as s:
            s.add(ClientProfile(intake=intake, policy=policy))
            _write_docs(s, form)
            audit(s, p.user_id, "tenant.onboarded", "tenant", tid)
    return t


async def delete_client(p: Principal, tenant_id: str) -> None:
    """Deletes the client and everything made for it (research, posts, leads, emails, runs, ledgers). Not undoable."""
    authorize(p, tenant_id)
    from app.agents.orchestrator.engine import get_orchestrator
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            run_ids = (await s.execute(select(Run.id).where(Run.status.in_(["pending", "running", "waiting_human"])))).scalars().all()
    for rid in run_ids:
        await get_orchestrator().stop(rid)
    tables = [m for m in reversed(Base.metadata.sorted_tables) if m.name in TENANT_TABLES]
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            for table in tables:
                model = next(c for c in Base.__subclasses__() if getattr(c, "__tablename__", None) == table.name)
                await s.execute(delete(model).where(model.tenant_id == tenant_id))
    async with get_db().session() as s:
        t = await s.get(Tenant, tenant_id)
        if t is not None and t.owner_user_id == p.user_id:
            await s.delete(t)
    log.info("account %s deleted client %s", p.user_id, tenant_id)


async def delete_account(p: Principal) -> None:
    """Closes the account: every client, then the account itself."""
    from app.api.deps import load_principal
    for tid in sorted(p.tenant_ids):
        await delete_client(p, tid)
    async with get_db().session() as s:
        user = await s.get(User, p.user_id)
        if user is not None:
            await s.delete(user)
    assert await load_principal(p.user_id) is None
    log.info("account %s closed", p.user_id)


def _write_docs(s, form: OnboardingForm) -> None:
    for i, sample in enumerate(form.samples, 1):
        s.add(KnowledgeDoc(kind="founder_sample", provider="client", source="onboarding", title=f"Founder post {i}",
                           content=sample, trusted=True))
    if form.facts:
        s.add(KnowledgeDoc(kind="intake_note", provider="client", source="onboarding", title="Verified company facts",
                           content="\n".join(form.facts), trusted=True))


async def update_profile(p: Principal, tenant_id: str, form: OnboardingForm) -> None:
    authorize(p, tenant_id)
    intake, policy = intake_and_policy(form)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            prof = (await s.execute(select(ClientProfile))).scalars().one()
            changed = sorted(k for k in intake if prof.intake.get(k) != intake[k])
            prof.intake, prof.policy = intake, policy
            await s.execute(delete(KnowledgeDoc).where(KnowledgeDoc.kind.in_(["founder_sample", "intake_note"]),
                                                       KnowledgeDoc.provider == "client"))
            _write_docs(s, form)
            audit(s, p.user_id, "profile.updated", "tenant", tenant_id, changed=changed)
    async with get_db().session() as s:
        t = await s.get(Tenant, tenant_id)
        t.name, t.industry, t.founder_name = form.company_name, form.industry, form.founder_name


async def profile_as_form_values(tenant_id: str) -> dict[str, Any]:
    """Current profile shaped like the HTML form, so the settings page edits what is stored."""
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            prof = (await s.execute(select(ClientProfile))).scalars().one()
            samples = [d.content for d in (await s.execute(select(KnowledgeDoc).where(
                KnowledgeDoc.kind == "founder_sample").order_by(KnowledgeDoc.created_at))).scalars()]
            facts = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.kind == "intake_note"))).scalars().first()
    i, pol = prof.intake, prof.policy or {}
    g = i.get("goals") if isinstance(i.get("goals"), dict) else {"primary_goal": i.get("goals") or ""}
    comp_urls = i.get("competitor_urls", [])
    comp_lines = [f"{n} | {comp_urls[k]}" if k < len(comp_urls) else n for k, n in enumerate(i.get("known_competitors", []))]
    v = {"company_name": i.get("company_name", ""), "website": i.get("website", ""), "industry": i.get("industry", ""),
         "company": i.get("company", ""), "target_customers": i.get("target_customers", ""),
         "geographies": "\n".join(i.get("geographies", [])), "known_competitors": "\n".join(comp_lines),
         "founder_name": i.get("founder_name", ""), "founder_title": i.get("founder_title", ""),
         "linkedin_url": i.get("linkedin_url", ""), "tone": i.get("tone", ""),
         "topics_to_own": "\n".join(i.get("topics_to_own", [])), "topics_to_avoid": "\n".join(i.get("topics_to_avoid", [])),
         "banned_phrases": "\n".join(pol.get("banned_phrases", [])),
         "facts": facts.content if facts else "",
         "primary_goal": g.get("primary_goal", ""), "posts_per_week": g.get("posts_per_week", 5),
         "leads_per_week_min": g.get("leads_per_week_min", 10), "leads_per_week_max": g.get("leads_per_week_max", 20)}
    for n in range(1, 6):
        v[f"sample_{n}"] = samples[n - 1] if n <= len(samples) else ""
    return v
