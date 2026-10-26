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
    return art.data if art else None


async def latest_meta(tenant_id: str, kind: str) -> Artifact | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(Artifact).where(Artifact.kind == kind)
                                    .order_by(Artifact.version.desc()).limit(1))).scalars().first()


async def profile(tenant_id: str) -> ClientProfile:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(ClientProfile))).scalars().one()


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
    for d in await docs(tenant_id, ["intake_note", "web_page"]):
        pack.append({"url": d.source if d.kind == "web_page" else "client:verified-facts",
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
    sources = [p["text"] for p in await source_pack(tenant_id)] + samples
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
        other_tenant_terms=await other_tenant_terms(tenant_id), source_texts=sources, previous_posts=previous)
