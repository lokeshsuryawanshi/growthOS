"""Live lead discovery on the open web, with the operator's own search key. Hunter is an optional extra.

discover():       query families (the model writes queries, code guarantees every family) -> web search (code)
                  -> companies named in results (model, each traced back to a result) + companies linked from list
                  pages (code reads the links) -> each company's own homepage (what it does, in its own words).
contact_route():  for companies that qualified, in this order: (a) a named person and role on the company's own
                  about/team/press page, with that page as evidence; (b) an email address published on the company's
                  own site, personal or a role inbox; (c) a LinkedIn profile URL as returned by search (linkedin.com is
                  never fetched); (d) Hunter.io, only if the operator saved a key. Email addresses are never guessed.
Every candidate that is dropped is counted with its reason (the funnel); searches and page reads are bounded per run.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.agents.blackboard import compact
from app.agents.research.agents import SearchArgs, _norm, _web_search, research_state
from app.agents.runtime.loop import AgentContext, structured, wrap_untrusted
from app.guardrails.input_guard import scan
from app.integrations.errors import IntegrationError
from app.integrations.fetch import fetch_page
from app.integrations.hunter import clean_domain

FAMILIES = {
    "hiring": "companies hiring for the job the client's product or service does (job posts reveal the pain)",
    "funding": "recent funding, launches or expansion in the target segment and region",
    "directory": "directories, marketplaces or accelerator and investor portfolios listing such companies",
    "top_list": "'top' or 'best' lists of companies in the segment and region",
    "events": "speakers, exhibitors or sponsors of the segment's conferences and meetups",
    "community": "communities, associations or member lists for the segment",
    "competitor_customers": "customer stories, case studies or 'customers' pages on the competitors' own sites",
}
NOT_COMPANIES = {"linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org",
                 "crunchbase.com", "g2.com", "capterra.com", "glassdoor.com", "indeed.com", "zoominfo.com", "reddit.com",
                 "medium.com", "github.com", "forbes.com", "techcrunch.com", "bloomberg.com", "reuters.com", "gartner.com",
                 "google.com", "apple.com", "microsoft.com", "amazon.com", "wellfound.com", "angel.co", "producthunt.com",
                 "substack.com", "wordpress.com", "tiktok.com", "pinterest.com", "quora.com", "yelp.com", "clutch.co",
                 "trustpilot.com", "builtin.com", "f6s.com", "tracxn.com", "pitchbook.com", "cbinsights.com",
                 "businesswire.com", "prnewswire.com", "globenewswire.com", "eventbrite.com", "meetup.com", "lu.ma",
                 "w3.org", "cloudflare.com", "gstatic.com", "googleapis.com", "bit.ly", "t.co", "goo.gl", "apps.apple.com",
                 "play.google.com", "vimeo.com", "spotify.com", "hubspot.com", "mailchimp.com", "typeform.com"}
LISTY = re.compile(r"(?i)\b(top \d*|best|leading|list|directory|portfolio|companies|startups|sponsors|exhibitors|"
                   r"speakers|members|customers|case stud\w*|vendors|agencies|brands|firms|providers|founders)\b")
GENERIC_ANCHOR = re.compile(r"(?i)^(website|visit|visit (?:the )?website|read more|learn more|more|home|here|click here|"
                            r"link|view|view profile|details|apply|sign up|log ?in|contact|contact us|about|about us|"
                            r"our story|team|blog|careers|\W*)$")
CONTACT_PAGE = re.compile(r"(?i)/(about|team|people|leadership|founders?|company|who-we-are|our-story|management|"
                          r"contact|press|news|media)(?:[-/_.]|$)")
EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
ROLE_INBOX = {"info", "hello", "hi", "contact", "sales", "team", "press", "media", "partnerships", "partners",
              "business", "founders", "office", "enquiries", "inquiries", "enquiry", "inquiry", "support", "marketing",
              "bd", "growth", "general", "admin", "connect", "studio", "projects", "project", "work", "newbusiness",
              "new", "ask", "talk", "start", "go", "help", "careers", "jobs", "mail", "contactus", "letstalk"}
CCTLD = {"in": "india", "uk": "uk", "de": "germany", "fr": "france", "es": "spain", "it": "italy", "nl": "netherlands",
         "sg": "singapore", "au": "australia", "ca": "canada", "jp": "japan", "br": "brazil", "se": "sweden",
         "ch": "switzerland", "ie": "ireland", "nz": "new zealand", "ae": "uae", "za": "south africa", "mx": "mexico"}
COUNTRY_ALIASES = {"uk": {"uk", "united kingdom", "britain", "great britain", "england", "scotland", "wales"},
                   "us": {"us", "usa", "united states", "america", "united states of america"},
                   "uae": {"uae", "united arab emirates", "dubai"}}
REGIONS = {"europe": {"uk", "germany", "france", "spain", "italy", "netherlands", "sweden", "switzerland", "ireland",
                      "portugal", "poland", "belgium", "austria", "denmark", "norway", "finland"},
           "dach": {"germany", "austria", "switzerland"}, "north america": {"us", "canada", "mexico"},
           "apac": {"india", "singapore", "australia", "japan", "new zealand", "indonesia", "malaysia", "philippines",
                    "vietnam", "thailand", "south korea"}, "asia": {"india", "singapore", "japan", "indonesia", "malaysia"},
           "emea": {"uk", "germany", "france", "spain", "italy", "netherlands", "uae", "south africa", "ireland"}}


def _country(name: str) -> str:
    n = name.strip().lower()
    return next((k for k, v in COUNTRY_ALIASES.items() if n in v), n)


@dataclass
class SearchBudget:
    """Bounded per run, and reported: searches cost the operator credits; page reads cost time."""
    max_searches: int = 24
    max_fetches: int = 60
    searches: int = 0
    fetches: int = 0

    def take_search(self) -> bool:
        if self.searches >= self.max_searches:
            return False
        self.searches += 1
        return True

    def take_fetch(self) -> bool:
        if self.fetches >= self.max_fetches:
            return False
        self.fetches += 1
        return True


def funnel_stats() -> dict:
    return {"queries": 0, "searches": 0, "families": {}, "list_pages_read": 0, "found_in_results": 0,
            "found_on_lists": 0, "homepages_read": 0, "pages_read": 0}


# ---------------------------------------------------------------- 1. queries
class FamilyQuery(BaseModel):
    family: str = Field(description="one of: " + ", ".join(FAMILIES))
    query: str = Field(description="3 to 8 plain words, as a person types into a search engine")


class LeadPlan(BaseModel):
    queries: list[FamilyQuery] = Field(min_length=1, max_length=14)


def _region(intake: dict, icp: dict) -> str:
    geos = intake.get("geographies") or icp.get("geographies") or []
    return geos[0] if geos else ""


def fallback_queries(icp: dict, intake: dict) -> dict[str, str]:
    """Plain queries per family when the model skips one: every family is always searched."""
    seg = next((s.get("name") for s in icp.get("segments", []) if s.get("name")), "") or intake.get("target_customers", "")
    seg = " ".join(re.findall(r"[A-Za-z0-9&+-]+", seg)[:5])
    region = _region(intake, icp)
    comp = (intake.get("known_competitors") or [""])[0]
    return {"hiring": f"{seg} hiring {region}", "funding": f"{seg} raised funding {region}",
            "directory": f"{seg} directory {region}", "top_list": f"top {seg} {region}",
            "events": f"{seg} conference sponsors {region}", "community": f"{seg} association members {region}",
            "competitor_customers": f"{comp} customer case study" if comp else f"{seg} case study {region}"}


async def plan_queries(ctx: AgentContext, icp: dict, intake: dict, round_no: int, broaden: bool,
                       seen: set[str]) -> list[tuple[str, str]]:
    wider = (" This is a retry with a wider net: include neighbouring regions, adjacent industries and a wider "
             "company-size range." if broaden or round_no > 1 else "")
    res = await structured(
        "company_scout_plan", "small",
        "Write web search queries that surface COMPANIES that could buy from the client. Use these families, at least "
        "one query each: " + "; ".join(f"{k} = {v}" for k, v in FAMILIES.items()) + ". Each query is 3 to 8 plain "
        "words: no quotes, no boolean operators, no site: filters. Name the region when the client targets one. If the "
        "client sells to consumers, look for the businesses that serve or partner with those consumers.",
        f"Client: {intake.get('company_name', '')} - {intake.get('company', '')[:300]}\nTarget customers: "
        f"{intake.get('target_customers', '')[:300]}\nRegions: {', '.join(intake.get('geographies', [])) or 'any'}\n"
        f"Competitors: {', '.join(intake.get('known_competitors', [])) or 'none given'}\nICP: {compact(icp, 1500)}\n"
        f"Round {round_no}.{wider}\nAlready seen (skip): {sorted(seen)[:20]}",
        LeadPlan, ctx, max_tokens=700, metadata={"icp": icp, "round": round_no})
    picked: dict[str, list[str]] = {}
    for q in res.output.queries:
        fam = q.family.strip().lower().replace(" ", "_").replace("-", "_")
        if fam in FAMILIES and q.query.strip():
            picked.setdefault(fam, []).append(q.query.strip()[:120])
    fb = fallback_queries(icp, intake)
    out = []
    for fam in FAMILIES:                             # every family, the model's query first
        qs = picked.get(fam) or [fb[fam]]
        out += [(fam, q) for q in qs[: 2 if round_no == 1 else 1]]
    return out


# ---------------------------------------------------------------- 2. companies named in results, or listed on a page
class Candidate(BaseModel):
    name: str
    domain: str = Field(description="the company's own website domain, e.g. acme.com")
    evidence_url: str = Field(description="the result URL that names the company")
    signal: str = Field(default="", description="one concrete fact from that result, in its words, or empty")
    why_fit: str = ""
    country: str = Field(default="", description="the country, if a result says it; else empty")


class Candidates(BaseModel):
    companies: list[Candidate] = Field(default_factory=list, max_length=30)


def company_domain(d: str) -> str | None:
    dom = clean_domain(d or "")
    if not dom:
        return None
    root = ".".join(dom.split(".")[-2:])
    if dom in NOT_COMPANIES or root in NOT_COMPANIES:
        return None
    return dom


def mine_list_links(page_url: str, title: str, links: list[tuple[str, str]]) -> list[dict]:
    """Companies linked from a list page, read from the HTML itself (no model): domain and anchor text."""
    host = (urlparse(page_url).hostname or "").removeprefix("www.")
    out, seen = [], set()
    for href, anchor in links:
        if not href.startswith("http"):
            continue
        dom = company_domain(urlparse(href).hostname or "")
        if not dom or dom == host or dom.endswith("." + host) or host.endswith("." + dom) or dom in seen:
            continue
        name = anchor if anchor and not GENERIC_ANCHOR.match(anchor) and len(anchor) <= 60 else ""
        name = name or dom.split(".")[0].replace("-", " ").title()
        seen.add(dom)
        out.append({"name": name, "domain": dom, "source_url": page_url,
                    "source_snippet": f'Listed on "{title[:120]}"'})
    return out[:40]


async def discover(ctx: AgentContext, icp: dict, intake: dict, need: int, round_no: int, broaden: bool,
                   seen_domains: set[str], budget: SearchBudget, f: dict) -> list[dict]:
    st = research_state(ctx)
    st.setdefault("snippets", {})
    st.setdefault("answers", [])
    asked: set[str] = ctx.call.extra.setdefault("lead_queries", set())
    queries = []
    for fam, q in await plan_queries(ctx, icp, intake, round_no, broaden, seen_domains):
        k = " ".join(q.lower().split())
        if k not in asked:                           # a query already run in this run would only repeat itself
            asked.add(k)
            queries.append((fam, q))
    failures: list[IntegrationError] = []

    async def one(fam: str, q: str):
        if not budget.take_search():
            return None
        try:
            r = await _web_search(SearchArgs(query=q), ctx, with_answer=False)
            return fam, r
        except IntegrationError as e:
            if e.code in ("not_configured", "auth_failed", "quota_exceeded"):
                raise
            failures.append(e)
            return None
    results = [r for r in await asyncio.gather(*[one(fam, q) for fam, q in queries]) if r]
    f["queries"] += len(queries)
    f["searches"] = budget.searches
    for fam, r in results:
        f["families"][fam] = f["families"].get(fam, 0) + len(r["results"])
    if not results and failures:
        raise IntegrationError("web_search", "unavailable", "every lead search failed: " + failures[0].message,
                               "Retry in a few minutes.")
    hits, urls = [], set()
    for _, r in results:                             # the same page often answers several queries
        for h in r["results"]:
            if _norm(h["url"]) not in urls:
                urls.add(_norm(h["url"]))
                hits.append(h)
    known = " ".join((h["url"] + " " + h["title"] + " " + h["snippet"]).lower() for h in hits)
    cands: dict[str, dict] = {}

    # (a) companies named in the results, each traced back to a result
    if hits:
        blocks = "\n".join(f"- {h['title']} | {h['url']} | {h['snippet']}" for h in hits)[:9000]
        res = await structured(
            "company_scout", "extract",
            "From these web search results, list real companies that could buy from the client. Only include a company "
            "a result names. domain must be the company's own website; evidence_url must be one of the result URLs; "
            "signal, if any, one concrete fact from that result in its words. Return companies, not publishers, "
            "directories or articles, and never the client itself or its competitors.",
            f"Client: {intake.get('company_name', '')}\nICP: {compact(icp, 1500)}\n<untrusted_data source=\"search\">\n"
            f"{blocks}\n</untrusted_data>\nReturn up to {min(30, need * 2)} companies.",
            Candidates, ctx, temperature=0.1, max_tokens=2000, metadata={"icp": icp, "round": round_no})
        for c in res.output.companies:
            dom = company_domain(c.domain)
            if not dom or dom in seen_domains or dom in cands:
                continue
            if _norm(c.evidence_url) not in st["allowed"] or (dom not in known and c.name.lower() not in known):
                f.setdefault("dropped", {}).setdefault("not_traced_to_a_result", 0)
                f["dropped"]["not_traced_to_a_result"] += 1
                continue
            sig = c.signal.strip()
            support = st["snippets"].get(_norm(c.evidence_url), "")
            if sig and len(set(re.findall(r"[a-z0-9]{4,}", sig.lower())) & set(re.findall(r"[a-z0-9]{4,}", support.lower()))) < 2:
                sig = ""
            cands[dom] = {"name": c.name.strip()[:200], "domain": dom, "source_url": c.evidence_url, "signal": sig,
                          "why_fit": c.why_fit[:300], "country": c.country.strip().lower(), "source_snippet": support[:300]}
        f["found_in_results"] += len(cands)

    # (b) companies linked from list pages (directories, top lists, sponsors, portfolios)
    listy = [h for h in hits if LISTY.search(h["title"] + " " + h["url"]) and company_domain(urlparse(h["url"]).hostname or "") != ""]
    on_lists = 0
    for h in listy[:4]:
        if "linkedin.com" in h["url"] or not budget.take_fetch():
            continue
        try:
            page = await fetch_page(h["url"])
        except IntegrationError:
            continue
        f["list_pages_read"] += 1
        for m in mine_list_links(h["url"], page.title, page.links):
            if m["domain"] not in cands and m["domain"] not in seen_domains:
                cands[m["domain"]] = {**m, "signal": "", "why_fit": "", "country": ""}
                on_lists += 1
    f["found_on_lists"] += on_lists
    return list(cands.values())


# ---------------------------------------------------------------- 3. the company's own homepage
async def read_homepages(ctx: AgentContext, cands: list[dict], budget: SearchBudget, f: dict) -> None:
    """What each company says about itself, and the links to its about/team/contact pages. Scanned for injection
    before any model reads it."""
    sem = asyncio.Semaphore(6)

    async def one(c: dict):
        if not budget.take_fetch():
            return
        async with sem:
            try:
                page = await fetch_page("https://" + c["domain"])
            except IntegrationError:
                return
        verdict = await scan(page.text[:1800], ctx.call)
        c["about"] = "" if verdict.quarantined else re.sub(r"\s+", " ", f"{page.title}. {verdict.clean_text}")[:500]
        c["name"] = better_name(c["name"], c["domain"], page.title)
        c["_links"] = page.links
        c["_home_text"] = page.text[:20000]
        c["_home_url"] = page.final_url
        f["homepages_read"] += 1
    await asyncio.gather(*[one(c) for c in cands])


def _root(domain: str) -> str:
    parts = domain.split(".")
    return parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac") else parts[-2] if len(parts) >= 2 else domain


def better_name(name: str, domain: str, title: str) -> str:
    """A list page's link label is often not the company's name ("catalogue of works"). Keep it only if it matches
    the domain; otherwise use the part of the company's own page title that does, or the domain itself."""
    root = re.sub(r"[^a-z0-9]", "", _root(domain).lower())

    def matches(text: str) -> bool:
        t = re.sub(r"[^a-z0-9]", "", text.lower())
        return bool(t) and (t in root or root in t or (len(t) >= 4 and t[:4] == root[:4]))
    if matches(name) and not re.fullmatch(r"(?i)(?:www\.)?[a-z0-9-]+(?:\.[a-z]{2,})+", name.strip()):
        return name                                  # a bare domain ("render.com") is not a name
    for seg in re.split(r"\s[|\-–—:·]\s|\s[|–—·]|[|–—·]\s", title or ""):
        seg = seg.strip()
        if seg and len(seg) <= 60 and matches(seg):
            return seg
    return _root(domain).replace("-", " ").title()


def region_mismatch(c: dict, targets: list[str]) -> bool:
    """True only when the evidence clearly puts the company in a country outside every target region. Unknown is not
    a mismatch, and a region ("Europe", "APAC") counts only when the client named it."""
    want = {t.strip().lower() for t in targets if t.strip()}
    if not want or want & {"global", "worldwide", "any", "anywhere", "international"}:
        return False
    accepted: set[str] = set()
    for t in want:
        accepted |= REGIONS.get(t, set()) | {_country(t)}
    tld = c["domain"].rsplit(".", 1)[-1]
    country = _country(c.get("country") or CCTLD.get(tld, ""))
    return bool(country) and country not in accepted


# ---------------------------------------------------------------- 4. a way to reach a decision-maker
class Person(BaseModel):
    name: str
    role: str
    page_url: str = Field(description="the page this person is named on")


class People(BaseModel):
    people: list[Person] = Field(default_factory=list, max_length=8)


SENIOR = re.compile(r"(?i)\b(founder|co-?founder|ceo|chief|president|owner|managing director|md|head of|vp|"
                    r"vice president|director|partner|cmo|cto|coo|cro|principal|general manager)\b")


def clean_name(name: str) -> str:
    """Letters, spaces, hyphens, apostrophes and dots only (no emoji or decorations), at most 200 characters."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.'\-]", "", name, flags=re.UNICODE)).strip()[:200]


def works_here(role: str, company: str, domain: str) -> bool:
    """False when the role names another organisation ("Director at JLL", "Co-Founder @ Floresco"): a testimonial or
    a partner, not someone at this company."""
    m = re.search(r"(?i)(?:\s@\s?|\sat\s|,\s*)([A-Z][\w&.-]+(?:\s[A-Z][\w&.-]+)*)\s*$", role.strip())
    if not m:
        return not re.search(r"\b[A-Z]{2,5}\b$", role.strip()) or company.lower().startswith(role.strip().split()[-1].lower())
    other = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
    mine = re.sub(r"[^a-z0-9]", "", company.lower()) + " " + re.sub(r"[^a-z0-9]", "", _root(domain).lower())
    return bool(other) and (other in mine or mine.split()[0] in other)


def site_emails(text: str, links: list[tuple[str, str]], domain: str) -> list[str]:
    """Addresses the company itself publishes on its own pages: on its own domain only, as written, never made up."""
    found = []
    for href, _ in links:
        if href.lower().startswith("mailto:"):
            found.append(href[7:].split("?")[0])
    found += [m.group(0) for m in EMAIL.finditer(text)]
    out = []
    for e in found:
        e = e.strip().strip(".,;:").lower()
        if "@" not in e:
            continue
        host = e.split("@", 1)[1]
        if (host == domain or host.endswith("." + domain)) and e not in out and not re.search(r"\.(png|jpe?g|gif|svg|webp)$", e):
            out.append(e)
    return out


def is_role_inbox(email: str) -> bool:
    return re.split(r"[._+-]", email.split("@", 1)[0])[0] in ROLE_INBOX


def _person_email(person: str, emails: list[str]) -> str:
    parts = [p for p in re.findall(r"[a-z]+", person.lower()) if len(p) >= 2]
    for e in emails:
        local = e.split("@", 1)[0]
        if not is_role_inbox(e) and parts and (parts[0] in local or (len(parts) > 1 and parts[-1] in local)):
            return e
    return ""


def linkedin_from_results(results: list[dict], company: str) -> tuple[str, str, str] | None:
    """(name, title, profile URL) from a search result that is a LinkedIn profile naming the company. Only what the
    search returned: the profile page itself is never fetched."""
    comp = company.lower()
    for h in results:
        u = h["url"]
        if not re.match(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+", u):
            continue
        title = h["title"]
        if comp not in (title + " " + h.get("snippet", "")).lower():
            continue
        bits = [b.strip() for b in re.split(r"\s[-|–]\s", title.replace(" | LinkedIn", "")) if b.strip()]
        name = bits[0] if bits else ""
        role = bits[1] if len(bits) > 2 else ""
        if name and len(name.split()) <= 5:
            return name, role, u.split("?")[0]
    return None


async def contact_route(ctx: AgentContext, c: dict, titles: list[str], budget: SearchBudget, f: dict,
                        hunter=None) -> dict:
    """Fill person, title, email, linkedin_url, contact_source, contact_url and contact_confidence on a candidate."""
    domain = c["domain"]
    pages: list[tuple[str, str]] = []                    # (url, text) from the company's own site
    if c.get("_home_text"):
        pages.append((c.get("_home_url") or "https://" + domain, c["_home_text"]))
    links = list(c.get("_links") or [])
    wanted = []
    for href, anchor in links:
        host = (urlparse(href).hostname or "").removeprefix("www.")
        if href.startswith("http") and (host == domain or host.endswith("." + domain)) and \
                (CONTACT_PAGE.search(urlparse(href).path + "/") or re.search(r"(?i)about|team|contact|people|leadership|press", anchor)):
            if href.split("#")[0] not in wanted:
                wanted.append(href.split("#")[0])
    for href in wanted[:3]:
        if not budget.take_fetch():
            break
        try:
            p = await fetch_page(href)
        except IntegrationError:
            continue
        f["pages_read"] += 1
        pages.append((p.final_url, p.text[:20000]))
        links += p.links
    emails = site_emails(" ".join(t for _, t in pages), links, domain)

    # (a) a named person on the company's own pages
    person = None
    home = c.get("_home_url") or "https://" + domain
    # people only from the company's own about/team/leadership/press pages: a homepage's names are often customers
    # quoted in testimonials
    team_text = [(u, t) for u, t in pages if u != home and SENIOR.search(t)]
    if team_text:
        scanned = [(u, (await scan(t[:3000], ctx.call)).clean_text) for u, t in team_text[:3]]
        block = "\n\n".join(f"[{u}]\n{t}" for u, t in scanned)
        res = await structured(
            "contact_finder", "small",
            "List the people these pages name together with their role at this company (founders, executives, heads "
            "of a function). Copy names and roles exactly as written; page_url is the [url] of the page that names "
            "them. Only people named on these pages; never guess.",
            f"Company: {c['name']} ({domain})\nRoles we want to reach: {', '.join(titles[:8]) or 'founder, CEO'}\n"
            + wrap_untrusted("company_pages", block), People, ctx, max_tokens=500,
            metadata={"company": c["name"], "pages": [u for u, _ in team_text[:3]]})
        page_text = {u: t for u, t in pages}
        verified = [p for p in res.output.people
                    if p.page_url in page_text and p.name.strip() and p.name.lower() in page_text[p.page_url].lower()
                    and (not p.role or p.role.lower()[:20] in page_text[p.page_url].lower())
                    and SENIOR.search(p.role) and works_here(p.role, c["name"], domain)]
        want = {w for t in titles for w in re.findall(r"[a-z]{3,}", t.lower())}
        verified.sort(key=lambda p: (bool(SENIOR.search(p.role)), len(want & set(re.findall(r"[a-z]{3,}", p.role.lower())))),
                      reverse=True)
        person = verified[0] if verified else None
    if person:
        email = _person_email(person.name, emails)
        c.update(full_name=clean_name(person.name), title=person.role[:200], contact_url=person.page_url,
                 email=email, email_status="published" if email else "",
                 contact_source="site_email" if email else "team_page", contact_confidence="high" if email else "medium")
    elif emails:
        e = next((x for x in emails if not is_role_inbox(x)), emails[0])
        c.update(email=e, email_status="published", contact_url=pages[0][0] if pages else "",
                 contact_source="role_inbox" if is_role_inbox(e) else "site_email",
                 contact_confidence="low" if is_role_inbox(e) else "medium")

    # (c) a LinkedIn profile from search results, when the site named nobody
    if not c.get("full_name") and budget.take_search():
        try:
            r = await _web_search(SearchArgs(query=f"{c['name']} founder CEO linkedin"), ctx, with_answer=False)
            f["searches"] = budget.searches
            li = linkedin_from_results(r["results"], c["name"])
        except IntegrationError:
            li = None
        if li:
            name, role, url = li
            c.update(full_name=clean_name(name), title=role[:200], linkedin_url=url)
            if not c.get("contact_source"):
                c.update(contact_source="linkedin_search", contact_url=url, contact_confidence="medium" if role else "low")

    # (d) Hunter, only as enrichment and only with the operator's own key
    if hunter is not None and not c.get("email"):
        from app.integrations.hunter import best_contact
        try:
            contacts = await hunter.domain_search(domain, limit=6)
            hc = best_contact(contacts, titles)
            if hc is not None and hc.status in ("valid", "accept_all"):
                c.update(email=hc.email, email_status=hc.status, email_confidence=hc.confidence,
                         full_name=c.get("full_name") or hc.full_name, title=c.get("title") or hc.position,
                         linkedin_url=c.get("linkedin_url") or hc.linkedin_url, contact_source="hunter",
                         contact_confidence="high" if hc.status == "valid" else "medium",
                         contact_url=c.get("contact_url") or "")
                f["hunter_used"] = f.get("hunter_used", 0) + 1
        except IntegrationError as e:
            f.setdefault("warnings", [])
            msg = f"Hunter.io: {e.message}"
            if msg not in f["warnings"]:
                f["warnings"].append(msg)
    return c


def broaden_suggestion(dropped: dict[str, int], found: int) -> str:
    """One concrete change for 'broaden and retry', from the biggest reason leads were lost."""
    if found == 0:
        return "Search adjacent industries and neighbouring regions: the searches found almost no companies."
    top = max(dropped, key=dropped.get) if dropped else ""
    return {"region": "Include neighbouring regions: many companies found were outside the target region.",
            "low_fit": "Widen the ideal customer profile (adjacent industries, a wider company size).",
            "size": "Relax the company-size range: many companies found were bigger or smaller than the profile.",
            "no_contact_route": "Keep searching: many companies qualified but published no way to reach a person.",
            "competitor": "Exclude the competitors' own lists and search other directories.",
            "duplicate": "Search new sources: most companies found were already in your leads.",
            }.get(top, "Widen the region, the industries and the company size.")
