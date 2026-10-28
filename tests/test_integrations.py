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
    assert e.value.code == NOT_CONFIGURED and "HUNTER_API_KEY" in e.value.recovery and not called


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


# ------------------------------------------------------------------ web search chain
async def test_search_fails_loudly_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_api_key", "")
    monkeypatch.setattr(get_settings(), "tavily_api_key", "")
    with pytest.raises(IntegrationError) as e:
        await WebSearch().search("anything")
    assert e.value.code == NOT_CONFIGURED and "TAVILY_API_KEY" in e.value.recovery


async def test_search_falls_back_and_labels_the_provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "tavily_api_key", "tv-key")
    monkeypatch.setattr(get_settings(), "search_providers", "gemini_search,tavily")
    ws = WebSearch()

    async def gemini_over_quota(q):
        raise IntegrationError("gemini_search", QUOTA, "Gemini search quota exceeded")
    monkeypatch.setattr(ws, "_gemini", gemini_over_quota)
    monkeypatch.setattr(get_settings(), "google_api_key", "g-key")
    transport(lambda r: httpx.Response(200, json={"answer": "A", "results": [{"title": "T", "url": "https://x.example/a", "content": "c"}]}))
    res = await ws.search("q")
    assert res.provider == "tavily" and res.skipped == ["gemini_search: Gemini search quota exceeded"]
    assert [h.url for h in res.hits] == ["https://x.example/a"]
    # the quota failure is remembered, so the next query does not waste a call on the dead provider
    assert ws._usable("gemini_search") is False


async def test_tavily_is_tried_first_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "tavily_api_key", "tv-key")
    monkeypatch.setattr(get_settings(), "google_api_key", "g-key")
    ws = WebSearch()

    async def gemini_should_not_run(q):
        raise AssertionError("Gemini grounding was called although Tavily answered")
    monkeypatch.setattr(ws, "_gemini", gemini_should_not_run)
    transport(lambda r: httpx.Response(200, json={"answer": "", "results": [{"title": "T", "url": "https://x.example/b", "content": "c"}]}))
    assert (await ws.search("q")).provider == "tavily"


async def test_search_reports_every_provider_failure(monkeypatch):
    monkeypatch.setattr(get_settings(), "tavily_api_key", "tv-key")
    monkeypatch.setattr(get_settings(), "google_api_key", "")
    transport(lambda r: httpx.Response(432, json={}))
    with pytest.raises(IntegrationError) as e:
        await WebSearch().search("q")
    assert e.value.code == UNAVAILABLE and "tavily" in e.value.message


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


async def test_an_empty_result_is_an_answer_not_an_outage(monkeypatch):
    monkeypatch.setattr(get_settings(), "tavily_api_key", "tv-key")
    monkeypatch.setattr(get_settings(), "google_api_key", "")
    ws = WebSearch()
    transport(lambda r: httpx.Response(200, json={"answer": "", "results": []}))
    res = await ws.search("very niche query")
    assert res.provider == "tavily" and res.hits == [] and ws._usable("tavily")     # still usable for the next query


def test_any_encryption_key_value_works_and_round_trips(monkeypatch):
    """Regression: a non-Fernet ENCRYPTION_KEY (e.g. a passphrase) used to crash with ValueError."""
    from app.integrations.crypto import decrypt, encrypt
    for key in ("not-a-fernet-key-just-words", "", "q0x6pUm8wuh9cUQBF1uqnrLKF0UQiS2Mfeg2rKeVc0s="):
        monkeypatch.setattr(get_settings(), "encryption_key", key)
        assert decrypt(encrypt('{"refresh_token": "rt"}')) == '{"refresh_token": "rt"}'
    monkeypatch.setattr(get_settings(), "encryption_key", "passphrase-one")
    token = encrypt("secret")
    monkeypatch.setattr(get_settings(), "encryption_key", "passphrase-two")
    assert decrypt(token) is None                       # a changed key is detected, never mis-decrypted
