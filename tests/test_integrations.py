"""Third-party integrations against mock HTTP transports: success, bad credentials, quotas, outages, never silent."""
import json

import httpx
import pytest

from app.core.config import get_settings
from app.integrations import http
from app.integrations.errors import (AUTH_FAILED, BLOCKED, NOT_CONFIGURED, QUOTA, UNAVAILABLE, IntegrationError)
from app.integrations.fetch import fetch_page, html_to_text
from app.integrations.hunter import Contact, HunterClient, best_contact, clean_domain
from app.integrations.search import WebSearch


def transport(fn):
    http.set_transport(httpx.MockTransport(fn))


# ------------------------------------------------------------------ Hunter.io
def hunter_emails():
    return {"data": {"emails": [
        {"value": "Pat@Acme.com", "first_name": "Pat", "last_name": "Lee", "position": "VP Operations", "seniority": "executive",
         "department": "operations", "confidence": 94, "linkedin": "https://linkedin.com/in/pat", "verification": {"status": "valid"}},
        {"value": "sam@acme.com", "first_name": "Sam", "last_name": "Roy", "position": "Office Manager", "confidence": 60,
         "verification": {"status": "unknown"}},
        {"value": "bad@acme.com", "first_name": "Bad", "last_name": "Bounce", "position": "CEO", "confidence": 90,
         "verification": {"status": "invalid"}}]}}


async def test_hunter_domain_search_parses_and_picks_best_contact():
    seen = {}

    def fn(req):
        seen["key"], seen["url"] = req.headers.get("x-api-key"), str(req.url)
        return httpx.Response(200, json=hunter_emails())
    transport(fn)
    contacts = await HunterClient("k-123").domain_search("https://www.Acme.com/about")
    assert seen["key"] == "k-123" and "k-123" not in seen["url"]          # key goes in a header, never in the URL
    assert "domain=acme.com" in seen["url"]
    assert [c.email for c in contacts][:1] == ["pat@acme.com"]
    best = best_contact(contacts, ["VP of Operations", "COO"])
    assert best.email == "pat@acme.com"                                    # verified + title overlap beats the rest
    assert all(c.email != "bad@acme.com" for c in [best_contact([c for c in contacts if c.email == "bad@acme.com"], [])] if c)


async def test_hunter_without_key_is_not_configured_and_never_calls_out():
    called = []
    transport(lambda r: called.append(r) or httpx.Response(200, json={}))
    with pytest.raises(IntegrationError) as e:
        await HunterClient("").domain_search("acme.com")
    assert e.value.code == NOT_CONFIGURED and "API keys" in e.value.recovery and not called


@pytest.mark.parametrize("status,code", [(401, AUTH_FAILED), (429, QUOTA)])
async def test_hunter_auth_and_quota_failures_are_explicit(status, code):
    transport(lambda r: httpx.Response(status, json={"errors": [{"id": "usage"}]}))
    with pytest.raises(IntegrationError) as e:
        await HunterClient("k").domain_search("acme.com")
    assert e.value.code == code and e.value.recovery


async def test_hunter_retries_server_errors_a_bounded_number_of_times():
    n = {"calls": 0}

    def fn(req):
        n["calls"] += 1
        return httpx.Response(503, json={})
    transport(fn)
    with pytest.raises(IntegrationError) as e:
        await HunterClient("k").verify("a@b.com")
    assert e.value.code == UNAVAILABLE and n["calls"] == 3                # bounded: 3 attempts, then an honest error


async def test_hunter_404_means_no_contacts_not_an_error():
    transport(lambda r: httpx.Response(404, json={}))
    assert await HunterClient("k").domain_search("nowhere.com") == []


def test_domain_cleaning_rejects_garbage():
    assert clean_domain("HTTPS://www.Example.com/path?x=1") == "example.com"
    assert clean_domain("not a domain") is None and clean_domain("localhost") is None and clean_domain("") is None


# ------------------------------------------------------------------ web search (Tavily, the operator's own key)
async def test_search_uses_the_given_key_in_a_header_and_parses_results():
    seen = {}

    def fn(r):
        seen["auth"], seen["url"], seen["body"] = r.headers.get("authorization"), str(r.url), json.loads(r.content)
        return httpx.Response(200, json={"answer": "A", "results": [{"title": "T", "url": "https://x.example/a", "content": "c"}]})
    transport(fn)
    res = await WebSearch("tvly-dev-abc123456789").search('"student housing" site:linkedin.com')
    assert seen["auth"] == "Bearer tvly-dev-abc123456789" and "tvly" not in seen["url"]
    assert seen["body"]["query"] == "student housing"                      # plain words only
    assert res.provider == "tavily" and [h.url for h in res.hits] == ["https://x.example/a"]


async def test_search_without_a_saved_key_says_how_to_fix_it(db):
    from tests.helpers import bare_tenants
    from app.integrations.search import search_for_client
    await bare_tenants(db, "t1")
    with pytest.raises(IntegrationError) as e:
        await search_for_client("t1")
    assert e.value.code == NOT_CONFIGURED and "Tavily key under API keys" in e.value.recovery


@pytest.mark.parametrize("status,code", [(401, AUTH_FAILED), (432, QUOTA), (500, UNAVAILABLE)])
async def test_search_failures_are_explicit_and_never_echo_the_key(status, code):
    transport(lambda r: httpx.Response(status, json={}))
    with pytest.raises(IntegrationError) as e:
        await WebSearch("tvly-dev-secretsecret99").search("q")
    assert e.value.code == code and "secretsecret" not in str(e.value) + e.value.recovery


async def test_an_empty_result_is_an_answer_not_an_outage():
    transport(lambda r: httpx.Response(200, json={"answer": "", "results": []}))
    res = await WebSearch("tvly-dev-abc123456789").search("very niche query")
    assert res.provider == "tavily" and res.hits == []


# ------------------------------------------------------------------ safe fetch
@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
                                 "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x"])
async def test_fetch_blocks_private_addresses_and_odd_schemes(url):
    http.set_transport(None)                                               # real SSRF checks, no network needed for IP literals
    with pytest.raises(IntegrationError) as e:
        await fetch_page(url)
    assert e.value.code == BLOCKED


async def test_fetch_extracts_readable_text_and_drops_scripts():
    html = ("<html><head><title>Acme</title><script>evil()</script></head><body><nav>menu</nav>"
            "<h1>We make widgets</h1><p>" + "Acme builds widgets for factories. " * 5 + "</p><style>.x{}</style></body></html>")
    transport(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
    page = await fetch_page("https://acme.example/")
    assert page.title == "Acme" and "We make widgets" in page.text and "evil()" not in page.text and "menu" not in page.text


async def test_fetch_rejects_non_text_and_error_pages():
    transport(lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    with pytest.raises(IntegrationError):
        await fetch_page("https://acme.example/a.pdf")
    transport(lambda r: httpx.Response(404, text="nope", headers={"content-type": "text/html"}))
    with pytest.raises(IntegrationError):
        await fetch_page("https://acme.example/missing")


def test_html_to_text_title():
    assert html_to_text("<title> Hi </title><p>text here</p>")[0] == "Hi"


def test_search_queries_are_reduced_to_plain_words():
    from app.integrations.search import clean_query
    assert clean_query('"student housing" "new lease" site:linkedin.com') == "student housing new lease"
    assert clean_query('"ecommerce" AND hiring OR Mumbai (expansion)') == "ecommerce hiring Mumbai expansion"


def test_encryption_round_trips_rotates_and_detects_a_changed_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.integrations.crypto import decrypt, encrypt, rotate
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(get_settings(), "encryption_keys", old)
    token = encrypt("secret")
    assert decrypt(token) == "secret"
    monkeypatch.setattr(get_settings(), "encryption_keys", f"{new},{old}")      # rotation: newest first, old still reads
    assert decrypt(token) == "secret"
    rotated = rotate(token)
    monkeypatch.setattr(get_settings(), "encryption_keys", new)
    assert decrypt(rotated) == "secret" and decrypt(token) is None             # retired key: refused, never mis-read
