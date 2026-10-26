"""Research must be verifiable: invented sources are dropped and counted, outages stop the run, provenance is recorded."""
import copy

import pytest
from sqlalchemy import select

from app.agents.orchestrator import engine
from app.agents.orchestrator.engine import Orchestrator
from app.core.tenancy import tenant_scope
from app.db.models import Artifact, RunNode
from app.fixtures.seed import seed
from app.integrations import research as research_mod
from app.integrations.errors import NOT_CONFIGURED, UNAVAILABLE, IntegrationError
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import MOCK_RESPONDERS

START = "2026-11-02"


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    ids = await seed(db)
    return db, ids, Orchestrator()


async def art(db, tid, kind):
    with tenant_scope(tid):
        async with db.reader() as s:
            return (await s.execute(select(Artifact).where(Artifact.kind == kind).order_by(Artifact.version.desc()))).scalars().first()


async def discover(orch, tid):
    rid = await orch.create_run(tid, "discovery", {"start_date": START, "auto_approve_gates": True})
    return rid, await orch.execute(tid, rid)


async def test_every_cited_url_in_the_stored_research_was_actually_seen(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    assert (await discover(orch, tid))[1] == "succeeded"
    market, comp, trends = [await art(db, tid, k) for k in ("market_report", "competitor_matrix", "trend_report")]
    seen = set(market.data["_provenance"]["pages"]) | set(comp.data["_provenance"]["pages"]) | set(trends.data["_provenance"]["pages"])
    assert market.data["key_facts"] and all(f["verified"] and f["source_url"] in seen for f in market.data["key_facts"])
    assert all(c["verified"] and set(c["source_urls"]) <= seen for c in comp.data["competitors"])
    assert all(t["verified"] and t["evidence_url"] in seen for t in trends.data["trends"])


async def test_provenance_marks_synthetic_runs(world):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    await discover(orch, tid)
    pv = (await art(db, tid, "market_report")).data["_provenance"]
    assert pv["mode"] == "mock" and pv["synthetic"] is True and pv["searches"] and pv["generated_at"]


async def test_fabricated_source_urls_are_dropped_and_counted(world, monkeypatch):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    real = MOCK_RESPONDERS["market_analyst"]

    def lying(req):
        out = real(req)
        for call in out.get("tool_calls", []):
            if call["name"] == "submit_result":
                call["arguments"]["key_facts"] += [
                    {"claim": "93% of CFOs love us", "source_url": "https://made-up.example/survey"},
                    {"claim": "an uncited claim", "source_url": ""}]
        return out
    monkeypatch.setitem(MOCK_RESPONDERS, "market_analyst", lying)
    assert (await discover(orch, tid))[1] == "succeeded"
    m = await art(db, tid, "market_report")
    assert not any("93%" in f["claim"] or f["claim"] == "an uncited claim" for f in m.data["key_facts"])
    assert m.data["_provenance"]["dropped_unverified"] >= 2


async def test_unsourced_competitors_are_dropped_and_none_left_fails_honestly(world, monkeypatch):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    real = MOCK_RESPONDERS["competitor_analyst"]

    def invented(req):
        out = real(req)
        for call in out.get("tool_calls", []):
            if call["name"] == "submit_result":
                for c in call["arguments"]["competitors"]:
                    c["source_urls"] = ["https://invented.example/page"]
        return out
    monkeypatch.setitem(MOCK_RESPONDERS, "competitor_analyst", invented)
    rid, status = await discover(orch, tid)
    assert status == "failed"
    with tenant_scope(tid):
        async with db.reader() as s:
            node = (await s.execute(select(RunNode).where(RunNode.run_id == rid, RunNode.key == "competitors"))).scalars().one()
    assert node.status == "failed" and "could be verified" in node.error
    assert await art(db, tid, "competitor_matrix") is None               # nothing fabricated was stored


class DeadSearch:
    name, synthetic = "live", False

    def __init__(self, err):
        self.err = err

    async def search(self, q):
        raise self.err

    async def fetch(self, url):
        raise self.err

    def may_fetch(self, url, allowed):
        return False


@pytest.mark.parametrize("err", [
    IntegrationError("web_search", NOT_CONFIGURED, "no web search provider is configured", "Set TAVILY_API_KEY"),
    IntegrationError("web_search", UNAVAILABLE, "no web search provider could answer: quota", "Retry later"),
])
async def test_search_outage_stops_research_with_the_fix_and_stores_nothing(world, monkeypatch, err):
    db, ids, orch = world
    tid = ids["ledgerloop"]
    monkeypatch.setattr("app.agents.research.agents.get_research", lambda: DeadSearch(err))
    rid, status = await discover(orch, tid)
    assert status == "failed"
    with tenant_scope(tid):
        async with db.reader() as s:
            nodes = {n.key: n for n in (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars()}
    for key in ("market", "competitors"):
        assert nodes[key].status == "failed" and nodes[key].output["integration"]["recovery"] == err.recovery
    assert nodes["strategy"].status == "blocked"
    for kind in ("market_report", "competitor_matrix", "trend_report", "content_strategy"):
        assert await art(db, tid, kind) is None                           # no stand-in research was substituted


async def test_injected_page_never_reaches_stored_research(world):
    db, ids, orch = world
    for slug in ("ledgerloop", "shiftwise"):
        await discover(orch, ids[slug])
    for slug in ("ledgerloop", "shiftwise"):
        for kind in ("market_report", "competitor_matrix", "trend_report", "content_strategy", "icp"):
            blob = str((await art(db, ids[slug], kind)).data).lower()
            assert "ignore all previous" not in blob and "system prompt" not in blob
