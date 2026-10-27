"""Role-based access, proven: team management, the platform admin, escalation attempts, the last owner, revocation,
cross-tenant isolation, and 403 on every write endpoint for a read-only viewer (JSON API and UI)."""
import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.tenancy import tenant_scope
from app.db.models import AuditLog, User
from tests.test_accounts import FORM, client, workspace  # noqa: F401  (client is a fixture)

PW = "correct horse battery"


async def login(client, email, password=PW):
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.status_code, ({"Authorization": f"Bearer {r.json()['token']}"} if r.status_code == 200 else None)


async def ui_login(client, email, password=PW):
    client.cookies.clear()
    r = await client.post("/login", data={"email": email, "password": password})
    assert r.status_code == 303, r.text
    return r


async def add(client, tid, h, email, role="client_viewer", password=PW):
    return await client.post(f"/api/tenants/{tid}/members", json={"email": email, "name": email.split("@")[0],
                                                                   "password": password, "role": role}, headers=h)


async def make_admin(db, email="admin@platform.example"):
    async with db.session() as s:
        s.add(User(email=email, name="Admin", role="platform_admin", password_hash=hash_password(PW)))


@pytest.fixture
async def team(client, db):
    """Workspace A with an owner and a viewer, workspace B with its own owner."""
    tid, owner = await workspace(client, "owner@a.example")
    assert (await add(client, tid, owner, "viewer@a.example")).status_code == 201
    _, viewer = await login(client, "viewer@a.example")
    tid_b, owner_b = await workspace(client, "owner@b.example", form={**FORM, "company_name": "Bravo Works"})
    return {"tid": tid, "owner": owner, "viewer": viewer, "tid_b": tid_b, "owner_b": owner_b}


def write_calls(tid: str, uid: str = "u-x") -> list[tuple[str, str, dict | None]]:
    """Every state-changing JSON endpoint on a workspace, with made-up object ids: authorization must fail first."""
    return [
        ("post", f"/api/tenants/{tid}/runs", {"kind": "content"}),
        ("post", f"/api/tenants/{tid}/posts/p-x/decision", {"action": "approve"}),
        ("post", f"/api/tenants/{tid}/posts/p-x/decision", {"action": "edit", "text": "x"}),
        ("post", f"/api/tenants/{tid}/posts/p-x/posted", None),
        ("post", f"/api/tenants/{tid}/outreach/m-x/decision", {"action": "approve"}),
        ("post", f"/api/tenants/{tid}/outreach/m-x/send", None),
        ("put", f"/api/tenants/{tid}/profile", FORM),
        ("post", f"/api/tenants/{tid}/runs/r-x/gates/strategy_review/approve", None),
        ("post", f"/api/tenants/{tid}/runs/r-x/rerun/market", None),
        ("post", f"/api/tenants/{tid}/kill-switch", {"on": True}),
        ("post", f"/api/tenants/{tid}/budget", {"monthly_budget_usd": 99}),
        ("post", f"/api/tenants/{tid}/members", {"email": "x@x.example", "name": "X", "password": PW, "role": "client_viewer"}),
        ("patch", f"/api/tenants/{tid}/members/{uid}", {"role": "client_owner"}),
        ("delete", f"/api/tenants/{tid}/members/{uid}", None),
        ("post", f"/api/tenants/{tid}/leads", {"company": "Acme", "email": "a@acme.example", "full_name": "A B"}),
        ("post", f"/api/tenants/{tid}/strategy/suggestions", {"note": "more about pricing", "parts": ["strategy"]}),
    ]


def ui_write_calls(tid: str, uid: str = "u-x") -> list[tuple[str, str, dict | None]]:
    return [
        ("post", f"/ui/{tid}/posts/p-x", {"action": "approve"}),
        ("post", f"/ui/{tid}/posts/p-x", {"action": "edit", "text": "x"}),
        ("post", f"/ui/{tid}/posts/p-x/posted", {}),
        ("post", f"/ui/{tid}/outreach/m-x", {"action": "approve"}),
        ("post", f"/ui/{tid}/outreach/m-x/send", {}),
        ("post", f"/ui/{tid}/settings", {**FORM}),
        ("post", f"/ui/{tid}/runs", {"kind": "content"}),
        ("post", f"/ui/{tid}/runs/r-x/approve/strategy_review", {}),
        ("post", f"/ui/{tid}/runs/r-x/rerun/market", {}),
        ("get", f"/oauth/google/start?tid={tid}", None),
        ("post", f"/ui/{tid}/integrations/gmail/disconnect", {}),
        ("post", f"/ui/{tid}/kill", {"on": "1"}),
        ("post", f"/ui/{tid}/team", {"email": "x@x.example", "name": "X", "password": PW, "role": "client_viewer"}),
        ("post", f"/ui/{tid}/team/{uid}/role", {"role": "client_owner"}),
        ("post", f"/ui/{tid}/team/{uid}/password", {"password": "another password 1"}),
        ("post", f"/ui/{tid}/team/{uid}/active", {"on": "0"}),
        ("post", f"/ui/{tid}/team/{uid}/remove", {}),
        ("post", f"/ui/{tid}/leads", {"company": "Acme", "email": "a@acme.example", "full_name": "A B"}),
        ("post", f"/ui/{tid}/strategy/suggestions", {"note": "more about pricing", "parts": "strategy"}),
        ("post", "/ui/ops/weekly", {}),
        ("post", "/ui/admin/account-managers", {"email": "am@x.example", "name": "AM", "password": PW}),
        ("post", "/ui/admin/assign", {"user_id": uid, "tenant_id": tid, "on": "1"}),
    ]


async def call(client, method, path, body, headers=None):
    fn = getattr(client, method)
    if method in ("get", "delete"):
        return await fn(path, headers=headers)
    if path.startswith("/api/"):
        return await fn(path, json=body, headers=headers)
    return await fn(path, data=body, headers=headers)


# ---------------------------------------------------------------- the viewer
async def test_viewer_reads_everything_but_every_api_write_is_403(client, team):
    tid, viewer = team["tid"], team["viewer"]
    for path in ("kpis", "calendar", "posts", "leads", "runs", "profile"):
        assert (await client.get(f"/api/tenants/{tid}/{path}", headers=viewer)).status_code == 200, path
    for method, path, body in write_calls(tid):
        r = await call(client, method, path, body, viewer)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])


async def test_viewer_gets_403_on_every_ui_write_with_a_page_naming_the_missing_permission(client, team):
    await ui_login(client, "viewer@a.example")
    for method, path, body in ui_write_calls(team["tid"]):
        r = await call(client, method, path, body)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
    r = await client.post(f"/ui/{team['tid']}/posts/p-x", data={"action": "approve"})
    assert "content:approve" in r.text and "approve, reject or mark posts as posted" in r.text


async def test_viewer_sees_no_write_controls(client, team, db):
    await ui_login(client, "viewer@a.example")
    tid = team["tid"]
    settings = (await client.get(f"/portal/{tid}?tab=settings")).text
    assert "Your role can view but not change the profile" in settings and 'action="/ui/' not in settings
    overview = (await client.get(f"/portal/{tid}")).text
    assert "Start research" not in overview
    assert (await client.get(f"/portal/{tid}?tab=team")).status_code == 403          # the team page is owners only
    assert ">Team<" not in overview                                                  # and not offered in the menu


async def test_a_teammate_signs_in_straight_into_the_workspace(client, team):
    r = await ui_login(client, "viewer@a.example")
    assert (await client.get("/")).headers["location"] == f"/portal/{team['tid']}"      # no onboarding
    assert (await client.get("/onboarding")).status_code == 303                        # can't create a workspace either


# ---------------------------------------------------------------- the founder manages their team
async def test_owner_adds_lists_changes_and_removes_teammates(client, team, db):
    tid, owner = team["tid"], team["owner"]
    assert (await add(client, tid, owner, "cofounder@a.example", role="client_owner")).status_code == 201
    members = (await client.get(f"/api/tenants/{tid}/members", headers=owner)).json()
    by_email = {m["email"]: m for m in members}
    assert {"owner@a.example", "viewer@a.example", "cofounder@a.example"} <= set(by_email)
    assert by_email["viewer@a.example"]["role"] == "client_viewer" and by_email["viewer@a.example"]["active"]
    assert not any("password" in k for m in members for k in m)
    vid = by_email["viewer@a.example"]["user_id"]
    r = await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"password": "a brand new password"}, headers=owner)
    assert r.status_code == 200
    assert (await login(client, "viewer@a.example"))[0] == 401
    assert (await login(client, "viewer@a.example", "a brand new password"))[0] == 200
    assert (await client.delete(f"/api/tenants/{tid}/members/{vid}", headers=owner)).status_code == 204
    assert (await login(client, "viewer@a.example", "a brand new password"))[0] == 401   # removed = account closed
    with tenant_scope(tid):
        async with db.reader() as s:
            actions = [a for (a,) in (await s.execute(select(AuditLog.action)))]
    assert {"team.member_added", "team.member_updated", "team.member_removed"} <= set(actions)


async def test_existing_email_and_short_password_are_refused(client, team):
    tid, owner = team["tid"], team["owner"]
    assert (await add(client, tid, owner, "owner@b.example")).status_code == 409     # someone else's account
    assert (await add(client, tid, owner, "new@a.example", password="short")).status_code == 422


# ---------------------------------------------------------------- escalation
@pytest.mark.parametrize("role", ["platform_admin", "account_manager", "superuser"])
async def test_a_founder_cannot_create_agency_or_unknown_roles(client, team, role):
    r = await add(client, team["tid"], team["owner"], f"{role}@a.example", role=role)
    assert r.status_code == 403
    members = (await client.get(f"/api/tenants/{team['tid']}/members", headers=team["owner"])).json()
    assert not any(m["email"] == f"{role}@a.example" for m in members)


async def test_a_founder_cannot_promote_anyone_to_an_agency_role(client, team):
    tid, owner = team["tid"], team["owner"]
    vid = next(m["user_id"] for m in (await client.get(f"/api/tenants/{tid}/members", headers=owner)).json()
               if m["email"] == "viewer@a.example")
    for role in ("platform_admin", "account_manager"):
        assert (await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"role": role}, headers=owner)).status_code == 403


async def test_signup_ignores_a_requested_role(client, db):
    r = await client.post("/api/auth/signup", json={"name": "Eve", "email": "eve@x.example", "password": PW,
                                                     "role": "platform_admin"})
    assert r.status_code == 201 and r.json()["role"] == "client_owner"


async def test_viewer_cannot_change_their_own_role(client, team):
    tid, viewer = team["tid"], team["viewer"]
    me = (await client.get(f"/api/tenants/{tid}/members", headers=team["owner"])).json()
    vid = next(m["user_id"] for m in me if m["email"] == "viewer@a.example")
    r = await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"role": "client_owner"}, headers=viewer)
    assert r.status_code == 403


# ---------------------------------------------------------------- the last owner
async def test_the_last_owner_cannot_be_demoted_deactivated_or_removed(client, team):
    tid, owner = team["tid"], team["owner"]
    oid = next(m["user_id"] for m in (await client.get(f"/api/tenants/{tid}/members", headers=owner)).json() if m["is_me"])
    for body in ({"role": "client_viewer"}, {"active": False}):
        r = await client.patch(f"/api/tenants/{tid}/members/{oid}", json=body, headers=owner)
        assert r.status_code == 409 and "last owner" in r.text
    assert (await client.delete(f"/api/tenants/{tid}/members/{oid}", headers=owner)).status_code == 409
    assert (await add(client, tid, owner, "second@a.example", role="client_owner")).status_code == 201
    r = await client.patch(f"/api/tenants/{tid}/members/{oid}", json={"role": "client_viewer"}, headers=owner)
    assert r.status_code == 200                                                       # fine once another owner exists


# ---------------------------------------------------------------- cross-tenant
async def test_a_founder_cannot_touch_another_workspaces_team(client, team):
    tid_b, owner_a, owner_b = team["tid_b"], team["owner"], team["owner_b"]
    bid = (await client.get(f"/api/tenants/{tid_b}/members", headers=owner_b)).json()[0]["user_id"]
    assert (await client.get(f"/api/tenants/{tid_b}/members", headers=owner_a)).status_code == 404
    assert (await add(client, tid_b, owner_a, "spy@b.example")).status_code == 404
    assert (await client.patch(f"/api/tenants/{tid_b}/members/{bid}", json={"active": False}, headers=owner_a)).status_code == 404
    # B's member id used under A's own workspace is "not found", not a side door
    assert (await client.patch(f"/api/tenants/{team['tid']}/members/{bid}", json={"active": False}, headers=owner_a)).status_code == 404
    assert (await client.delete(f"/api/tenants/{team['tid']}/members/{bid}", headers=owner_a)).status_code == 404
    assert (await login(client, "owner@b.example"))[0] == 200                       # B is untouched


# ---------------------------------------------------------------- revocation is immediate
async def test_role_change_deactivation_and_removal_apply_on_the_next_request(client, team):
    tid, owner, viewer = team["tid"], team["owner"], team["viewer"]
    vid = next(m["user_id"] for m in (await client.get(f"/api/tenants/{tid}/members", headers=owner)).json()
               if m["email"] == "viewer@a.example")
    assert (await client.put(f"/api/tenants/{tid}/profile", json=FORM, headers=viewer)).status_code == 403
    await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"role": "client_owner"}, headers=owner)
    assert (await client.put(f"/api/tenants/{tid}/profile", json=FORM, headers=viewer)).status_code == 200   # same token
    await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"role": "client_viewer"}, headers=owner)
    assert (await client.put(f"/api/tenants/{tid}/profile", json=FORM, headers=viewer)).status_code == 403
    await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"active": False}, headers=owner)
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=viewer)).status_code == 401              # deactivated
    assert (await login(client, "viewer@a.example"))[0] == 401
    await client.patch(f"/api/tenants/{tid}/members/{vid}", json={"active": True}, headers=owner)
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=viewer)).status_code == 200
    await client.delete(f"/api/tenants/{tid}/members/{vid}", headers=owner)
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=viewer)).status_code == 401              # removed


async def test_a_deactivated_users_browser_session_ends_on_the_next_click(client, team):
    await ui_login(client, "viewer@a.example")
    assert (await client.get(f"/portal/{team['tid']}")).status_code == 200
    vid = next(m["user_id"] for m in (await client.get(f"/api/tenants/{team['tid']}/members", headers=team["owner"])).json()
               if m["email"] == "viewer@a.example")
    await client.patch(f"/api/tenants/{team['tid']}/members/{vid}", json={"active": False}, headers=team["owner"])
    r = await client.get(f"/portal/{team['tid']}")
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------- platform admin
async def test_admin_creates_an_account_manager_and_assigns_clients(client, team, db):
    await make_admin(db)
    _, admin = await login(client, "admin@platform.example")
    r = await client.post("/api/admin/account-managers", json={"email": "am@agency.example", "name": "Sam", "password": PW},
                          headers=admin)
    assert r.status_code == 201
    am_id = r.json()["user_id"]
    _, am = await login(client, "am@agency.example")
    assert (await client.get("/api/tenants", headers=am)).json() == []
    assert (await client.get(f"/api/tenants/{team['tid']}/kpis", headers=am)).status_code == 404
    assert (await client.post("/api/admin/assignments", json={"user_id": am_id, "tenant_id": team["tid"]},
                              headers=admin)).status_code == 200
    assert [t["id"] for t in (await client.get("/api/tenants", headers=am)).json()] == [team["tid"]]
    assert (await client.get(f"/api/tenants/{team['tid']}/kpis", headers=am)).status_code == 200
    assert (await client.get(f"/api/tenants/{team['tid_b']}/kpis", headers=am)).status_code == 404       # not assigned
    # an account manager drafts and runs, but approving and sending stay with the founder
    assert (await client.post(f"/api/tenants/{team['tid']}/posts/p-x/decision", json={"action": "approve"},
                              headers=am)).status_code == 403
    # the founder sees the account manager on the team but cannot change them
    members = (await client.get(f"/api/tenants/{team['tid']}/members", headers=team["owner"])).json()
    am_row = next(m for m in members if m["email"] == "am@agency.example")
    assert am_row["agency"]
    r = await client.patch(f"/api/tenants/{team['tid']}/members/{am_id}", json={"active": False}, headers=team["owner"])
    assert r.status_code == 403
    await client.post("/api/admin/assignments", json={"user_id": am_id, "tenant_id": team["tid"], "on": False}, headers=admin)
    assert (await client.get(f"/api/tenants/{team['tid']}/kpis", headers=am)).status_code == 404       # unassigned at once


async def test_only_the_admin_can_use_the_admin_endpoints(client, team, db):
    await make_admin(db)
    _, admin = await login(client, "admin@platform.example")
    await client.post("/api/admin/account-managers", json={"email": "am2@agency.example", "name": "Ria", "password": PW},
                      headers=admin)
    _, am = await login(client, "am2@agency.example")
    for who in (team["owner"], team["viewer"], am):
        assert (await client.get("/api/admin/overview", headers=who)).status_code == 403
        assert (await client.post("/api/admin/account-managers", json={"email": "z@z.example", "name": "Z", "password": PW},
                                  headers=who)).status_code == 403
        assert (await client.post("/api/admin/assignments", json={"user_id": "x", "tenant_id": team["tid"]},
                                  headers=who)).status_code == 403
    assert (await client.get("/api/admin/overview")).status_code == 401


async def test_admin_data_view_is_searchable_and_never_shows_secrets(client, team, db):
    await make_admin(db)
    _, admin = await login(client, "admin@platform.example")
    d = (await client.get("/api/admin/overview", headers=admin)).json()
    assert {"users", "workspaces", "memberships", "runs", "audit"} <= set(d)
    emails = {u["email"] for u in d["users"]}
    assert {"owner@a.example", "viewer@a.example", "owner@b.example"} <= emails
    blob = json.dumps(d)
    assert "$2b$" not in blob and "password" not in blob.lower() and "secret" not in blob.lower()
    assert get_settings().jwt_secret not in blob
    only_b = (await client.get("/api/admin/overview?q=bravo", headers=admin)).json()
    assert [w["name"] for w in only_b["workspaces"]] == ["Bravo Works"]
    await ui_login(client, "admin@platform.example")
    page = await client.get("/admin?tab=users&q=viewer")
    assert page.status_code == 200 and "viewer@a.example" in page.text and "owner@b.example" not in page.text
    assert "$2b$" not in page.text
