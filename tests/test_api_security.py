"""RBAC and tenant isolation through the HTTP API."""
import httpx
import pytest

from app.fixtures.clients import DEMO_PASSWORD
from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from app.fixtures.seed import seed


@pytest.fixture
async def api(db):
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    ids = await seed(db)
    for slug in ("ledgerloop", "shiftwise"):
        rid = await orch.create_run(ids[slug], "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True})
        await orch.execute(ids[slug], rid)
        await orch.execute(ids[slug], await orch.create_run(ids[slug], "weekly", {}))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield client, ids
    await client.aclose()


async def token(client, email):
    r = await client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_login_and_unauthenticated(api):
    client, ids = api
    assert (await client.post("/api/auth/login", json={"email": "admin@growthos.example", "password": "nope"})).status_code == 401
    assert (await client.get("/api/tenants")).status_code == 401
    assert (await client.get("/api/tenants", headers={"Authorization": "Bearer forged.token.value"})).status_code == 401


async def test_tenant_listing_respects_membership(api):
    client, ids = api
    admin = await token(client, "admin@growthos.example")
    am = await token(client, "am.sam@growthos.example")
    priya = await token(client, "priya@ledgerloop.example")
    assert len((await client.get("/api/tenants", headers=admin)).json()) == 12
    assert len((await client.get("/api/tenants", headers=am)).json()) == 6
    assert [t["slug"] for t in (await client.get("/api/tenants", headers=priya)).json()] == ["ledgerloop"]


TENANT_GETS = ["/kpis", "/calendar", "/posts", "/leads", "/artifacts/voice_profile"]
AGENCY_GETS = ["/incidents", "/costs"]


async def test_cross_tenant_probes_return_404(api):
    client, ids = api
    priya = await token(client, "priya@ledgerloop.example")
    riya = await token(client, "am.riya@growthos.example")  # manages neither demo client
    other = ids["shiftwise"]
    for path in TENANT_GETS:
        assert (await client.get(f"/api/tenants/{other}{path}", headers=priya)).status_code == 404, path
        assert (await client.get(f"/api/tenants/{other}{path}", headers=riya)).status_code == 404, path
        assert (await client.get(f"/api/tenants/{ids['ledgerloop']}{path}", headers=priya)).status_code == 200, path
    posts = (await client.get(f"/api/tenants/{ids['ledgerloop']}/posts", headers=priya)).json()
    # a valid post id from tenant A used under tenant B's path is not found
    marcus = await token(client, "marcus@shiftwise.example")
    r = await client.post(f"/api/tenants/{other}/posts/{posts[0]['id']}/decision", json={"action": "approve"}, headers=marcus)
    assert r.status_code == 404


async def test_role_matrix(api):
    client, ids = api
    tid = ids["ledgerloop"]
    viewer = await token(client, "viewer@ledgerloop.example")
    priya = await token(client, "priya@ledgerloop.example")
    am = await token(client, "am.sam@growthos.example")
    pid = (await client.get(f"/api/tenants/{tid}/posts?status=pending_approval", headers=priya)).json()[0]["id"]

    assert (await client.post(f"/api/tenants/{tid}/posts/{pid}/decision", json={"action": "approve"}, headers=viewer)).status_code == 403
    assert (await client.post(f"/api/tenants/{tid}/posts/{pid}/decision", json={"action": "approve"}, headers=am)).status_code == 403
    assert (await client.post(f"/api/tenants/{tid}/posts/{pid}/decision", json={"action": "approve"}, headers=priya)).status_code == 200
    assert (await client.post(f"/api/tenants/{tid}/posts/{pid}/decision", json={"action": "approve"}, headers=priya)).status_code == 409

    for path in AGENCY_GETS:  # founders don't see internals
        assert (await client.get(f"/api/tenants/{tid}{path}", headers=priya)).status_code == 403, path
        assert (await client.get(f"/api/tenants/{tid}{path}", headers=am)).status_code == 200, path
    assert (await client.post(f"/api/tenants/{tid}/runs", json={"kind": "weekly"}, headers=viewer)).status_code == 403
    # founders see run progress, but never traces, costs or raw outputs
    runs = (await client.get(f"/api/tenants/{tid}/runs", headers=priya)).json()
    detail = (await client.get(f"/api/tenants/{tid}/runs/{runs[0]['id']}", headers=priya)).json()
    assert detail["cost_usd"] is None and all("output" not in n for n in detail["nodes"])
    assert (await client.post(f"/api/tenants/{tid}/budget", json={"monthly_budget_usd": 99}, headers=am)).status_code == 403
    assert (await client.post(f"/api/tenants/{tid}/kill-switch", json={"on": True}, headers=priya)).status_code == 403


async def test_send_requires_approval_and_owner(api):
    client, ids = api
    tid = ids["ledgerloop"]
    priya = await token(client, "priya@ledgerloop.example")
    am = await token(client, "am.sam@growthos.example")
    lead = next(l for l in (await client.get(f"/api/tenants/{tid}/leads", headers=priya)).json() if l["message_id"])
    mid = lead["message_id"]
    assert (await client.post(f"/api/tenants/{tid}/outreach/{mid}/send", headers=priya)).status_code == 409  # not approved yet
    assert (await client.post(f"/api/tenants/{tid}/outreach/{mid}/decision", json={"action": "approve"}, headers=priya)).status_code == 200
    assert (await client.post(f"/api/tenants/{tid}/outreach/{mid}/send", headers=am)).status_code == 403  # AM can't send as founder
    from tests.helpers import FakeGoogle, connect_fake_gmail, make_sendable
    from app.db.session import get_db
    FakeGoogle().install()
    await connect_fake_gmail(get_db(), tid)
    r = await client.post(f"/api/tenants/{tid}/outreach/{mid}/send", headers=priya)
    assert r.status_code == 409 and "synthetic" in r.text                # fixture leads can never be emailed
    await make_sendable(get_db(), tid)
    r = await client.post(f"/api/tenants/{tid}/outreach/{mid}/send", headers=priya)
    assert r.status_code == 200 and r.json()["status"] == "sent"


async def test_guardrail_refuses_leaky_human_edit(api):
    client, ids = api
    tid = ids["ledgerloop"]
    priya = await token(client, "priya@ledgerloop.example")
    post = (await client.get(f"/api/tenants/{tid}/posts?status=pending_approval", headers=priya)).json()[0]
    r = await client.post(f"/api/tenants/{tid}/posts/{post['id']}/decision",
                          json={"action": "edit", "text": post["text"] + "\nCall me on 555-201-0199."}, headers=priya)
    assert r.status_code == 422


async def test_founder_never_sees_blocked_drafts(api):
    client, ids = api
    tid = ids["ledgerloop"]
    priya = await token(client, "priya@ledgerloop.example")
    assert all(p["status"] != "blocked" for p in (await client.get(f"/api/tenants/{tid}/posts", headers=priya)).json())


async def test_ui_pages_render(api):
    client, ids = api
    tid = ids["ledgerloop"]
    r = await client.post("/login", data={"email": "am.sam@growthos.example", "password": DEMO_PASSWORD})
    assert r.status_code == 303
    cookies = {"growthos_token": r.cookies["growthos_token"]}
    for path in ["/ops", f"/ops/{tid}", f"/ops/{tid}?tab=runs", f"/ops/{tid}?tab=content", f"/ops/{tid}?tab=leads",
                 f"/ops/{tid}?tab=costs", f"/ops/{tid}?tab=guardrails", f"/ops/{tid}?tab=audit",
                 *[f"/portal/{tid}?tab={t}" for t in ("overview", "content", "calendar", "leads", "research", "integrations", "settings", "activity")]]:
        r = await client.get(path, cookies=cookies)
        assert r.status_code == 200, (path, r.text[:500])
    runs = (await client.get(f"/api/tenants/{tid}/runs", headers=await token(client, "am.sam@growthos.example"))).json()
    assert (await client.get(f"/ops/{tid}/runs/{runs[0]['id']}", cookies=cookies)).status_code == 200
    # a founder can't open the ops console, and a stranger tenant's portal is 404
    r = await client.post("/login", data={"email": "priya@ledgerloop.example", "password": DEMO_PASSWORD})
    pc = {"growthos_token": r.cookies["growthos_token"]}
    assert (await client.get("/ops", cookies=pc)).status_code == 404
    assert (await client.get(f"/portal/{ids['shiftwise']}", cookies=pc)).status_code == 404
