"""A signed token is only a claim. The database decides who is logged in, with what role, on which workspace."""
import pytest
from sqlalchemy import delete, update

from app.db.models import Membership, Tenant, User
from tests.test_accounts import FORM, client, signup, workspace  # noqa: F401  (client is a fixture)


async def user_id(db, email):
    from sqlalchemy import select
    async with db.reader() as s:
        return (await s.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_token_for_a_wiped_database_is_rejected_not_a_foreign_key_crash(client, db):
    """Regression: free hosting resets the database on redeploy; the browser keeps a valid cookie for a user that is gone."""
    _, h = await signup(client, "ghost@x.example")
    uid = await user_id(db, "ghost@x.example")
    async with db.session() as s:
        await s.execute(delete(User).where(User.id == uid))               # the database was reset
    r = await client.post("/api/tenants", json=FORM, headers=h)
    assert r.status_code == 401                                          # previously: IntegrityError, FOREIGN KEY constraint failed
    assert (await client.get("/api/tenants", headers=h)).status_code == 401


async def test_stale_browser_session_is_sent_to_login_without_a_loop(client, db):
    r = await client.post("/signup", data={"name": "Ghost", "email": "ghost2@x.example", "password": "correct horse battery"})
    token = r.cookies["growthos_token"]
    async with db.session() as s:
        await s.execute(delete(User).where(User.email == "ghost2@x.example"))
    client.cookies.set("growthos_token", token)
    for path in ("/onboarding", "/", "/portal/anything"):
        resp = await client.get(path)
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path
        assert "growthos_token" in resp.headers.get("set-cookie", "")        # the dead cookie is cleared
    assert (await client.get("/login")).status_code == 200                   # and /login does not bounce back


async def test_deactivated_user_is_locked_out_immediately(client, db):
    tid, h = await workspace(client, "gone@x.example")
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).status_code == 200
    async with db.session() as s:
        await s.execute(update(User).where(User.email == "gone@x.example").values(active=False))
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).status_code == 401


async def test_role_change_takes_effect_on_the_next_request(client, db):
    tid, h = await workspace(client, "demoted@x.example")                    # token was issued while a founder
    async with db.session() as s:
        await s.execute(update(User).where(User.email == "demoted@x.example").values(role="client_viewer"))
    assert (await client.post(f"/api/tenants/{tid}/runs", json={"kind": "discovery"}, headers=h)).status_code == 403
    assert (await client.put(f"/api/tenants/{tid}/profile", json=FORM, headers=h)).status_code == 403
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).status_code == 200      # can still read


async def test_removed_membership_takes_effect_on_the_next_request(client, db):
    tid, h = await workspace(client, "removed@x.example")
    uid = await user_id(db, "removed@x.example")
    async with db.session() as s:
        await s.execute(delete(Membership).where(Membership.user_id == uid))
    assert (await client.get(f"/api/tenants/{tid}/kpis", headers=h)).status_code == 404
    assert (await client.get("/api/tenants", headers=h)).json() == []
