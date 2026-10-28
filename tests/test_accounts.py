"""Accounts and clients: add, edit and delete client profiles; the clean-start guarantee; the operator's journey."""
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.tenancy import tenant_scope
from app.db.models import TENANT_TABLES, AuditLog, Base, KnowledgeDoc, Tenant, User
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from app.services import accounts
from tests.helpers import add_client, client_for, sign_in

SAMPLE = ("Most teams I talk to say they want faster decisions. What they mean is fewer meetings about the same spreadsheet. "
          "We rebuilt our weekly review around one page and one owner, and the arguments about whose numbers were right disappeared.")
FORM = {
    "company_name": "Brightwave Labs", "website": "brightwave.example", "industry": "Developer tooling",
    "company": "We build observability tooling that helps small platform teams find the cause of an outage in minutes.",
    "target_customers": "Heads of platform engineering at 50-500 person software companies",
    "geographies": "US\nUK", "known_competitors": "OtherCo | https://otherco.example\nThirdCo", "founder_name": "Alex Rivera",
    "founder_title": "CEO", "linkedin_url": "", "sample_1": SAMPLE, "sample_2": SAMPLE.replace("faster", "better"),
    "tone": "plain and a bit dry", "topics_to_own": "incident response\non-call health", "topics_to_avoid": "politics",
    "banned_phrases": "game-changer", "facts": "We reduced median time to diagnosis from 41 to 9 minutes for 30 customers",
    "primary_goal": "Inbound demo requests",
    "posts_per_week": "5", "leads_per_week_min": "10", "leads_per_week_max": "20",
}


@pytest.fixture
async def client(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        yield c


async def signed_in_with_client(client, email="op@example.com", form=None) -> str:
    await sign_in(client, email)
    return await add_client(client, **(form or FORM))


async def count(db, tid, model) -> int:
    with tenant_scope(tid):
        async with db.reader() as s:
            return (await s.execute(select(func.count(model.id)))).scalar_one()


@pytest.mark.parametrize("patch,fragment", [
    ({"company_name": ""}, "company_name"), ({"sample_1": "", "sample_2": ""}, "sample"),
    ({"website": "not a site!"}, "website"), ({"posts_per_week": "2"}, "posts_per_week"),
    ({"posts_per_week": "five"}, "posts_per_week"),
    ({"leads_per_week_min": "30", "leads_per_week_max": "5"}, "maximum"),
])
async def test_client_form_validation_keeps_the_values(client, patch, fragment):
    await sign_in(client)
    r = await client.post("/clients/new", data={**FORM, **patch})
    assert r.status_code == 422 and fragment in r.text, r.text[:400]
    if patch.get("company_name") != "":
        assert "Brightwave Labs" in r.text                       # what was typed is still in the form


async def test_a_new_client_starts_completely_empty(client, db):
    tid = await signed_in_with_client(client)
    from app.services.kpis import tenant_kpis
    k = await tenant_kpis(tid)
    assert k["posts_pending"] == 0 and k["leads_total"] == 0 and k["calendar_weeks_planned"] == 0
    assert k["cost_total_usd"] == 0 and k["approval_rate"] is None
    assert k["targets"] == {"posts_per_week": 5, "calendar_weeks": 8, "leads_per_week": [10, 20]}   # the client's own goals
    async with db.reader() as s:
        assert [t.is_demo for t in (await s.execute(select(Tenant))).scalars()] == [False]


async def test_onboarding_persists_exactly_what_was_entered(client, db):
    tid = await signed_in_with_client(client)
    prof = await accounts.profile_as_form_values(tid)
    assert prof["company_name"] == "Brightwave Labs" and prof["website"] == "https://brightwave.example"
    assert prof["known_competitors"].splitlines() == ["OtherCo | https://otherco.example", "ThirdCo"]
    assert prof["sample_1"] == SAMPLE and prof["sample_3"] == "" and prof["posts_per_week"] == 5
    with tenant_scope(tid):
        async with db.reader() as s:
            kinds = sorted(d.kind for d in (await s.execute(select(KnowledgeDoc))).scalars())
    assert kinds == ["founder_sample", "founder_sample", "intake_note"]


async def test_profile_edits_persist_and_are_audited(client, db):
    tid = await signed_in_with_client(client)
    edited = {**FORM, "primary_goal": "Pipeline from CTOs", "posts_per_week": "6", "sample_3": SAMPLE.replace("meetings", "standups"),
              "banned_phrases": "synergy\nleverage", "company_name": "Brightwave Systems"}
    assert (await client.post(f"/ui/{tid}/settings", data=edited)).status_code == 303
    prof = await accounts.profile_as_form_values(tid)
    assert prof["primary_goal"] == "Pipeline from CTOs" and prof["posts_per_week"] == 6 and "standups" in prof["sample_3"]
    assert prof["banned_phrases"].splitlines() == ["synergy", "leverage"]
    assert "Brightwave Systems" in (await client.get(f"/portal/{tid}?tab=settings")).text
    with tenant_scope(tid):
        async with db.reader() as s:
            actions = [a.action for a in (await s.execute(select(AuditLog))).scalars()]
    assert "profile.updated" in actions and "tenant.onboarded" in actions


async def test_an_invalid_edit_changes_nothing(client):
    tid = await signed_in_with_client(client)
    bad = await client.post(f"/ui/{tid}/settings", data={**FORM, "sample_2": "too short", "company_name": "Hacked"})
    assert bad.status_code == 422 and "Not saved" in bad.text
    assert (await accounts.profile_as_form_values(tid))["company_name"] == "Brightwave Labs"


async def test_one_account_runs_several_clients_and_home_follows_the_count(client):
    await sign_in(client)
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/clients"            # no clients: the clients page
    assert "No clients yet" in (await client.get("/clients")).text
    a = await add_client(client)
    r = await client.get("/")
    assert r.headers["location"] == f"/portal/{a}"                                 # one client: straight to it
    b = await add_client(client, company_name="Second Co", founder_name="Sam Lee")
    assert (await client.get("/")).headers["location"] == "/clients"               # several: choose
    page = (await client.get("/clients")).text
    assert "Brightwave Analytics" in page and "Second Co" in page and a != b


async def test_deleting_a_client_removes_everything_made_for_it(client, db):
    from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
    from app.fixtures.seed import seed
    set_orchestrator(orch := Orchestrator())
    ids = await seed(db, clients=None)
    tid = ids["ledgerloop"]
    rid = await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True})
    assert await orch.execute(tid, rid) == "succeeded"
    keep = ids["shiftwise"]
    before_other = await count(db, keep, KnowledgeDoc)
    await sign_in(client, "operator@growthos.example")
    assert (await client.post(f"/ui/{tid}/delete", data={"confirm": "wrong name"})).status_code == 303
    async with db.reader() as s:
        assert await s.get(Tenant, tid) is not None                              # wrong confirmation: nothing deleted
    r = await client.post(f"/ui/{tid}/delete", data={"confirm": "LedgerLoop"})
    assert r.status_code == 303 and r.headers["location"] == "/clients"
    async with db.reader() as s:
        assert await s.get(Tenant, tid) is None
    for name in TENANT_TABLES:
        model = next(c for c in Base.__subclasses__() if getattr(c, "__tablename__", None) == name)
        assert await count(db, tid, model) == 0, name
    assert await count(db, keep, KnowledgeDoc) == before_other                    # the other client is untouched
    assert (await client.get(f"/portal/{tid}")).status_code == 404


async def test_closing_the_account_deletes_it_and_signs_out(client, db):
    tid = await signed_in_with_client(client, "leaving@example.com")
    assert (await client.post("/account/delete", data={"confirm": "someone@else.com"})).status_code == 303
    async with db.reader() as s:
        assert (await s.execute(select(func.count(User.id)))).scalar_one() == 1
    r = await client.post("/account/delete", data={"confirm": "Leaving@Example.com"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    async with db.reader() as s:
        assert (await s.execute(select(func.count(User.id)))).scalar_one() == 0
        assert await s.get(Tenant, tid) is None
    assert (await client.get("/clients")).headers["location"] == "/login"


async def test_monthly_cap_is_validated_and_saved(client, db):
    await sign_in(client)
    for bad in ("lots", "-3", "20000"):
        r = await client.post("/account/cap", data={"cap": bad})
        assert r.status_code == 303 and "gx_flash" in r.headers.get("set-cookie", "")
    await client.post("/account/cap", data={"cap": "$7.5"})
    async with db.reader() as s:
        assert (await s.execute(select(User.monthly_cap_usd))).scalar_one() == 7.5
    await client.post("/account/cap", data={"cap": ""})
    async with db.reader() as s:
        assert (await s.execute(select(User.monthly_cap_usd))).scalar_one() is None
    page = await client.get("/account")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"


# ---- the browser flow, as the operator walks it ---------------------------------------------------------------------
async def test_the_operator_journey_from_sign_in_to_an_empty_workspace(client):
    login = await client.get("/login")
    assert login.status_code == 200 and "Continue with Google" in login.text and "password" not in login.text.lower()
    assert "/termsofservice" in login.text and "/login/policy" in login.text
    await sign_in(client, "alex@example.com", "Alex Rivera")
    page = (await client.get("/clients/new")).text
    assert "Company" in page and "voice" in page.lower() and "Goals" in page
    tid = await add_client(client, **FORM)
    home = await client.get(f"/portal/{tid}")
    assert home.status_code == 200 and "Start research" in home.text and "Brightwave Labs" in home.text
    for forbidden in ("LedgerLoop", "ShiftWise", "synthetic", "DEMO MODE"):
        assert forbidden not in home.text
    for tab, empty_title in (("content", "Nothing to review yet"), ("calendar", "No calendar yet"), ("leads", "No leads yet"),
                             ("research", "No research yet"), ("activity", "Nothing has run yet"), ("settings", "Delete this client")):
        t = await client.get(f"/portal/{tid}?tab={tab}")
        assert t.status_code == 200 and empty_title in t.text, tab
    assert (await client.get("/account")).status_code == 200


async def test_starting_research_without_an_ai_key_is_an_honest_error_not_mock_output(client, db):
    tid = await signed_in_with_client(client)
    set_gateway(LLMGateway(Settings(_env_file=None, app_env="production", llm_mode="auto", groq_api_key="", jwt_secret="x" * 40)))
    r = await client.post(f"/ui/{tid}/runs", data={"kind": "discovery"})
    assert r.status_code == 303 and "gx_flash" in r.headers["set-cookie"]
    page = await client.get(r.headers["location"])
    assert "No AI provider" in page.text
    from app.services import queries
    assert await queries.runs(tid) == []                                            # nothing was started or faked
