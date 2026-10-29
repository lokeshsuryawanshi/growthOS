"""The live lead engine, offline: every stage against mock HTTP (search, list pages, homepages, team pages) and mock
models, including the empty and the thin case. Nothing here reaches the internet."""
import json
import re

import httpx
import pytest
from sqlalchemy import select

from app.agents.blackboard import put_artifact
from app.agents.leads import live
from app.agents.leads.agents import find_leads
from app.agents.runtime.loop import AgentContext
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.models import ClientProfile, Lead, LeadRejection
from app.integrations import http
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import MOCK_RESPONDERS
from app.llm.types import Budget, CallContext
from tests.helpers import bare_tenants, give_key

ICP = {"segments": [{"name": "independent coffee roasters", "titles": ["Founder", "Head of Operations"],
                     "industries": ["coffee"], "min_employees": 5, "max_employees": 200}], "geographies": ["UK"]}
INTAKE = {"company_name": "RoastIQ", "website": "https://roastiq.example", "company": "Roast profiling software",
          "target_customers": "Independent coffee roasters in the UK", "geographies": ["UK"],
          "known_competitors": ["Cropster"], "competitor_urls": ["https://cropster.example"],
          "goals": {"posts_per_week": 5, "leads_per_week_min": 2, "leads_per_week_max": 4}}

LIST_PAGE = """<html><head><title>Top 10 UK coffee roasters</title></head><body><h1>Top UK coffee roasters</h1>
<p>Our pick of the best independent roasters in Britain this year, from Bristol to Leeds.</p>
<a href="https://beanbarn.example/">Bean Barn Roastery</a> <a href="https://hilltoproast.example">Website</a>
<a href="https://www.facebook.com/x">Facebook</a> <a href="https://ukroasters.example/about">About us</a>
<a href="https://cropster.example">Cropster</a></body></html>"""


def homepage(name: str, extra: str = "") -> str:
    return (f"<html><head><title>{name}</title></head><body><p>{name} is an independent specialty coffee roaster in "
            f"Bristol, UK, roasting single origin beans for cafes since 2014.</p>{extra}"
            f"<a href='/about'>About us</a></body></html>")


TEAM_PAGE = ("<html><head><title>About Bean Barn</title></head><body><h2>Our team</h2><p>Jane Doe, Founder and Head "
             "Roaster, started Bean Barn in 2014.</p><p>Sam Lee - Head of Operations</p></body></html>")


class Web:
    """Tavily and the pages it points to. `results` is what every search returns."""

    def __init__(self, results=None, pages=None):
        self.results = results if results is not None else []
        self.pages = pages or {}
        self.searches, self.fetched = [], []

    def handler(self, r: httpx.Request) -> httpx.Response:
        if r.url.host == "api.tavily.com":
            q = json.loads(r.content)["query"]
            self.searches.append(q)
            res = self.results(q) if callable(self.results) else self.results
            return httpx.Response(200, json={"answer": "", "results": res})
        self.fetched.append(str(r.url))
        assert "linkedin.com" not in r.url.host, "linkedin.com must never be fetched"
        key = r.url.host + (r.url.path.rstrip("/") or "")
        body = self.pages.get(key)
        if body is None:
            return httpx.Response(404, text="nope", headers={"content-type": "text/html"})
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def hit(title, url, snippet=""):
    return {"title": title, "url": url, "content": snippet}


@pytest.fixture
async def client(db, monkeypatch):
    """A client with an ICP, running on live (non-fixture) sources with mock models."""
    set_gateway(LLMGateway())
    monkeypatch.setattr(type(get_settings()), "uses_fixture_sources", property(lambda self: False))
    await bare_tenants(db, "t1")
    with tenant_scope("t1"):
        async with db.session() as s:
            s.add(ClientProfile(intake=INTAKE, policy={}))
    await put_artifact("t1", "icp", ICP)
    await give_key("u-test", "tavily")
    monkeypatch.setitem(MOCK_RESPONDERS, "company_scout_plan", lambda req: {"content": {"queries": [
        {"family": "top_list", "query": "top uk coffee roasters"}, {"family": "hiring", "query": "coffee roaster hiring uk"}]}})
    monkeypatch.setitem(MOCK_RESPONDERS, "company_scout", lambda req: {"content": {"companies": [
        {"name": "Hilltop Roast", "domain": "hilltoproast.example", "evidence_url": "https://news.example/roasters",
         "signal": "Hilltop Roast is hiring a head of operations", "country": "UK"},
        {"name": "Made Up Ltd", "domain": "madeup.example", "evidence_url": "https://not-a-result.example/x"},
        {"name": "Kaffee Haus", "domain": "kaffeehaus.de", "evidence_url": "https://news.example/roasters", "country": "Germany"}]}})

    def qualifier(req):
        return {"content": {"scores": [{"prospect_id": p["id"], "fit_score": 85, "segment": "roasters",
                                        "evidence": [p.get("about") or p.get("signal") or p["company"]]}
                                       for p in req.metadata["batch"]]}}
    monkeypatch.setitem(MOCK_RESPONDERS, "qualifier", qualifier)

    def people(req):
        pages = req.metadata["pages"]
        return {"content": {"people": [{"name": "Jane Doe", "role": "Founder and Head Roaster", "page_url": u}
                                       for u in pages if "beanbarn" in u][:1] +
                            [{"name": "Invented Person", "role": "CEO", "page_url": pages[0]}]}}
    monkeypatch.setitem(MOCK_RESPONDERS, "contact_finder", people)
    return db


def ctx():
    return AgentContext(CallContext(tenant_id="t1", run_id="r1", node_key="leads", budget=Budget(max_usd=5)))


async def leads(db):
    with tenant_scope("t1"):
        async with db.reader() as s:
            return {l.company_domain: l for l in (await s.execute(select(Lead))).scalars()}, \
                [r for r in (await s.execute(select(LeadRejection))).scalars()]


async def test_a_full_run_finds_traceable_companies_and_real_ways_to_reach_them(client):
    db = client
    web = Web(results=[hit("Top 10 UK coffee roasters", "https://lists.example/top-uk-roasters", "Our pick of UK roasters"),
                       hit("Roasters hiring", "https://news.example/roasters",
                           "Hilltop Roast is hiring a head of operations in Leeds; Kaffee Haus in Berlin too")],
              pages={"lists.example/top-uk-roasters": LIST_PAGE,
                     "beanbarn.example": homepage("Bean Barn Roastery"),
                     "beanbarn.example/about": TEAM_PAGE,
                     "hilltoproast.example": homepage("Hilltop Roast", "<p>Write to hello@hilltoproast.example or "
                                                                      "<a href='mailto:press@othersite.example'>press</a></p>"),
                     "ukroasters.example": homepage("UK Roasters Guild")})
    http.set_transport(httpx.MockTransport(web.handler))
    stats = await find_leads("t1", ctx(), __import__("datetime").date(2026, 11, 2))
    got, rejected = await leads(db)
    # Bean Barn: a named person on its own about page; no email was published, so none is invented
    bb = got["beanbarn.example"]
    assert (bb.full_name, bb.contact_source, bb.email, bb.contact_confidence) == ("Jane Doe", "team_page", "", "medium")
    assert bb.contact_url == "https://beanbarn.example/about"
    # Hilltop: nobody named, but a team inbox published on its own site (the other site's address is ignored)
    ht = got["hilltoproast.example"]
    assert (ht.email, ht.contact_source, ht.contact_confidence) == ("hello@hilltoproast.example", "role_inbox", "low")
    reasons = {r.company: r.reason for r in rejected}
    assert "Cropster" in reasons and reasons["Cropster"] == "competitor"           # the competitor's own site
    assert reasons.get("Kaffee Haus") == "region"                                   # clearly outside the UK
    assert "Made Up Ltd" not in got and "madeup.example" not in got                 # not traced to a result
    assert "Invented Person" not in {l.full_name for l in got.values()}             # not on the page: never kept
    assert stats["funnel"]["found_on_lists"] >= 2 and stats["funnel"]["list_pages_read"] == 1
    assert stats["dropped"]["region"] == 1 and stats["dropped"]["competitor"] == 1
    assert stats["contact_routes"] == {"team_page": 1, "role_inbox": 1}
    assert web.fetched.count("https://lists.example/top-uk-roasters") == 1          # a page answering many queries: read once
    assert stats["funnel"]["budget"]["searches"][0] <= stats["funnel"]["budget"]["searches"][1]
    assert set(stats["funnel"]["families"]) >= {"top_list", "hiring", "events", "community", "directory", "funding",
                                                "competitor_customers"}                # every family searched
    for l in got.values():
        assert not l.email or l.email.endswith("@" + l.company_domain)              # only addresses the company published


async def test_an_empty_search_ends_in_zero_with_the_reason_and_a_suggestion(client):
    web = Web(results=[])
    http.set_transport(httpx.MockTransport(web.handler))
    stats = await find_leads("t1", ctx(), __import__("datetime").date(2026, 11, 2))
    from app.agents.orchestrator.engine import lead_shortfall
    why = lead_shortfall(stats)
    assert stats["week_total"] == 0 and stats["rounds"] == 3
    assert any("found 0 companies" in w for w in why) and any("no companies" in w for w in why)
    assert "adjacent industries" in stats["suggestion"]
    assert len(web.searches) <= live.SearchBudget().max_searches                     # bounded, and reported
    assert stats["funnel"]["budget"]["searches"][0] == len(web.searches)


async def test_a_thin_web_presence_explains_each_drop(client, monkeypatch):
    """One company found, no person named, no email published, no LinkedIn result: dropped, and said so."""
    monkeypatch.setitem(MOCK_RESPONDERS, "company_scout", lambda req: {"content": {"companies": [
        {"name": "Quiet Beans", "domain": "quietbeans.example", "evidence_url": "https://news.example/q"}]}})
    web = Web(results=lambda q: [hit("Quiet Beans opens", "https://news.example/q", "Quiet Beans opens a roastery")]
              if "linkedin" not in q else [],
              pages={"quietbeans.example": homepage("Quiet Beans")})
    http.set_transport(httpx.MockTransport(web.handler))
    stats = await find_leads("t1", ctx(), __import__("datetime").date(2026, 11, 2))
    got, rejected = await leads(client)
    assert got == {} and stats["dropped"]["no_contact_route"] >= 1
    assert any(r.reason == "no_contact_route" and r.company == "Quiet Beans" for r in rejected)
    assert "reach a person" in stats["suggestion"] or stats["suggestion"]


async def test_companies_clearly_outside_the_size_range_are_dropped_unless_broadened(client, monkeypatch):
    def qualifier(req):
        return {"content": {"scores": [{"prospect_id": p["id"], "fit_score": 90, "size": "too_big",
                                        "evidence": [p.get("about") or p["company"]]} for p in req.metadata["batch"]]}}
    monkeypatch.setitem(MOCK_RESPONDERS, "qualifier", qualifier)
    web = Web(results=[hit("Roasters", "https://news.example/roasters", "Hilltop Roast is hiring a head of operations")],
              pages={"hilltoproast.example": homepage("Hilltop Roast", "<p>hello@hilltoproast.example</p>")})
    http.set_transport(httpx.MockTransport(web.handler))
    stats = await find_leads("t1", ctx(), __import__("datetime").date(2026, 11, 2))
    assert stats["dropped"].get("size") and (await leads(client))[0] == {}
    assert "size range" in stats["suggestion"]


async def test_broaden_skips_the_region_filter(client):
    web = Web(results=[hit("Roasters", "https://news.example/roasters", "Kaffee Haus roasts in Berlin")],
              pages={"kaffeehaus.de": homepage("Kaffee Haus", "<p>kontakt@kaffeehaus.de</p>")})
    http.set_transport(httpx.MockTransport(web.handler))
    stats = await find_leads("t1", ctx(), __import__("datetime").date(2026, 11, 2), broaden=True)
    got, _ = await leads(client)
    assert "kaffeehaus.de" in got and stats["dropped"].get("region", 0) == 0


async def test_a_week_that_already_has_its_leads_searches_nothing_and_says_so(client):
    web = Web(results=[hit("Roasters", "https://news.example/roasters",
                           "Hilltop Roast is hiring a head of operations; Kaffee Haus roasts in Berlin")],
              pages={"hilltoproast.example": homepage("Hilltop Roast", "<p>hello@hilltoproast.example</p>"),
                     "kaffeehaus.de": homepage("Kaffee Haus", "<p>kontakt@kaffeehaus.de</p>")})
    http.set_transport(httpx.MockTransport(web.handler))
    week = __import__("datetime").date(2026, 11, 2)
    first = await find_leads("t1", ctx(), week, broaden=True)
    assert first["week_total"] >= 2 and not first.get("already_met")
    searched = len(web.searches)
    again = await find_leads("t1", ctx(), week)
    assert again["already_met"] and again["rounds"] == 0 and len(web.searches) == searched


# ---------------------------------------------------------------- the pieces
def test_list_pages_yield_company_links_not_the_publisher_or_social_sites():
    from app.integrations.fetch import parse_html
    title, _, links = parse_html(LIST_PAGE, "https://lists.example/top")
    got = {m["domain"]: m["name"] for m in live.mine_list_links("https://lists.example/top", title, links)}
    assert got["beanbarn.example"] == "Bean Barn Roastery" and got["hilltoproast.example"] == "Hilltoproast"
    assert "facebook.com" not in got and "lists.example" not in got


def test_only_addresses_published_on_the_companys_own_domain_are_used():
    text = "Email jane@acme.example or info@acme.example. Partner: bob@other.example. logo@2x.png"
    links = [("mailto:Sales@Acme.Example?subject=hi", "sales"), ("mailto:x@other.example", "x")]
    emails = live.site_emails(text, links, "acme.example")
    assert emails == ["sales@acme.example", "jane@acme.example", "info@acme.example"]
    assert live.is_role_inbox("info@acme.example") and not live.is_role_inbox("jane@acme.example")
    assert live._person_email("Jane Doe", emails) == "jane@acme.example"
    assert live._person_email("Pat Kim", emails) == ""                              # never pattern-generated


def test_linkedin_profiles_come_only_from_search_results_naming_the_company():
    res = [hit("Jane Doe - Founder - Bean Barn | LinkedIn", "https://uk.linkedin.com/in/janedoe?trk=x", ""),
           hit("Someone - CEO - Other Co | LinkedIn", "https://www.linkedin.com/in/someone", ""),
           hit("Bean Barn | LinkedIn", "https://www.linkedin.com/company/beanbarn", "")]
    assert live.linkedin_from_results(res, "Bean Barn") == ("Jane Doe", "Founder", "https://uk.linkedin.com/in/janedoe")
    assert live.linkedin_from_results(res[1:], "Bean Barn") is None


@pytest.mark.parametrize("cand,targets,out", [
    ({"domain": "acme.de", "country": ""}, ["UK"], True), ({"domain": "acme.de", "country": ""}, ["UK", "Germany"], False),
    ({"domain": "acme.com", "country": ""}, ["UK"], False), ({"domain": "acme.com", "country": "united states"}, ["US"], False),
    ({"domain": "acme.in", "country": ""}, ["Global"], False), ({"domain": "acme.co.uk", "country": ""}, ["UK"], False)])
def test_region_filter_drops_only_clear_mismatches(cand, targets, out):
    assert live.region_mismatch(cand, targets) is out


def test_every_query_family_has_a_fallback():
    fb = live.fallback_queries(ICP, INTAKE)
    assert set(fb) == set(live.FAMILIES) and all(re.search(r"\w", q) for q in fb.values())
    assert "Cropster" in fb["competitor_customers"]


def test_names_from_link_labels_are_repaired_and_testimonials_are_not_staff():
    assert live.better_name("catalogue of works", "mooveagency.com", "Moove Agency | Digital agency") == "Moove Agency"
    assert live.better_name("case studies", "seahawkmedia.com", "") == "Seahawkmedia"
    assert live.better_name("Bean Barn Roastery", "beanbarn.example", "") == "Bean Barn Roastery"
    assert live.better_name("render.com", "render.com", "Render | The cloud for builders") == "Render"
    assert not live.works_here("Senior Director, Global Digital Marketing JLL", "Moove Agency", "mooveagency.com")
    assert not live.works_here("Co-Founder @ Floresco", "Yellow Peach", "yellowpeach.co.uk")
    assert live.works_here("Founder and Head Roaster", "Bean Barn", "beanbarn.example")
    assert live.clean_name("Chris Januszewski \U0001F399️") == "Chris Januszewski"
    assert live.is_role_inbox("studio@impactmedia.co.uk") and live.is_role_inbox("go@codal.com")
