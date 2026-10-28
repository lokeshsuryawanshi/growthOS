"""A signed session cookie is only a claim. The database decides who is signed in and which clients they own."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import User
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from tests.helpers import FORM, add_client, client_for, sign_in


@pytest.fixture
async def client(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        yield c


async def test_cookie_for_a_wiped_database_goes_to_login_not_a_foreign_key_crash(client, db):
    """Regression (the reviewer's first sign-up error): free hosting reset the database while the browser kept a valid
    cookie for an account that no longer existed, and onboarding failed with a FOREIGN KEY error (HTTP 500)."""
    await sign_in(client, "ghost@x.example")
    async with db.session() as s:
        await s.execute(delete(User))                                            # the database was reset
    r = await client.post("/clients/new", data=FORM)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/", "/clients", "/clients/new", "/account", "/portal/anything"):
        resp = await client.get(path)
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path
        assert "growthos_token" in resp.headers.get("set-cookie", "")          # the dead cookie is cleared
    assert (await client.get("/login")).status_code == 200                       # and /login does not bounce back


def _cookie(sub: str, secret: str | None = None, minutes: int = 60) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": sub, "iat": now, "exp": now + timedelta(minutes=minutes)},
                      secret or get_settings().jwt_secret, algorithm="HS256")


async def test_forged_expired_and_malformed_cookies_are_signed_out(client, db):
    await sign_in(client, "real@x.example")
    real = client.cookies["growthos_token"]
    uid = jwt.decode(real, get_settings().jwt_secret, algorithms=["HS256"])["sub"]
    for bad in (_cookie(uid, secret="guessed-secret-guessed-secret-guessed"),   # forged
                _cookie(uid, minutes=-5),                                        # expired
                _cookie("no-such-user"),                                         # validly signed, unknown account
                "not.a.jwt", ""):
        client.cookies.set("growthos_token", bad)
        r = await client.get("/clients")
        assert r.status_code == 303 and r.headers["location"] == "/login", bad[:20]
    client.cookies.set("growthos_token", real)
    assert (await client.get("/clients")).status_code == 200


async def test_a_deleted_client_is_gone_on_the_next_request(client, db):
    await sign_in(client)
    tid = await add_client(client)
    assert (await client.get(f"/portal/{tid}")).status_code == 200
    await client.post(f"/ui/{tid}/delete", data={"confirm": FORM["company_name"]})
    assert (await client.get(f"/portal/{tid}")).status_code == 404
