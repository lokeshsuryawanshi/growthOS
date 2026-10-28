"""Bring your own keys: stored encrypted and bound to the owner, never shown or logged, and only ever used for the
owner's own clients."""
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text, update

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.config import Settings
from app.core.security import Principal
from app.db.models import AccountEvent, AccountSecret, RunNode
from app.integrations import http
from app.llm.gateway import LLMGateway, set_gateway
from app.llm.providers import KeyRejected, Provider, RawResponse
from app.llm.types import CallContext, LLMRequest
from app.main import app
from app.services import keys
from tests.helpers import FAKE_KEYS, add_client, bare_tenants, client_for, give_key, sign_in

ROOT = Path(__file__).resolve().parents[1]
A_KEY, B_KEY = "gsk_" + "A" * 52, "gsk_" + "B" * 52


class RecordingProvider(Provider):
    """Remembers which key it was built with and every call made through it."""
    instances: list["RecordingProvider"] = []

    def __init__(self, provider, key, fail=None):
        self.provider, self.key, self.fail, self.calls = provider, key, fail, 0
        RecordingProvider.instances.append(self)

    async def complete(self, model, req):
        self.calls += 1
        if self.fail:
            raise self.fail
        return RawResponse('{"ok": true}', [], 10, 5)


def live_gateway(fail=None) -> LLMGateway:
    RecordingProvider.instances = []
    return LLMGateway(Settings(_env_file=None, app_env="test", llm_mode="live"),
                      provider_factory=lambda p, k: RecordingProvider(p, k, fail))


@pytest.fixture
async def two_accounts(db):
    await bare_tenants(db, "a1", "a2", owner="u-a")
    await bare_tenants(db, "b1", owner="u-b")
    await give_key("u-a", "groq", A_KEY)
    await give_key("u-b", "groq", B_KEY)
    return db


def ok_transport(seen: list | None = None):
    import httpx2

    def h(r):
        if seen is not None:
            seen.append(r)
        return httpx.Response(200, json={"data": []})
    http.set_transport(httpx.MockTransport(h), httpx2.MockTransport(lambda r: httpx2.Response(200, json={"data": [], "has_more": False})))


# ---------------------------------------------------------------- storage
async def test_a_key_is_checked_then_stored_encrypted_and_bound_to_its_owner(db):
    await bare_tenants(db, "t", owner="u-a")
    seen: list = []
    ok_transport(seen)
    v = await keys.save_key(Principal("u-a", "A"), "llm", "groq", "  " + A_KEY + "\n")
    assert (v.provider, v.last4, v.status) == ("groq", A_KEY[-4:], "valid")
    assert seen[0].url.host == "api.groq.com" and seen[0].headers["authorization"] == f"Bearer {A_KEY}"
    async with db.reader() as s:
        row = (await s.execute(select(AccountSecret))).scalars().one()
    assert A_KEY not in row.ciphertext and A_KEY[4:20] not in row.ciphertext
    assert (await keys.secret("u-a", "llm")).reveal() == A_KEY


async def test_a_ciphertext_copied_to_another_account_or_kind_is_refused(two_accounts):
    db = two_accounts
    async with db.session() as s:
        a_blob = (await s.execute(select(AccountSecret.ciphertext).where(AccountSecret.user_id == "u-a"))).scalar_one()
        await s.execute(update(AccountSecret).where(AccountSecret.user_id == "u-b").values(ciphertext=a_blob))
    assert await keys.secret("u-b", "llm") is None                  # bound to u-a inside the encryption
    assert (await keys.secret("u-a", "llm")).reveal() == A_KEY
    async with db.session() as s:                                    # moved to another kind of the same account
        await s.execute(update(AccountSecret).where(AccountSecret.user_id == "u-a").values(kind="search"))
    assert await keys.secret("u-a", "search") is None


@pytest.mark.parametrize("provider,bad", [("groq", "sk-not-groq-1234567890123456"), ("tavily", "gsk_" + "x" * 40),
                                          ("openai", "sk-ant-api03-" + "x" * 30), ("hunter", "short")])
async def test_keys_in_the_wrong_format_are_refused_without_a_call(db, provider, bad):
    seen: list = []
    ok_transport(seen)
    with pytest.raises(keys.KeyRefused, match="does not look like"):
        await keys.save_key(Principal("u", "U"), keys_kind(provider), provider, bad)
    assert seen == []


def keys_kind(provider):
    return {"groq": "llm", "openai": "llm", "anthropic": "llm", "tavily": "search", "hunter": "contacts"}[provider]


@pytest.mark.parametrize("status,fragment", [(401, "rejected this key"), (402, "no credit")])
async def test_a_key_the_provider_refuses_is_never_stored(db, status, fragment):
    await bare_tenants(db, "t", owner="u-a")
    http.set_transport(httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "nope"})))
    with pytest.raises(keys.KeyRefused, match=fragment) as e:
        await keys.save_key(Principal("u-a", "A"), "search", "tavily", FAKE_KEYS["tavily"])
    assert FAKE_KEYS["tavily"] not in str(e.value)
    assert await keys.list_keys("u-a") == {}


async def test_anthropic_keys_are_checked_through_the_sdk(db):
    import httpx2
    await bare_tenants(db, "t", owner="u-a")
    seen = []
    http.set_transport(None, httpx2.MockTransport(lambda r: seen.append(r) or httpx2.Response(401, json={
        "type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})))
    with pytest.raises(keys.KeyRefused, match="Anthropic rejected"):
        await keys.save_key(Principal("u-a", "A"), "llm", "anthropic", FAKE_KEYS["anthropic"])
    h = seen[0].headers
    assert seen[0].url.path == "/v1/models" and FAKE_KEYS["anthropic"] in (h.get("x-api-key", "") + h.get("authorization", ""))


async def test_replace_and_delete_are_audited_without_the_value(two_accounts):
    db = two_accounts
    await give_key("u-a", "openai")
    assert (await keys.list_keys("u-a"))["llm"].provider == "openai"
    assert await keys.delete_key(Principal("u-a", "A"), "llm")
    async with db.reader() as s:
        assert (await s.execute(select(AccountSecret).where(AccountSecret.user_id == "u-a"))).first() is None   # a row, not a flag
        evs = (await s.execute(select(AccountEvent).where(AccountEvent.user_id == "u-a"))).scalars().all()
    assert [e.action for e in evs].count("key.added") == 1 and "key.replaced" in [e.action for e in evs]
    assert "key.deleted" in [e.action for e in evs]
    assert not any(A_KEY in json.dumps(e.detail) or FAKE_KEYS["openai"] in json.dumps(e.detail) for e in evs)


def test_only_the_keys_module_touches_the_ciphertext():
    allowed = {ROOT / "app/services/keys.py", ROOT / "app/db/models.py"}
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
                 if p not in allowed and "ciphertext" in p.read_text()]
    assert offenders == []


def test_a_secret_value_never_prints_itself():
    v = keys.SecretValue("groq", A_KEY)
    assert A_KEY not in repr(v) and A_KEY not in str(v) and A_KEY not in f"{v}" and repr(v).endswith(A_KEY[-4:] + ">")


# ---------------------------------------------------------------- use: whose key does a run get?
async def test_each_clients_work_uses_its_owners_key_and_nothing_is_shared(two_accounts):
    gw = live_gateway()
    req = LLMRequest(agent="t", tier="small", messages=[{"role": "user", "content": "hi"}])
    for tid in ("a1", "b1", "a2"):
        await gw.complete(req, CallContext(tenant_id=tid))
    used = {p.key: p.calls for p in RecordingProvider.instances}
    assert used == {A_KEY: 2, B_KEY: 1}                              # one client per key; a1 and a2 share A's
    lanes = {tid: await gw.lane(tid) for tid in ("a1", "b1")}
    assert lanes["a1"].fingerprint != lanes["b1"].fingerprint
    assert set(gw.breakers) == {("groq", lanes["a1"].fingerprint), ("groq", lanes["b1"].fingerprint)}
    assert {fp for fp, _ in gw.buckets} == {lanes["a1"].fingerprint, lanes["b1"].fingerprint}
    assert gw._cache_key(req, "a1", "groq") != gw._cache_key(req, "b1", "groq")


async def test_a_client_whose_account_has_no_key_fails_with_the_fix(db):
    await bare_tenants(db, "lonely", owner="u-nokey")
    gw = live_gateway()
    with pytest.raises(Exception) as e:
        await gw.complete(LLMRequest(agent="t", tier="small", messages=[{"role": "user", "content": "x"}]),
                          CallContext(tenant_id="lonely"))
    assert "No AI provider key" in str(e.value) and RecordingProvider.instances == []


async def test_a_revoked_key_fails_the_run_fast_marks_the_key_and_never_leaks(two_accounts, caplog):
    db = two_accounts
    set_gateway(live_gateway(fail=KeyRejected(f"Error code: 401 - Invalid API Key {A_KEY}")))
    set_orchestrator(orch := Orchestrator())
    from app.db.models import ClientProfile, KnowledgeDoc
    from app.core.tenancy import tenant_scope
    with tenant_scope("a1"):
        async with db.session() as s:
            s.add(ClientProfile(intake={"company_name": "A1"}, policy={}))
            for i in range(2):
                s.add(KnowledgeDoc(kind="founder_sample", provider="client", content=f"A writing sample number {i}. " * 5,
                                   trusted=True))
    caplog.set_level(logging.INFO)
    rid = await orch.create_run("a1", "discovery", {"start_date": "2026-11-02"})
    status = await orch.execute("a1", rid)
    assert status == "failed"
    calls = sum(p.calls for p in RecordingProvider.instances)
    assert calls <= 4, calls                                         # one per step that started: no retry storm
    from app.core.tenancy import tenant_scope as ts
    with ts("a1"):
        async with db.reader() as s:
            nodes = (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars().all()
    failed = [n for n in nodes if n.status == "failed"]
    assert failed and all("Groq rejected the saved API key" in n.error or "saved Groq key is not working" in n.error
                      for n in failed)
    assert all(n.attempts == 1 for n in failed)                     # non-retryable: no second attempt
    assert all("Replace the key" in (n.output.get("integration") or {}).get("recovery", "") for n in failed)
    assert (await keys.list_keys("u-a"))["llm"].status == "invalid"
    assert (await keys.list_keys("u-b"))["llm"].status == "valid"    # another account is untouched
    assert A_KEY not in caplog.text and A_KEY[8:30] not in caplog.text
    await assert_no_plaintext_key(db, [A_KEY, B_KEY])


async def assert_no_plaintext_key(db, secrets: list[str]) -> None:
    """Every column of every row of every table, as text."""
    from app.db.models import Base
    async with db.reader() as s:
        for table in Base.metadata.sorted_tables:
            rows = (await s.execute(text(f"SELECT * FROM {table.name}"))).all()
            blob = json.dumps([list(map(str, r)) for r in rows])
            for sec in secrets:
                assert sec not in blob and sec[6:30] not in blob, table.name


# ---------------------------------------------------------------- the browser never gets a key back
async def test_pages_never_contain_a_saved_key_and_the_form_is_write_only(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        await sign_in(c, "a@example.com")
        tid = await add_client(c)
        ok_transport()
        r = await c.post("/account/keys", data={"kind": "llm", "provider": "groq", "key": A_KEY})
        assert r.status_code == 303 and A_KEY not in r.headers["location"] and A_KEY not in r.headers.get("set-cookie", "")
        page = await c.get("/account")
        assert "Groq ···" + A_KEY[-4:] in page.text and page.headers["cache-control"] == "no-store"
        assert re.search(r'name="key" type="password" autocomplete="new-password"', page.text)
        pages = [page.text] + [(await c.get(f"/portal/{tid}?tab={t}")).text for t in
                               ("overview", "content", "calendar", "leads", "research", "activity", "settings")]
        pages.append((await c.get("/clients")).text)
        for html in pages:
            assert A_KEY not in html and A_KEY[4:20] not in html
        http.set_transport(httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": f"bad key {B_KEY}"}})))
        r = await c.post("/account/keys", data={"kind": "llm", "provider": "groq", "key": B_KEY})
        flash = (await c.get(r.headers["location"])).text
        assert "rejected this key" in flash and B_KEY not in flash
    await assert_no_plaintext_key(db, [A_KEY, B_KEY])


async def test_another_account_cannot_replace_or_delete_my_key(two_accounts):
    set_gateway(LLMGateway())
    async with client_for(app) as b:
        await sign_in(b, "u-b@example.com")                           # a different account from the fixture's u-a
        for kind in ("llm", "search", "contacts"):
            await b.post(f"/account/keys/{kind}/delete")
        assert (await b.post("/account/keys/admin/delete")).status_code == 404
    assert (await keys.secret("u-a", "llm")).reveal() == A_KEY        # untouched


async def test_saving_keys_is_rate_limited(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        await sign_in(c)
        http.set_transport(httpx.MockTransport(lambda r: httpx.Response(401, json={})))
        codes = [(await c.post("/account/keys", data={"kind": "search", "provider": "tavily", "key": FAKE_KEYS["tavily"]})).status_code
                 for _ in range(12)]
    assert codes[:10] == [303] * 10 and 429 in codes[10:]


def test_error_text_and_logs_are_scrubbed_of_anything_key_shaped(caplog):
    from app.core.scrub import ScrubFilter, scrub
    msg = (f"401 for {A_KEY}; openai {FAKE_KEYS['openai']}; claude {FAKE_KEYS['anthropic']}; tavily {FAKE_KEYS['tavily']}; "
           f"hunter {FAKE_KEYS['hunter']}; header Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    clean = scrub(msg)
    for v in (A_KEY, *FAKE_KEYS.values(), "abcdefghijklmnopqrstuvwxyz"):
        assert v not in clean
    assert "401 for ***" in clean
    logger = logging.getLogger("scrubtest")
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())
    seen: list[str] = []
    handler.addFilter(ScrubFilter())
    logger.addHandler(handler)
    logger.warning("provider said %s", msg)
    assert seen and all(v not in seen[0] for v in FAKE_KEYS.values())
