"""Discovery agents: founder voice, market, competitors, trends.

Market/competitor/trend analysts are tool-using loops over web_search and
fetch_page. Every fetched page goes through the input guard and is stored
(clean) in the tenant's knowledge base, so later agents can cite it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.data.web_corpus import PAGES
from app.adapters.sources import get_research
from app.agents.blackboard import compact, docs, latest, profile
from app.agents.runtime.loop import AgentContext, Tool, run_agent, structured
from app.agents.schemas import CompetitorMatrix, MarketReport, TrendReport, VoiceProfile
from app.core.tenancy import tenant_scope
from app.db.audit import incident
from app.db.models import KnowledgeDoc
from app.db.session import get_db
from app.guardrails.input_guard import scan, sanitize


class SearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=200)


class FetchArgs(BaseModel):
    url: str = Field(max_length=500)


async def _web_search(args: SearchArgs, ctx: AgentContext):
    res = await get_research().search(args.query)
    if res["provider"] == "gemini_search":
        verdict = await scan(res["answer"], ctx.call)
        if verdict.quarantined:
            return {"provider": "gemini_search", "answer": "[withheld: flagged as prompt injection]", "sources": res["sources"]}
        return {"provider": "gemini_search", "answer": verdict.clean_text[:3000], "sources": res["sources"][:8]}
    return {"provider": "seeded_corpus",
            "results": [dict(r, snippet=sanitize(r["snippet"])[0]) for r in res["results"]]}


async def _fetch_page(args: FetchArgs, ctx: AgentContext):
    corpus = get_research().corpus
    if not corpus.allowed(args.url):  # tool guard: domain/URL allowlist
        ctx.event("research", "tool_denied", tool="fetch_page", reason="url not allowlisted", url=args.url)
        return "Error: this URL is not on the research allowlist."
    tid = ctx.tenant_id
    with tenant_scope(tid):
        async with get_db().reader() as s:
            existing = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.source == args.url))).scalars().first()
    if existing is None:
        raw = corpus.fetch(args.url) or ""
        verdict = await scan(raw, ctx.call)
        title = next((p["title"] for p in PAGES if p["url"] == args.url), args.url)
        with tenant_scope(tid):
            async with get_db().session() as s:
                # Parallel agents may fetch the same URL. Writes run under BEGIN IMMEDIATE,
                # so re-checking inside this write transaction is race-free.
                existing = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.source == args.url))).scalars().first()
                if existing is None:
                    existing = KnowledgeDoc(kind="web_page", source=args.url, title=title, content=verdict.clean_text,
                                            trusted=False, injection_score=verdict.score, quarantined=verdict.quarantined)
                    s.add(existing)
                    await s.flush()
                    if verdict.quarantined:
                        incident(s, "input", "prompt_injection", "critical",
                                 f"quarantined {args.url}: {'; '.join(verdict.reasons)}", "knowledge_doc", existing.id,
                                 run_id=ctx.call.run_id)
                    elif verdict.hidden_injection:
                        incident(s, "input", "hidden_injection_stripped", "high",
                                 f"{args.url}: {'; '.join(verdict.reasons)}", "knowledge_doc", existing.id,
                                 run_id=ctx.call.run_id)
    if existing.quarantined:
        return {"url": args.url, "status": "quarantined",
                "text": "[content withheld: this page contained instructions aimed at AI systems]"}
    return {"url": args.url, "title": existing.title, "text": existing.content[:4000]}


RESEARCH_TOOLS = [
    Tool("web_search", "Search the web. Returns results with url, title and snippet.", SearchArgs, _web_search),
    Tool("fetch_page", "Fetch the readable text of a URL returned by web_search.", FetchArgs, _fetch_page),
]


async def _brief(tenant_id: str) -> tuple[dict, str]:
    intake = (await profile(tenant_id)).intake
    brief = (f"Client: {intake.get('company_name')} ({intake.get('industry')}).\n"
             f"What they do: {intake.get('company')}\nTarget customers: {intake.get('target_customers')}\n"
             f"Known competitors: {', '.join(intake.get('known_competitors', []))}")
    return intake, brief


async def voice_analyst(tenant_id: str, ctx: AgentContext) -> VoiceProfile:
    intake, brief = await _brief(tenant_id)
    samples = [d.content for d in await docs(tenant_id, ["founder_sample"])]
    current = await latest(tenant_id, "voice_profile")
    user = (f"{brief}\nFounder: {intake.get('founder_name')}, {intake.get('founder_title', '')}\n"
            f"Tone notes from intake: {intake.get('tone')}\n\nFounder's own posts:\n" +
            "\n---\n".join(samples) +
            (f"\n\nKeep these learned rules: {current.get('learned_rules')}" if current else ""))
    res = await structured("voice_analyst", "big",
                           "Analyse how this founder writes so a ghostwriter can match the voice exactly. "
                           "Be concrete (sentence length, structure, words they use and never use).",
                           user, VoiceProfile, ctx, cacheable=True, max_tokens=2000,
                           metadata={"intake": intake, "samples": samples,
                                     "learned_rules": (current or {}).get("learned_rules", [])})
    out = res.output
    if current and current.get("learned_rules"):
        out.learned_rules = list(dict.fromkeys(current["learned_rules"] + out.learned_rules))[:15]
    return out


async def market_analyst(tenant_id: str, ctx: AgentContext) -> MarketReport:
    intake, brief = await _brief(tenant_id)
    res = await run_agent("market_analyst", "big",
                          "You are a B2B market analyst. Research the client's industry: buyer segments, their pains, "
                          "and hard facts with sources. Every key_fact must carry the URL it came from.",
                          brief, MarketReport, RESEARCH_TOOLS, ctx, metadata={"intake": intake}, max_tokens=2500)
    return res.output


async def competitor_analyst(tenant_id: str, ctx: AgentContext) -> CompetitorMatrix:
    intake, brief = await _brief(tenant_id)
    res = await run_agent("competitor_analyst", "big",
                          "You are a competitive analyst. For each competitor find positioning, offerings, what they post "
                          "about and how often, strengths and weaknesses. Then list whitespace: content angles nobody owns. "
                          "Stay factual and neutral; describe weaknesses as gaps, never as insults.",
                          brief, CompetitorMatrix, RESEARCH_TOOLS, ctx, metadata={"intake": intake}, max_tokens=3000)
    return res.output


async def trend_scout(tenant_id: str, ctx: AgentContext) -> TrendReport:
    intake, brief = await _brief(tenant_id)
    market = await latest(tenant_id, "market_report")
    res = await run_agent("trend_scout", "big",
                          "You track market and content trends. Find current trends relevant to the client's buyers and "
                          "suggest a content angle for each. Each trend needs an evidence URL.",
                          f"{brief}\nMarket summary: {compact(market, 800)}", TrendReport, RESEARCH_TOOLS, ctx,
                          metadata={"intake": intake}, max_tokens=2500)
    return res.output
