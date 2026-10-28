"""Discovery agents: founder voice, market, competitors, trends.

Market, competitor and trend research is a bounded agent loop with batched actions (`gather_evidence`): at each step
the model sees a compact observation (result titles, URLs, short snippets, the opening of pages it read) and decides
which searches to run and which pages to read next, or that it has enough. Code runs those searches and reads in
parallel. One structured extraction then turns the evidence pack into the typed report. The context stays small
because no step ever re-reads raw pages, which is what made the old tool loop slow on per-minute token limits.

Rules enforced in code, not left to the prompt:
* search runs on a real provider or fails the node with the fix; there is no canned fallback;
* pages can only be read if a search returned them or the client gave them, through an SSRF-safe fetcher;
* every page passes the input guard before any model reads it;
* every claim is checked against the text of the source it cites: unsupported claims are not shown as facts (they
  are kept aside, flagged, for the UI), figures from three or more years ago are labelled outdated, and competitor
  claims backed by the competitor's own site are marked official. The artifact records its provenance.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.blackboard import compact, docs, latest, profile
from app.agents.runtime.loop import AgentContext, AgentError, Tool, structured, wrap_untrusted
from app.agents.schemas import CompetitorMatrix, MarketReport, TrendReport, VoiceProfile
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import incident
from app.db.usage import record_external
from app.db.models import KnowledgeDoc
from app.db.session import get_db
from app.guardrails.input_guard import MODEL_VISIBLE_CHARS, sanitize, scan
from app.integrations.errors import IntegrationError
from app.integrations.research import get_research


class SearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=200)


class FetchArgs(BaseModel):
    url: str = Field(max_length=500)


class ResearchStep(BaseModel):
    queries: list[str] = Field(default_factory=list, max_length=4, description="New web searches to run now (0-4)")
    read_urls: list[str] = Field(default_factory=list, max_length=4,
                                 description="URLs listed above to read in full now (0-4)")
    done: bool = Field(default=False, description="True when the evidence already covers the goal")


def _norm(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def research_state(ctx: AgentContext) -> dict:
    return ctx.call.extra.setdefault("research", {"allowed": set(), "hosts": set(), "searches": [], "pages": [],
                                                   "fallbacks": [], "dropped": 0, "synthetic": False,
                                                   "texts": {}, "snippets": {}, "answers": []})


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


# ---------------------------------------------------------------- search and read (code, not the model)
_SEARCH_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
SEARCH_CACHE_TTL_S = 12 * 3600


async def _web_search(args: SearchArgs, ctx: AgentContext, with_answer: bool = True):
    """One search, cached per client for 12 hours (a retried step or the next run does not pay for it again).

    `with_answer=False` drops the search engine's own summary without scanning it (research works from results and
    pages, so scanning a summary nobody reads would only cost classifier calls)."""
    st = research_state(ctx)
    key = (ctx.tenant_id, re.sub(r"\s+", " ", args.query.strip().lower()))
    hit = _SEARCH_CACHE.get(key)
    cached = bool(hit and time.time() - hit[0] < SEARCH_CACHE_TTL_S)
    res = hit[1] if cached else await get_research(ctx.tenant_id).search(args.query)   # IntegrationError aborts the node with the fix
    if not cached:
        _SEARCH_CACHE[key] = (time.time(), res)
        if not res.synthetic:
            await record_external(ctx.tenant_id, res.provider, "search", ctx.call.run_id, ctx.call.node_key)
    st["synthetic"] = st["synthetic"] or res.synthetic
    st["searches"].append({"query": args.query, "provider": res.provider, "results": len(res.hits), "cached": cached})
    for note in res.skipped:
        if note not in st["fallbacks"]:
            st["fallbacks"].append(note)
    answer = ""
    if res.answer and with_answer:
        verdict = await scan(res.answer, ctx.call)
        answer = "[withheld: flagged as prompt injection]" if verdict.quarantined else verdict.clean_text[:3000]
        if not verdict.quarantined:
            st["answers"].append(answer)
    results = []
    for h in res.hits[:8]:
        title, snippet = sanitize(h.title)[0][:150], sanitize(h.snippet)[0][:240]
        st["allowed"].add(_norm(h.url))
        st["snippets"][_norm(h.url)] = f"{title} {snippet}".strip()
        results.append({"title": title, "url": h.url, "snippet": snippet})
    return {"provider": res.provider, "answer": answer, "results": results}


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
    backend = get_research(ctx.tenant_id)
    url = args.url.strip()
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if not (backend.may_fetch(_norm(url), st["allowed"]) or _norm(url) in st["allowed"] or host in st["hosts"]):
        ctx.event("research", "tool_denied", tool="fetch_page", reason="url not allowlisted", url=url)
        return "Error: you can only open URLs returned by web_search or the client's own sites."
    with tenant_scope(ctx.tenant_id):
        async with get_db().reader() as s:
            doc = (await s.execute(select(KnowledgeDoc).where(KnowledgeDoc.source == url))).scalars().first()
    if doc is None:                                   # the website read at ingest (or by a sibling agent) is reused
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
    st["texts"][_norm(url)] = doc.content              # full text: for checking claims in code
    return {"url": url, "title": doc.title, "text": doc.content[:MODEL_VISIBLE_CHARS]}   # what a model may read


# Kept for tool-loop agents (and tests); the research agents below call the same functions from code.
RESEARCH_TOOLS = [
    Tool("web_search", "Search the web. Returns results with url, title and snippet.", SearchArgs, _web_search),
    Tool("fetch_page", "Fetch the readable text of a URL returned by web_search.", FetchArgs, _fetch_page),
]


STEP_SYSTEM = (
    "You direct a web research task. Goal: {goal}\n"
    "Each step you get what has been found so far. Choose up to 4 new searches and up to 4 URLs from the list to read "
    "in full. Prefer primary and official sources (a company's own site for its products, the original survey or report "
    "for a statistic) and recent material. Set done=true when the evidence covers the goal; reading the right pages "
    "matters more than many searches. Write each query as 3 to 8 plain words, the way a person types into a search "
    "engine: no quotes, no boolean operators, no site: filters."
)


def _observation(results: dict[str, dict], pages: dict[str, dict], queries: list[str], seeds: list[str]) -> str:
    lines = []
    if seeds:
        lines.append("Client-provided URLs you may read: " + ", ".join(seeds))
    if queries:
        lines.append("Searches already run: " + "; ".join(queries))
    if results:
        lines.append("Search results (url | title | snippet):")
        lines += [f"- {u} | {r['title'][:90]} | {r['snippet'][:160]}" for u, r in list(results.items())[:24]]
    if pages:
        lines.append("Pages already read (url: opening):")
        lines += [f"- {u}: {p['text'][:160]}" for u, p in pages.items()]
    return wrap_untrusted("research_so_far", "\n".join(lines) or "Nothing yet.")


async def gather_evidence(agent: str, goal: str, brief: str, ctx: AgentContext, seeds: list[str] | None = None,
                          max_steps: int = 3, max_reads: int = 6) -> dict:
    """The research loop: decide (model) -> search and read in parallel (code) -> observe, at most `max_steps` times."""
    seeds = [u for u in (seeds or []) if u]
    results: dict[str, dict] = {}
    pages: dict[str, dict] = {}
    queries: list[str] = []
    for step in range(1, max_steps + 1):
        decision = (await structured(
            f"{agent}_step", "research", STEP_SYSTEM.format(goal=goal),
            f"{brief}\n\n{_observation(results, pages, queries, seeds)}\nStep {step} of {max_steps}.",
            ResearchStep, ctx, temperature=0.2, max_tokens=500,
            metadata={"agent": agent, "step": step, "seeds": seeds, "queries_run": list(queries),
                      "results": list(results), "pages": list(pages)})).output
        seen_q = {q.lower() for q in queries}
        new_q = [q.strip()[:200] for q in decision.queries if len(q.strip()) >= 3 and q.strip().lower() not in seen_q][:4]
        to_read = [u.strip() for u in decision.read_urls if u.strip() and u.strip() not in pages][:max(0, max_reads - len(pages))][:4]
        ctx.event(agent, "research_step", step=step, queries=new_q, read=to_read, done=decision.done)
        if not new_q and not to_read:
            break
        searched = await asyncio.gather(*[_web_search(SearchArgs(query=q), ctx, with_answer=False) for q in new_q])
        queries += new_q
        for out in searched:
            for r in out["results"]:
                results.setdefault(r["url"], r)
        read = await asyncio.gather(*[_fetch_page(FetchArgs(url=u), ctx) for u in to_read])
        for u, out in zip(to_read, read):
            if isinstance(out, dict) and out.get("status") != "quarantined":
                pages[u] = {"title": out["title"], "text": out["text"]}
        if decision.done:
            break
    _require_research(ctx, agent)
    return {"results": results, "pages": pages}


def evidence_pack(ev: dict, page_chars: int = 1800, limit: int = 14000) -> tuple[str, list[dict]]:
    """What the extraction step reads: page excerpts first, then the remaining search snippets, inside untrusted tags."""
    blocks = [f"[PAGE] {u} | {p['title']}\n{p['text'][:page_chars]}" for u, p in ev["pages"].items()]
    blocks += [f"[RESULT] {u} | {r['title']} | {r['snippet']}" for u, r in ev["results"].items() if u not in ev["pages"]]
    text = "\n\n".join(blocks)[:limit]
    pages = [{"url": u, "title": p["title"], "text": p["text"]} for u, p in ev["pages"].items()]
    return wrap_untrusted("research", text), pages


# ---------------------------------------------------------------- claim verification (code)
_STOP = set("that this with from have will your their about which there these those been were into more most than also "
            "over such they them what when where while some many much very just only each other being does make made "
            "company companies".split())


def _content_tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z][a-z0-9-]{3,}", (text or "").lower()):
        if w not in _STOP:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[^\d.]", "", n).rstrip(".") for n in re.findall(r"\d[\d,.]*", text or "") if re.sub(r"\D", "", n)}


def source_text(ctx: AgentContext, url: str) -> str:
    st = research_state(ctx)
    n = _norm(url)
    return " ".join(x for x in (st["texts"].get(n, ""), st["snippets"].get(n, "")) if x)


def check_support(claim: str, text: str, min_overlap: float = 0.5) -> tuple[bool, str]:
    """Is `claim` backed by `text`? Every figure must appear in the source and most of its content words must too."""
    if not text:
        return False, "the cited page could not be read"
    missing = {n for n in _numbers(claim) if len(n) >= 1 and n not in _numbers(text)}
    years = {n for n in missing if len(n) == 4 and n.isdigit()}
    if missing - years:
        return False, f"figure(s) {', '.join(sorted(missing - years))} not found in the source"
    toks = _content_tokens(claim)
    if not toks:
        return True, ""
    ratio = len(toks & _content_tokens(text)) / len(toks)
    if ratio < min_overlap:
        return False, f"only {ratio:.0%} of the claim's key words appear in the source"
    return True, ""


def outdated_year(claim: str, text: str, now_year: int | None = None) -> int | None:
    """The year a statistic refers to, if it is three or more years old (claim first, then the source around it)."""
    now_year = now_year or datetime.now(timezone.utc).year
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20\d\d)\b", claim or "")]
    if not years and text:
        for n in [n for n in _numbers(claim) if not (len(n) == 4 and n.isdigit())][:3]:
            i = text.find(n)
            if i >= 0:
                years += [int(y) for y in re.findall(r"\b(19[89]\d|20\d\d)\b", text[max(0, i - 160):i + 160])]
    if years and max(years) <= now_year - 3:
        return max(years)
    return None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


# ---------------------------------------------------------------- agents
async def ingest_website(tenant_id: str, ctx: AgentContext) -> str:
    """Read the client's own website once, so strategy is grounded in what the company says about itself."""
    intake = (await profile(tenant_id)).intake
    url = intake.get("website", "")
    backend = get_research(tenant_id)
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
        brief += f"\nFrom their own website: {site[0].content[:900]}"
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
            "\n---\n".join(s[:1500] for s in samples) +
            (f"\n\nKeep these learned rules: {current.get('learned_rules')}" if current else ""))
    res = await structured("voice_analyst", "big",
                           "Analyse how this founder writes so a ghostwriter can match the voice exactly. "
                           "Be concrete (sentence length, structure, words they use and never use). "
                           "example_hooks must be short descriptions of hook PATTERNS, never copied sentences.",
                           user, VoiceProfile, ctx, cacheable=True, max_tokens=1400,
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


EXTRACT_RULES = ("Use only the evidence below. Every claim must name the exact URL it came from, taken from the evidence. "
                 "Copy figures exactly as the source states them, with the year they refer to when the source gives one. "
                 "Do not add illustrative or example numbers. Anything you cannot tie to a URL in the evidence is discarded.")


async def market_analyst(tenant_id: str, ctx: AgentContext) -> MarketReport:
    intake, brief = await _brief(tenant_id, ctx)
    ev = await gather_evidence("market_analyst", "the client's market: buyer segments, their pains, and hard facts "
                               "(market data, surveys, benchmarks) with sources", brief, ctx)
    pack, pages = evidence_pack(ev)
    res = await structured("market_analyst", "extract",
                           "You are a B2B market analyst. Summarise the client's market from the evidence: buyer segments, "
                           "their pains and key facts. " + EXTRACT_RULES,
                           f"{brief}\n\nEvidence:\n{pack}", MarketReport, ctx, max_tokens=2000,
                           metadata={"intake": intake, "pages": pages})
    out, st = res.output, research_state(ctx)
    kept, unverified = [], []
    for f in out.key_facts:
        ok, why = (check_support(f.claim, source_text(ctx, f.source_url)) if _seen(ctx, f.source_url)
                   else (False, "the cited URL was never returned by a search or read"))
        if not ok:
            st["dropped"] += 1
            unverified.append({"claim": f.claim, "source_url": f.source_url, "reason": why})
            continue
        f.verified = True
        f.official = _host(f.source_url) in st["hosts"]
        year = outdated_year(f.claim, source_text(ctx, f.source_url))
        f.status, f.note = ("outdated", f"refers to {year}; may no longer hold") if year else ("verified", "")
        kept.append(f)
    out.key_facts = kept
    ctx.call.extra["unverified"] = unverified
    return out


def _official_domains(intake: dict) -> dict[str, str]:
    """Competitor name -> its own domain, from the client's onboarding answers."""
    names, urls = intake.get("known_competitors", []), intake.get("competitor_urls", [])
    out = {}
    for u in urls:
        host = _host(u)
        root = host.split(".")[0].replace("-", "")
        name = next((n for n in names if n.lower().replace(" ", "").startswith(root[:5])), None)
        out[(name or root).lower()] = host
    return out


async def competitor_analyst(tenant_id: str, ctx: AgentContext) -> CompetitorMatrix:
    intake, brief = await _brief(tenant_id, ctx)
    seeds = list(intake.get("competitor_urls", []))
    ev = await gather_evidence("competitor_analyst", "each competitor's positioning, offerings, what they post about "
                               "and how often, strengths and weaknesses. Read each competitor's own website first "
                               "(official source for what they offer), then independent coverage", brief, ctx,
                               seeds=seeds, max_reads=7)
    pack, pages = evidence_pack(ev, page_chars=1400, limit=13000)
    res = await structured("competitor_analyst", "extract",
                           "You are a competitive analyst. For each competitor give positioning, offerings, content "
                           "themes, posting cadence, strengths and weaknesses, citing source_urls from the evidence. "
                           "Product capabilities must come from the competitor's own site when it is in the evidence. "
                           "Then list whitespace: content angles nobody owns. Stay factual and neutral; describe "
                           "weaknesses as gaps, never as insults. " + EXTRACT_RULES,
                           f"{brief}\n\nEvidence:\n{pack}", CompetitorMatrix, ctx, max_tokens=2300,
                           metadata={"intake": intake, "pages": pages})
    out, st = res.output, research_state(ctx)
    official = _official_domains(intake)
    kept, unverified = [], []
    for c in out.competitors:
        c.source_urls = [u for u in c.source_urls if _seen(ctx, u)]
        if not c.source_urls:
            st["dropped"] += 1
            unverified.append({"claim": f"{c.name}: {c.positioning}", "source_url": "", "reason": "no source was seen"})
            continue
        text = " ".join(source_text(ctx, u) for u in c.source_urls)
        ok, why = check_support(" ".join([c.positioning, *c.offerings]), text, min_overlap=0.4)
        own = official.get(c.name.lower()) or next((h for n, h in official.items() if n in c.name.lower()), "")
        c.official_source = any(_host(u).endswith(own) for u in c.source_urls) if own else \
            any(re.sub(r"\W", "", c.name.lower())[:6] in _host(u).replace("-", "") for u in c.source_urls)
        c.verified = ok
        c.note = "" if ok else f"positioning and offerings not confirmed by the cited pages ({why})"
        kept.append(c)
    if not kept:
        raise AgentError("none of the competitors could be verified against a source. Add competitor websites in "
                         "Settings so they can be read directly, then retry.")
    out.competitors = sorted(kept, key=lambda c: (not c.verified, not c.official_source))
    ctx.call.extra["unverified"] = unverified
    return out


async def trend_scout(tenant_id: str, ctx: AgentContext) -> TrendReport:
    intake, brief = await _brief(tenant_id, ctx)
    market = await latest(tenant_id, "market_report")
    brief = f"{brief}\nMarket summary: {compact(market, 600)}"
    ev = await gather_evidence("trend_scout", "current trends (this year and last) relevant to the client's buyers, "
                               "each backed by a recent source", brief, ctx)
    pack, pages = evidence_pack(ev)
    res = await structured("trend_scout", "extract",
                           "You track market and content trends. From the evidence, list current trends relevant to the "
                           "client's buyers and suggest a content angle for each. Each trend needs the evidence_url it "
                           "came from. " + EXTRACT_RULES,
                           f"{brief}\n\nEvidence:\n{pack}", TrendReport, ctx, max_tokens=1800,
                           metadata={"intake": intake, "pages": pages})
    out, st = res.output, research_state(ctx)
    kept, unverified = [], []
    for t in out.trends:
        ok, why = (check_support(t.summary, source_text(ctx, t.evidence_url), min_overlap=0.4)
                   if _seen(ctx, t.evidence_url) else (False, "the cited URL was never returned by a search or read"))
        if not ok:
            st["dropped"] += 1
            unverified.append({"claim": f"{t.name}: {t.summary}", "source_url": t.evidence_url, "reason": why})
            continue
        t.verified = True
        year = outdated_year(t.summary, source_text(ctx, t.evidence_url))
        t.note = f"evidence refers to {year}; may no longer be current" if year else ""
        kept.append(t)
    if not kept:
        raise AgentError("no trend could be tied to a real source")
    out.trends = kept
    ctx.call.extra["unverified"] = unverified
    return out
