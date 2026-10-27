"""Accounts and workspaces: signup, onboarding, profile edits. Server-side validation for every input."""
from __future__ import annotations

import re
import secrets
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.security import ForbiddenError, Principal, authorize, hash_password
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import ClientProfile, KnowledgeDoc, Membership, Tenant, User
from app.db.session import get_db
from app.integrations.hunter import clean_domain

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
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"


# ---------------------------------------------------------------- signup / login
async def signup(name: str, email: str, password: str, invite_code: str = "") -> Principal:
    st = get_settings()
    name, email = name.strip(), email.strip().lower()
    if st.signup_invite_code and not secrets.compare_digest(invite_code.strip(), st.signup_invite_code):
        raise AccountError("That invite code is not valid.")
    if not 1 <= len(name) <= 100:
        raise AccountError("Please enter your name.")
    if not EMAIL_RE.match(email):
        raise AccountError("Please enter a valid email address.")
    if len(password) < 10:
        raise AccountError("Choose a password of at least 10 characters.")
    async with get_db().session() as s:
        if (await s.execute(select(User.id).where(User.email == email))).first():
            raise AccountError("An account with that email already exists. Sign in instead.")
        user = User(email=email, name=name, role="client_owner", password_hash=hash_password(password))
        s.add(user)
        await s.flush()
        return Principal(user.id, user.name, user.role, frozenset())


async def my_tenant_ids(user_id: str) -> list[str]:
    async with get_db().reader() as s:
        return list((await s.execute(select(Membership.tenant_id).where(Membership.user_id == user_id))).scalars().all())


async def create_workspace(p: Principal, form: OnboardingForm) -> Tenant:
    if p.role != "client_owner":
        raise ForbiddenError("only founders create workspaces")
    if await my_tenant_ids(p.user_id):
        raise AccountError("You already have a workspace.")
    intake, policy = intake_and_policy(form)
    base = slugify(form.company_name)
    async with get_db().session() as s:
        slug = base
        while (await s.execute(select(Tenant.id).where(Tenant.slug == slug))).first():
            slug = f"{base}-{secrets.token_hex(2)}"
        t = Tenant(slug=slug, name=form.company_name, industry=form.industry, founder_name=form.founder_name,
                   status="active", monthly_budget_usd=get_settings().self_serve_monthly_budget_usd)
        s.add(t)
        await s.flush()
        s.add(Membership(user_id=p.user_id, tenant_id=t.id))
        tid = t.id
    with tenant_scope(tid):
        async with get_db().session() as s:
            s.add(ClientProfile(intake=intake, policy=policy))
            _write_docs(s, form)
            audit(s, p.user_id, "tenant.onboarded", "tenant", tid)
    return t


def _write_docs(s, form: OnboardingForm) -> None:
    for i, sample in enumerate(form.samples, 1):
        s.add(KnowledgeDoc(kind="founder_sample", provider="client", source="onboarding", title=f"Founder post {i}",
                           content=sample, trusted=True))
    if form.facts:
        s.add(KnowledgeDoc(kind="intake_note", provider="client", source="onboarding", title="Verified company facts",
                           content="\n".join(form.facts), trusted=True))


async def update_profile(p: Principal, tenant_id: str, form: OnboardingForm) -> None:
    authorize(p, "onboarding:edit", tenant_id)
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
