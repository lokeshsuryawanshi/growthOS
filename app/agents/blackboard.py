"""Blackboard: versioned, typed artifacts per tenant, plus the context agents read from it.

Every read/write runs inside tenant_scope(tenant_id), so the ORM interceptor
filters it. Agents get compact summaries, not raw history (context compaction).
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import func, select

from app.core.tenancy import system_scope, tenant_scope
from app.db.models import Artifact, ClientProfile, KnowledgeDoc, Post, Tenant
from app.db.session import get_db
from app.guardrails.output_guard import PolicyContext


async def put_artifact(tenant_id: str, kind: str, data: dict[str, Any], run_id: str | None = None) -> Artifact:
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            ver = (await s.execute(select(func.max(Artifact.version)).where(Artifact.kind == kind))).scalar() or 0
            art = Artifact(kind=kind, version=ver + 1, data=data, run_id=run_id)
            s.add(art)
            return art


async def latest(tenant_id: str, kind: str) -> dict[str, Any] | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            art = (await s.execute(select(Artifact).where(Artifact.kind == kind)
                                   .order_by(Artifact.version.desc()).limit(1))).scalars().first()
    return _public(art.data) if art else None


def tenant_goals(intake: dict[str, Any]) -> dict[str, int]:
    """Per-client targets from onboarding, bounded to sane ranges; defaults come from settings."""
    from app.core.config import get_settings
    st, g = get_settings(), (intake or {}).get("goals")
    g = g if isinstance(g, dict) else {}

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(g.get(key, default))))
        except (TypeError, ValueError):
            return default
    lo = num("leads_per_week_min", st.leads_per_week_min, 1, 40)
    return {"posts_per_week": num("posts_per_week", st.posts_per_week_target, 3, 7),
            "leads_min": lo, "leads_max": max(lo, num("leads_per_week_max", st.leads_per_week_max, 1, 60))}


def primary_goal(intake: dict[str, Any]) -> str:
    g = (intake or {}).get("goals")
    return g.get("primary_goal", "") if isinstance(g, dict) else (g or "")


def _public(data: dict[str, Any]) -> dict[str, Any]:
    """Artifacts carry provenance under '_' keys for the UI; agents never see it."""
    return {k: v for k, v in data.items() if not k.startswith("_")}


async def latest_meta(tenant_id: str, kind: str) -> Artifact | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(Artifact).where(Artifact.kind == kind)
                                    .order_by(Artifact.version.desc()).limit(1))).scalars().first()


async def profile(tenant_id: str) -> ClientProfile:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(ClientProfile))).scalars().one()


async def intake_of(tenant_id: str) -> dict[str, Any]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            row = (await s.execute(select(ClientProfile))).scalars().first()
    return dict(row.intake) if row else {}


async def docs(tenant_id: str, kinds: list[str] | None = None, usable_only: bool = True) -> list[KnowledgeDoc]:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            q = select(KnowledgeDoc)
            if kinds:
                q = q.where(KnowledgeDoc.kind.in_(kinds))
            if usable_only:
                q = q.where(KnowledgeDoc.quarantined.is_(False))
            return list((await s.execute(q)).scalars().all())


def compact(data: dict[str, Any] | None, limit: int = 2500) -> str:
    """Stable, size-capped JSON for prompts."""
    if not data:
        return "(none)"
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


async def source_pack(tenant_id: str) -> list[dict[str, str]]:
    """Facts a post may cite: client-verified facts + cleaned, non-quarantined web pages."""
    pack = []
    for d in await docs(tenant_id, ["intake_note", "web_page", "website"]):
        pack.append({"url": d.source if d.kind in ("web_page", "website") else "client:verified-facts",
                     "title": d.title, "text": d.content[:1500]})
    return pack


# -- output-guard policy -------------------------------------------------------
_leak_cache: dict[str, tuple[float, list[str]]] = {}


async def other_tenant_terms(tenant_id: str) -> list[str]:
    hit = _leak_cache.get(tenant_id)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    # Reading every client's names is a cross-tenant read, so it is explicit and narrow.
    with system_scope("output guard: build cross-tenant leakage list"):
        async with get_db().reader() as s:
            tenants = (await s.execute(select(Tenant))).scalars().all()
            profiles = (await s.execute(select(ClientProfile.tenant_id, ClientProfile.policy))).all()
    products = {tid: (pol or {}).get("products", []) for tid, pol in profiles}
    terms: list[str] = []
    for t in tenants:
        if t.id == tenant_id:
            continue
        terms += [t.name, t.founder_name, *products.get(t.id, [])]
    terms = sorted({x for x in terms if x})
    _leak_cache[tenant_id] = (time.monotonic(), terms)
    return terms


async def policy_context(tenant_id: str, include_previous_posts: bool = True) -> PolicyContext:
    prof = await profile(tenant_id)
    pol, intake = prof.policy or {}, prof.intake or {}
    comp = await latest(tenant_id, "competitor_matrix")
    competitors = list(intake.get("known_competitors", [])) + [c["name"] for c in (comp or {}).get("competitors", [])]
    samples = [d.content for d in await docs(tenant_id, ["founder_sample"])]
    voice = await latest(tenant_id, "voice_profile") or {}
    source_docs: dict[str, str] = {}
    for d in await docs(tenant_id, ["intake_note", "web_page", "website"]):
        key = d.source if d.kind in ("web_page", "website") else "client:verified-facts"
        source_docs[key] = (source_docs.get(key, "") + "\n" + d.content).strip()
    for i, smp in enumerate(samples, 1):
        source_docs[f"founder:sample-{i}"] = smp
    sources = list(source_docs.values())
    previous: list[str] = []
    if include_previous_posts:
        with tenant_scope(tenant_id):
            async with get_db().reader() as s:
                previous = [t for (t,) in (await s.execute(
                    select(Post.text).where(Post.status.notin_(["rejected"])).order_by(Post.created_at.desc()).limit(40)))]
    return PolicyContext(
        tenant_name=intake.get("company_name", ""), competitors=sorted(set(competitors)),
        banned_topics=pol.get("banned_topics", []),
        banned_phrases=pol.get("banned_phrases", []), required_disclaimer=pol.get("required_disclaimer"),
        other_tenant_terms=await other_tenant_terms(tenant_id), source_texts=sources, previous_posts=previous,
        source_docs=source_docs, founder_samples=samples, example_hooks=list(voice.get("example_hooks", [])))
