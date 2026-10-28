"""Sign in with Google: every check in the flow is exercised against a fake Google that signs real RS256 tokens."""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.core import google_oidc
from app.core.config import get_settings
from app.db.models import User
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from tests.helpers import FakeGoogle, client_for


@pytest.fixture
async def client(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        yield c


@pytest.fixture
def google():
    return FakeGoogle(client_id=get_settings().google_oauth_client_id).install()


async def begin(client) -> dict:
    """Start the flow like the browser does; return the parameters sent to Google."""
    r = await client.get("/auth/google/start")
    assert r.status_code == 303
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert r.headers["location"].startswith(google_oidc.AUTH_URL)
    return q


async def users(db) -> int:
    async with db.reader() as s:
        return (await s.execute(select(func.count(User.id)))).scalar_one()


async def test_start_asks_only_for_openid_email_profile_with_pkce(client):
    q = await begin(client)
    assert q["scope"] == "openid email profile" and q["response_type"] == "code"
    assert q["redirect_uri"] == get_settings().app_base_url + "/auth/google/callback"
    assert q["code_challenge_method"] == "S256" and len(q["state"]) > 20 and len(q["nonce"]) > 20
    flow = google_oidc._flow(client.cookies[google_oidc.FLOW_COOKIE])
    expect = base64.urlsafe_b64encode(hashlib.sha256(flow["v"].encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == expect and flow["st"] == q["state"] and flow["n"] == q["nonce"]


async def test_a_good_sign_in_creates_the_account_once_and_reuses_it(client, db, google):
    q = await begin(client)
    google.next_nonce = q["nonce"]
    r = await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert google.token_requests[0]["code_verifier"] and google.token_requests[0]["grant_type"] == "authorization_code"
    assert (await client.get("/clients")).status_code == 200
    assert await users(db) == 1
    # the same Google account again, with a changed email: same account, email updated (identity is `sub`)
    google.claims = {"email": "new.address@example.com"}
    client.cookies.clear()
    q = await begin(client)
    google.next_nonce = q["nonce"]
    await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert await users(db) == 1
    async with db.reader() as s:
        assert (await s.execute(select(User.email))).scalar_one() == "new.address@example.com"
    # a different Google account is a different, empty account
    google.claims = {"sub": "google-sub-2", "email": "other@example.com"}
    client.cookies.clear()
    q = await begin(client)
    google.next_nonce = q["nonce"]
    await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert await users(db) == 2


@pytest.mark.parametrize("claims,fragment", [
    ({"aud": "someone-elses-client"}, "did not check out"),
    ({"iss": "https://evil.example"}, "not issued by Google"),
    ({"exp": 1_000_000_000}, "did not check out"),
    ({"email_verified": False}, "not verified"),
    ({"nonce": "replayed-nonce"}, "does not belong"),
])
async def test_bad_id_tokens_are_refused(client, db, google, claims, fragment):
    q = await begin(client)
    google.next_nonce = q["nonce"]
    google.claims = claims
    r = await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert r.status_code == 400 and fragment in r.text
    assert await users(db) == 0 and "growthos_token" not in r.headers.get("set-cookie", "")


async def test_a_token_signed_by_another_key_is_refused(client, db, google):
    q = await begin(client)
    impostor = FakeGoogle(client_id=google.client_id)              # same kid, different private key
    google.next_nonce = q["nonce"]
    google.id_token = impostor.id_token                            # type: ignore[method-assign]
    r = await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert r.status_code == 400 and "did not check out" in r.text and await users(db) == 0


async def test_state_mismatch_missing_cookie_and_cancel_are_refused(client, db, google):
    q = await begin(client)
    google.next_nonce = q["nonce"]
    r = await client.get("/auth/google/callback", params={"code": "abc", "state": "attacker-state"})
    assert r.status_code == 400 and "did not match" in r.text
    client.cookies.clear()                                         # started in another browser / expired
    r = await client.get("/auth/google/callback", params={"code": "abc", "state": q["state"]})
    assert r.status_code == 400 and "took too long" in r.text
    q = await begin(client)
    r = await client.get("/auth/google/callback", params={"error": "access_denied", "state": q["state"]})
    assert r.status_code == 400 and "cancelled" in r.text
    assert await users(db) == 0 and google.token_requests == []


async def test_google_refusing_the_code_is_reported(client, db, google):
    google.token_status = 400
    q = await begin(client)
    r = await client.get("/auth/google/callback", params={"code": "used-code", "state": q["state"]})
    assert r.status_code == 400 and "invalid_grant" in r.text and await users(db) == 0


async def test_allowed_emails_limits_who_may_sign_in(client, db, google, monkeypatch):
    monkeypatch.setattr(get_settings(), "allowed_emails", "boss@example.com, Founder.Ops@example.com")
    q = await begin(client)
    google.next_nonce = q["nonce"]
    assert (await client.get("/auth/google/callback", params={"code": "c", "state": q["state"]})).status_code == 303
    monkeypatch.setattr(get_settings(), "allowed_emails", "boss@example.com")
    client.cookies.clear()
    q = await begin(client)
    google.next_nonce = q["nonce"]
    r = await client.get("/auth/google/callback", params={"code": "c", "state": q["state"]})
    assert r.status_code == 400 and "not on the list" in r.text


async def test_without_google_credentials_the_button_explains_instead_of_crashing(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_oauth_client_id", "")
    assert "not set up" in (await client.get("/login")).text
    r = await client.get("/auth/google/start")
    assert r.status_code == 503 and "not set up" in r.text
