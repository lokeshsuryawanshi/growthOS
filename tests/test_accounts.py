"""Signup, onboarding, profile persistence and the clean-workspace guarantee."""
import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.tenancy import tenant_scope
from app.db.models import AuditLog, KnowledgeDoc, Tenant
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app

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
    "do_not_contact": "ex-customer@old.example\nblocked.example\nSome Company", "primary_goal": "Inbound demo requests",
    "posts_per_week": "5", "leads_per_week_min": "10", "leads_per_week_max": "20",
}


@pytest.fixture
async def client(db):
    set_gateway(LLMGateway())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def signup(client, email="alex@brightwave.example", name="Alex Rivera", password="correct horse battery"):
    r = await client.post("/api/auth/signup", json={"name": name, "email": email, "password": password})
    return r, ({"Authorization": f"Bearer {r.json()['token']}"} if r.status_code == 201 else None)


async def workspace(client, email="alex@brightwave.example", form=None):
    r, h = await signup(client, email)
    assert r.status_code == 201, r.text
    r = await client.post("/api/tenants", json=form or FORM, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"], {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.mark.parametrize("payload,fragment", [
    ({"name": "A", "email": "not-an-email", "password": "correct horse battery"}, "valid email"),
    ({"name": "A", "email": "a@b.example", "password": "short"}, "10 characters"),
    ({"name": "", "email": "a@b.example", "password": "correct horse battery"}, "name"),
])
async def test_signup_validation(client, payload, fragment):
    r = await client.post("/api/auth/signup", json=payload)
    assert r.status_code == 422 and fragment in r.json()["detail"]


async def test_duplicate_email_and_invite_code(client, monkeypatch):
    assert (await signup(client))[0].status_code == 201
    r, _ = await signup(client)
    assert r.status_code == 422 and "already exists" in r.json()["detail"]
    monkeypatch.setattr(get_settings(), "signup_invite_code", "sesame")
    assert (await signup(client, email="b@brightwave.example"))[0].status_code == 422
    r = await client.post("/api/auth/signup", json={"name": "B", "email": "b@brightwave.example",
                                                    "password": "correct horse battery", "invite_code": "sesame"})
    assert r.status_code == 201


async def test_new_workspace_starts_completely_empty(client, db):
    tid, h = await workspace(client)
    assert [t["name"] for t in (await client.get("/api/tenants", headers=h)).json()] == ["Brightwave Labs"]
    k = (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).json()
    assert k["posts_pending"] == 0 and k["leads_latest_week"] == 0 and k["leads_total"] == 0 and k["calendar_weeks_planned"] == 0
    assert k["cost_total_usd"] == 0 and k["approval_rate"] is None
    for path in ("posts", "leads", "runs", "calendar"):
        assert (await client.get(f"/api/tenants/{tid}/{path}", headers=h)).json() == []
    assert (await client.get(f"/api/tenants/{tid}/artifacts/content_strategy", headers=h)).status_code == 404
    async with db.reader() as s:
        assert [t.is_demo for t in (await s.execute(select(Tenant))).scalars()] == [False]
    # targets come from the founder's own goals, not global defaults
    assert k["targets"] == {"posts_per_week": 5, "calendar_weeks": 8, "leads_per_week": [10, 20]}


async def test_onboarding_persists_exactly_what_was_entered(client, db):
    tid, h = await workspace(client)
    prof = (await client.get(f"/api/tenants/{tid}/profile", headers=h)).json()
    assert prof["company_name"] == "Brightwave Labs" and prof["website"] == "https://brightwave.example"
    assert prof["known_competitors"].splitlines() == ["OtherCo | https://otherco.example", "ThirdCo"]
    assert prof["do_not_contact"].splitlines() == ["ex-customer@old.example", "blocked.example", "some company"]
    assert prof["sample_1"] == SAMPLE and prof["sample_3"] == "" and prof["posts_per_week"] == 5
    with tenant_scope(tid):
        async with db.reader() as s:
            kinds = sorted(d.kind for d in (await s.execute(select(KnowledgeDoc))).scalars())
    assert kinds == ["founder_sample", "founder_sample", "intake_note"]


async def test_profile_edits_persist_and_are_audited(client, db):
    tid, h = await workspace(client)
    edited = {**FORM, "primary_goal": "Pipeline from CTOs", "posts_per_week": "6", "leads_per_week_min": "12",
              "sample_3": SAMPLE.replace("meetings", "standups"), "banned_phrases": "synergy\nleverage"}
    assert (await client.put(f"/api/tenants/{tid}/profile", json=edited, headers=h)).status_code == 200
    prof = (await client.get(f"/api/tenants/{tid}/profile", headers=h)).json()
    assert prof["primary_goal"] == "Pipeline from CTOs" and prof["posts_per_week"] == 6 and "standups" in prof["sample_3"]
    assert prof["banned_phrases"].splitlines() == ["synergy", "leverage"]
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).json()["targets"]["posts_per_week"] == 6
    with tenant_scope(tid):
        async with db.reader() as s:
            actions = [a.action for a in (await s.execute(select(AuditLog))).scalars()]
    assert "profile.updated" in actions and "tenant.onboarded" in actions


async def test_invalid_edit_is_rejected_and_changes_nothing(client):
    tid, h = await workspace(client)
    r = await client.put(f"/api/tenants/{tid}/profile", json={**FORM, "sample_2": "too short", "company_name": "Hacked"}, headers=h)
    assert r.status_code == 422
    assert (await client.get(f"/api/tenants/{tid}/profile", headers=h)).json()["company_name"] == "Brightwave Labs"


@pytest.mark.parametrize("patch,fragment", [
    ({"company_name": ""}, "company_name"), ({"sample_1": "", "sample_2": ""}, "sample"),
    ({"website": "not a site!"}, "website"), ({"posts_per_week": "2"}, "posts_per_week"),
    ({"leads_per_week_min": "30", "leads_per_week_max": "5"}, "maximum"),
])
async def test_onboarding_validation(client, patch, fragment):
    _, h = await signup(client)
    r = await client.post("/api/tenants", json={**FORM, **patch}, headers=h)
    assert r.status_code == 422 and fragment in r.text, r.text


async def test_one_workspace_per_founder(client):
    tid, h = await workspace(client)
    r = await client.post("/api/tenants", json=FORM, headers=h)
    assert r.status_code == 422 and "already have a workspace" in r.text


async def test_two_founders_cannot_see_each_other(client):
    a, ha = await workspace(client, "a@one.example")
    b, hb = await workspace(client, "b@two.example", {**FORM, "company_name": "Second Co"})
    assert a != b
    for path in ("kpis", "posts", "leads", "runs", "calendar", "profile"):
        assert (await client.get(f"/api/tenants/{a}/{path}", headers=hb)).status_code == 404, path
        assert (await client.get(f"/api/tenants/{b}/{path}", headers=ha)).status_code == 404, path
    assert (await client.put(f"/api/tenants/{a}/profile", json=FORM, headers=hb)).status_code == 404
    assert (await client.post(f"/api/tenants/{a}/runs", json={"kind": "discovery"}, headers=hb)).status_code == 404


async def test_login_is_rate_limited(client):
    await signup(client)
    for _ in range(8):
        assert (await client.post("/api/auth/login", json={"email": "alex@brightwave.example", "password": "wrong"})).status_code == 401
    r = await client.post("/api/auth/login", json={"email": "alex@brightwave.example", "password": "correct horse battery"})
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_signup_is_rate_limited(client):
    for i in range(5):
        assert (await signup(client, email=f"u{i}@x.example"))[0].status_code == 201
    assert (await signup(client, email="u9@x.example"))[0].status_code == 429


# ---- the browser flow, as a founder would walk it -------------------------------------------------------------------
async def test_ui_signup_to_empty_dashboard(client):
    assert "Create your workspace" in (await client.get("/signup")).text
    r = await client.post("/signup", data={"name": "Alex Rivera", "email": "alex@brightwave.example", "password": "correct horse battery"})
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    client.cookies.set("growthos_token", r.cookies["growthos_token"])
    page = (await client.get("/onboarding")).text
    assert "Company" in page and "Your voice" in page and "Brand rules" in page and "Goals" in page
    bad = await client.post("/onboarding", data={**FORM, "sample_1": "", "sample_2": ""})
    assert bad.status_code == 422 and "Brightwave Labs" in bad.text          # form values kept on error
    ok = await client.post("/onboarding", data=FORM)
    assert ok.status_code == 303 and ok.headers["location"].startswith("/portal/")
    client.cookies.set("growthos_token", ok.cookies["growthos_token"])
    home = await client.get(ok.headers["location"])
    assert home.status_code == 200 and "Getting set up" in home.text and "Start research" in home.text
    for forbidden in ("LedgerLoop", "ShiftWise", "synthetic", "DEMO MODE"):
        assert forbidden not in home.text
    for tab, empty_title in (("content", "Nothing to review yet"), ("calendar", "No calendar yet"), ("leads", "No leads yet"),
                             ("research", "No research yet"), ("activity", "Nothing has run yet")):
        t = await client.get(f"{ok.headers['location']}?tab={tab}")
        assert t.status_code == 200 and empty_title in t.text, tab


async def test_ui_settings_roundtrip(client):
    tid, h = await workspace(client)
    login = await client.post("/login", data={"email": "alex@brightwave.example", "password": "correct horse battery"})
    client.cookies.set("growthos_token", login.cookies["growthos_token"])
    page = await client.get(f"/portal/{tid}?tab=settings")
    assert "Brightwave Labs" in page.text and "Inbound demo requests" in page.text
    r = await client.post(f"/ui/{tid}/settings", data={**FORM, "company_name": "Brightwave Systems"})
    assert r.status_code == 303
    assert "Brightwave Systems" in (await client.get(f"/portal/{tid}?tab=settings")).text
    bad = await client.post(f"/ui/{tid}/settings", data={**FORM, "sample_1": "x"})
    assert bad.status_code == 422 and "Not saved" in bad.text


async def test_starting_research_without_an_ai_key_is_an_honest_error_not_mock_output(client, db):
    from app.core.config import ConfigError
    tid, h = await workspace(client)
    set_gateway(LLMGateway(Settings(app_env="production", llm_mode="auto", groq_api_key="", jwt_secret="x" * 40)))
    r = await client.post(f"/api/tenants/{tid}/runs", json={"kind": "discovery"}, headers=h)
    assert r.status_code == 424
    body = r.json()
    assert body["code"] == "not_configured" and "GROQ_API_KEY" in body["recovery"]
    assert (await client.get(f"/api/tenants/{tid}/runs", headers=h)).json() == []     # nothing was started or faked
