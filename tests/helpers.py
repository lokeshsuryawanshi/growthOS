"""Test doubles and shortcuts. They exist only in tests: production code never imports them."""
from __future__ import annotations

import base64
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.tenancy import tenant_scope
from app.db.models import Lead
from app.integrations import http

FORM = {"company_name": "Brightwave Analytics", "website": "", "industry": "B2B SaaS: analytics for retailers",
        "company": "Brightwave turns point-of-sale data into weekly demand forecasts for independent retailers.",
        "target_customers": "Owners and operations leads at independent retailers with 5 to 50 stores.",
        "geographies": "UK", "known_competitors": "", "founder_name": "Alex Rivera", "founder_title": "CEO",
        "sample_1": "Retail forecasting is not about the algorithm. It is about knowing which Tuesday matters. We learned that the hard way.",
        "sample_2": "Independent shops do not need dashboards. They need one number on Monday morning that tells them what to order."}


def client_for(app, raise_errors: bool = True) -> AsyncClient:
    """raise_errors=False turns an unhandled exception into the 500 response a browser would get (used by evals)."""
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=raise_errors), base_url="http://testserver")


async def sign_in(client: AsyncClient, email: str = "op@example.com", name: str = "Op Erator") -> AsyncClient:
    """Sign in through the test-only route (it exists only when APP_ENV is test or demo)."""
    r = await client.post("/auth/test-login", data={"email": email, "name": name})
    assert r.status_code == 303, r.text
    return client


async def add_client(client: AsyncClient, **overrides) -> str:
    r = await client.post("/clients/new", data={**FORM, **overrides})
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/portal/")[1].split("?")[0]


async def account(email: str = "op@example.com"):
    """An account created the way Google sign-in creates one, without HTTP."""
    from app.services.accounts import google_user
    return await google_user("test:" + email, email, email.split("@")[0])


async def make_sendable(db, tid: str) -> None:
    """Pretend the fixture leads are real contacts. Only inside tests of the outreach flow."""
    with tenant_scope(tid):
        async with db.session() as s:
            await s.execute(update(Lead).values(is_synthetic=False))


def reset_http() -> None:
    http.set_transport(None)
    from app.core import google_oidc
    from app.agents.research import agents as research
    google_oidc.reset_key_cache()
    research._SEARCH_CACHE.clear()          # per-client search cache: tests reuse client ids


class FakeGoogle:
    """Google's token endpoint and signing keys, with a real RSA key so ID-token checks run for real."""

    def __init__(self, client_id: str = "test-client-id"):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.client_id = client_id
        self.claims: dict = {}
        self.token_status = 200
        self.token_requests: list[dict] = []

    def id_token(self, nonce: str, **over) -> str:
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": self.client_id, "sub": "google-sub-1", "iat": now,
                  "exp": now + 600, "nonce": nonce, "email": "founder.ops@example.com", "email_verified": True,
                  "name": "Sam Operator", **self.claims, **over}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def _jwk(self) -> dict:
        nums = self.key.public_key().public_numbers()

        def b64(n: int) -> str:
            return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode().rstrip("=")
        return {"kty": "RSA", "kid": self.kid, "alg": "RS256", "use": "sig", "n": b64(nums.n), "e": b64(nums.e)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"keys": [self._jwk()]})
        if request.url.host == "oauth2.googleapis.com":
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.token_requests.append(body)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token(self.next_nonce), "access_token": "at"})
        return httpx.Response(404)

    next_nonce = ""

    def install(self) -> "FakeGoogle":
        http.set_transport(httpx.MockTransport(self.handler))
        return self


async def weekly(orch, tid: str) -> tuple[str, str]:
    """What the operator's two weekly buttons do: write the week's posts, then find leads (independent runs)."""
    content = await orch.execute(tid, await orch.create_run(tid, "content", {}))
    leads = await orch.execute(tid, await orch.create_run(tid, "leads", {}))
    return content, leads


async def bare_tenants(db, *tids: str, owner: str = "u-test") -> None:
    """Minimal client rows (and their owning account) for tests below the HTTP layer."""
    from app.db.models import Tenant, User
    async with db.session() as s:
        if await s.get(User, owner) is None:
            s.add(User(id=owner, google_sub="test:" + owner, email=f"{owner}@example.com", name=owner))
            await s.flush()
        for tid in tids:
            s.add(Tenant(id=tid, owner_user_id=owner, slug=tid, name=tid.upper()))


FAKE_KEYS = {"groq": "gsk_" + "a" * 52, "openai": "sk-proj-" + "b" * 40, "anthropic": "sk-ant-api03-" + "c" * 40,
             "tavily": "tvly-dev-" + "d" * 30, "hunter": "e" * 40}
KIND_OF = {"groq": "llm", "openai": "llm", "anthropic": "llm", "tavily": "search", "hunter": "contacts"}


async def give_key(user_id: str, provider: str = "groq", value: str | None = None):
    """Save a key for an account through the real keys service; the provider's check is answered by a mock (200)."""
    from app.core.security import Principal
    from app.services import keys
    saved = (http._transport, http._transport2)
    import httpx2
    http.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "account": {}})),
                       httpx2.MockTransport(lambda r: httpx2.Response(200, json={"data": [], "has_more": False,
                                                                                  "first_id": None, "last_id": None})))
    try:
        return await keys.save_key(Principal(user_id, user_id), KIND_OF[provider], provider,
                                   value or FAKE_KEYS[provider])
    finally:
        http.set_transport(*saved)
