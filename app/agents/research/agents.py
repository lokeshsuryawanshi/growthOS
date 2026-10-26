"""Discovery agents: founder voice, market, competitors, trends.

Market/competitor/trend analysts are tool-using loops over web_search and fetch_page. Rules that are enforced in
code, not left to the prompt:
* search runs on a real provider or fails the node with the fix; there is no canned fallback;
* fetch_page can only open URLs a search returned or the client gave us, over an SSRF-safe fetcher;
* every page passes the input guard before an agent reads it;
* after the agent finishes, every cited URL is checked against what was actually seen. Unverifiable claims are
  dropped and counted, and the artifact records its provenance (providers, queries, pages) for the UI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.blackboard import compact, docs, latest, profile
from app.agents.runtime.loop import AgentContext, AgentError, Tool, run_agent, structured
from app.agents.schemas import CompetitorMatrix, MarketReport, TrendReport, VoiceProfile
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import incident
from app.db.models import KnowledgeDoc
from app.db.session import get_db
from app.guardrails.input_guard import sanitize, scan
from app.integrations.errors import IntegrationError
from app.integrations.research import get_research


class SearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=200)


class FetchArgs(BaseModel):
    url: str = Field(max_length=500)


def _norm(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def research_state(ctx: AgentContext) -> dict:
    return ctx.call.extra.setdefault("research", {"allowed": set(), "hosts": set(), "searches": [], "pages": [],
                                                   "fallbacks": [], "dropped": 0, "synthetic": False})


def prime(ctx: AgentContext, intake: dict) -> None:
    """URLs the client gave us are readable from the start (their site and competitor sites)."""
    st = research_state(ctx)
    for u in [intake.get("website", ""), *intake.get("competitor_urls", [])]:
        if u:
            st["allowed"].add(_norm(u))
            if urlparse(u).hostname:
                st["hosts"].add(urlparse(u).hostname.removeprefix("www."))


def provenance(ctx: AgentContext) -> dict:
    st = research_state(ctx)
    return {"mode": get_settings().effective_llm_mode, "synthetic": st["synthetic"],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "searches": st["searches"], "pages": st["pages"], "fallbacks": st["fallbacks"],
            "dropped_unverified": st["dropped"]}


async def _web_search(args: SearchArgs, ctx: AgentContext):
    st = research_state(ctx)
    res = await get_research().search(args.query)        # IntegrationError aborts the node with the fix
    st["synthetic"] = st["synthetic"] or res.synthetic
    st["searches"].append({"query": args.query, "provider": res.provider, "results": len(res.hits)})
    for note in res.skipped:
        if note not in st["fallbacks"]:
            st["fallbacks"].append(note)
    answer = ""
    if res.answer:
        verdict = await scan(res.answer, ctx.call)
        answer = "[withheld: flagged as prompt injection]" if verdict.quarantined else verdict.clean_text[:3000]
    for h in res.hits:
        st["allowed"].add(_norm(h.url))
    return {"provider": res.provider, "answer": answer,
            "results": [{"title": sanitize(h.title)[0][:150], "url": h.url, "snippet": sanitize(h.snippet)[0][:240]}
                        for h in res.hits[:8]]}


async def _store_page(ctx: AgentContext, url: str, title: str, text: str, kind: str, provider: str) -> KnowledgeDoc:
    verdict = await scan(text, ctx.call)
    with tenant_scope(ctx.tenant_id):
        async with get_db().session() as s:
            # Parallel agents may fetch the same URL. Writes run under BEGIN IMMEDIATE, so re-checking inside this
            # write transaction is race-free.
            existing = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.source == url))).scalars().first()
            if existing is None:
                existing = KnowledgeDoc(kind=kind, provider=provider, source=url, title=title[:300],
                                        content=verdict.clean_text, trusted=False, injection_score=verdict.score,
                                        quarantined=verdict.quarantined)
                s.add(existing)
                await s.flush()
                if verdict.quarantined:
                    incident(s, "input", "prompt_injection", "critical",
                             f"quarantined {url}: {'; '.join(verdict.reasons)}", "knowledge_doc", existing.id,
                             run_id=ctx.call.run_id)
                elif verdict.hidden_injection:
                    incident(s, "input", "hidden_injection_stripped", "high",
                             f"{url}: {'; '.join(verdict.reasons)}", "knowledge_doc", existing.id, run_id=ctx.call.run_id)
            return existing


async def _fetch_page(args: FetchArgs, ctx: AgentContext):
    st = research_state(ctx)
    backend = get_research()
    url = args.url.strip()
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if not (backend.may_fetch(_norm(url), st["allowed"]) or _norm(url) in st["allowed"] or host in st["hosts"]):
        ctx.event("research", "tool_denied", tool="fetch_page", reason="url not allowlisted", url=url)
        return "Error: you can only open URLs returned by web_search or the client's own sites."
    with tenant_scope(ctx.tenant_id):
        async with get_db().reader() as s:
            doc = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.source == url))).scalars().first()
    if doc is None:
        try:
            title, text = await backend.fetch(url)
        except IntegrationError as e:
            ctx.event("research", "fetch_failed", url=url, code=e.code, error=e.message)
            return f"Error: could not open this page ({e.message}). Try another source."
        doc = await _store_page(ctx, url, title, text, "web_page", "fixture" if backend.synthetic else "web_fetch")
    st["allowed"].add(_norm(url))
    if url not in st["pages"]:
        st["pages"].append(url)
    if doc.quarantined:
        return {"url": url, "status": "quarantined",
                "text": "[content withheld: this page contained instructions aimed at AI systems]"}
    return {"url": url, "title": doc.title, "text": doc.content[:4000]}


RESEARCH_TOOLS = [
    Tool("web_search", "Search the web. Returns results with url, title and snippet.", SearchArgs, _web_search),
    Tool("fetch_page", "Fetch the readable text of a URL returned by web_search.", FetchArgs, _fetch_page),
]


async def ingest_website(tenant_id: str, ctx: AgentContext) -> str:
    """Read the client's own website once, so strategy is grounded in what the company says about itself."""
    intake = (await profile(tenant_id)).intake
    url = intake.get("website", "")
    backend = get_research()
    if not url or backend.synthetic:
        return "skipped: no website given" if not url else "skipped"
    try:
        title, text = await backend.fetch(url)
    except IntegrationError as e:
        return f"could not read {url}: {e.message}"
    await _store_page(ctx, url, title, text, "website", "web_fetch")
    return f"read {url}"


def _seen(ctx: AgentContext, url: str) -> bool:
    return bool(url) and _norm(url) in research_state(ctx)["allowed"]


async def _brief(tenant_id: str, ctx: AgentContext) -> tuple[dict, str]:
    intake = (await profile(tenant_id)).intake
    prime(ctx, intake)
    site = await docs(tenant_id, ["website"])
    brief = (f"Client: {intake.get('company_name')} ({intake.get('industry')}).\n"
             f"What they do: {intake.get('company')}\nTarget customers: {intake.get('target_customers')}\n"
             f"Website: {intake.get('website', '(not given)')}\n"
             f"Known competitors: {', '.join(intake.get('known_competitors', [])) or '(none given)'}\n"
             f"Geographies: {', '.join(intake.get('geographies', [])) or '(not specified)'}")
    if site:
        brief += f"\nFrom their own website: {site[0].content[:1200]}"
    return intake, brief


async def voice_analyst(tenant_id: str, ctx: AgentContext) -> VoiceProfile:
    intake = (await profile(tenant_id)).intake
    brief = (f"Client: {intake.get('company_name')} ({intake.get('industry')}).\n"
             f"What they do: {intake.get('company')}\nTarget customers: {intake.get('target_customers')}")
    samples = [d.content for d in await docs(tenant_id, ["founder_sample"])]
    if not samples:
        raise AgentError("no founder writing samples were provided; add at least 2 in Settings")
    current = await latest(tenant_id, "voice_profile")
    user = (f"{brief}\nFounder: {intake.get('founder_name')}, {intake.get('founder_title', '')}\n"
            f"Tone notes from intake: {intake.get('tone')}\n\nFounder's own posts:\n" +
            "\n---\n".join(samples) +
            (f"\n\nKeep these learned rules: {current.get('learned_rules')}" if current else ""))
    res = await structured("voice_analyst", "big",
                           "Analyse how this founder writes so a ghostwriter can match the voice exactly. "
                           "Be concrete (sentence length, structure, words they use and never use). "
                           "example_hooks must be short descriptions of hook PATTERNS, never copied sentences.",
                           user, VoiceProfile, ctx, cacheable=True, max_tokens=2000,
                           metadata={"intake": intake, "samples": samples,
                                     "learned_rules": (current or {}).get("learned_rules", [])})
    out = res.output
    if current and current.get("learned_rules"):
        out.learned_rules = list(dict.fromkeys(current["learned_rules"] + out.learned_rules))[:15]
    return out


def _require_research(ctx: AgentContext, agent: str) -> None:
    if not research_state(ctx)["searches"] and not research_state(ctx)["pages"]:
        raise IntegrationError("web_search", "unavailable",
                               f"{agent} finished without reading a single source, so nothing it says can be verified",
                               "Check the Integrations page (web search) and retry this step.")


async def market_analyst(tenant_id: str, ctx: AgentContext) -> MarketReport:
    intake, brief = await _brief(tenant_id, ctx)
    res = await run_agent("market_analyst", "big",
                          "You are a B2B market analyst. Research the client's industry: buyer segments, their pains, "
                          "and hard facts with sources. Every key_fact must carry the exact URL it came from, taken "
                          "from a search result or page you opened. Facts without a real URL will be discarded.",
                          brief, MarketReport, RESEARCH_TOOLS, ctx, metadata={"intake": intake}, max_tokens=2500)
    _require_research(ctx, "market_analyst")
    out, st = res.output, research_state(ctx)
    kept = []
    for f in out.key_facts:
        f.verified = _seen(ctx, f.source_url)
        if f.verified:
            kept.append(f)
        else:
            st["dropped"] += 1
    out.key_facts = kept
    return out


async def competitor_analyst(tenant_id: str, ctx: AgentContext) -> CompetitorMatrix:
    intake, brief = await _brief(tenant_id, ctx)
    res = await run_agent("competitor_analyst", "big",
                          "You are a competitive analyst. For each competitor find positioning, offerings, what they post "
                          "about and how often, strengths and weaknesses. Cite source_urls from pages or search results "
                          "you actually opened. Then list whitespace: content angles nobody owns. Stay factual and "
                          "neutral; describe weaknesses as gaps, never as insults.",
                          brief, CompetitorMatrix, RESEARCH_TOOLS, ctx, metadata={"intake": intake}, max_tokens=3000)
    _require_research(ctx, "competitor_analyst")
    out, st = res.output, research_state(ctx)
    kept = []
    for c in out.competitors:
        c.source_urls = [u for u in c.source_urls if _seen(ctx, u)]
        c.verified = bool(c.source_urls)
        if c.verified:
            kept.append(c)
        else:
            st["dropped"] += 1
    if not kept:
        raise AgentError("none of the competitors could be verified against a source. Add competitor websites in "
                         "Settings so they can be read directly, then retry.")
    out.competitors = kept
    return out


async def trend_scout(tenant_id: str, ctx: AgentContext) -> TrendReport:
    intake, brief = await _brief(tenant_id, ctx)
    market = await latest(tenant_id, "market_report")
    res = await run_agent("trend_scout", "big",
                          "You track market and content trends. Find current trends relevant to the client's buyers and "
                          "suggest a content angle for each. Each trend needs an evidence_url taken from a search result "
                          "or page you opened; trends without one are discarded.",
                          f"{brief}\nMarket summary: {compact(market, 800)}", TrendReport, RESEARCH_TOOLS, ctx,
                          metadata={"intake": intake}, max_tokens=2500)
    _require_research(ctx, "trend_scout")
    out, st = res.output, research_state(ctx)
    kept = []
    for t in out.trends:
        t.verified = _seen(ctx, t.evidence_url)
        if t.verified:
            kept.append(t)
        else:
            st["dropped"] += 1
    if not kept:
        raise AgentError("no trend could be tied to a real source")
    out.trends = kept
    return out
