"""Terms of service and privacy policy: public, short, linked everywhere, and silent about how the product is built."""
import re

import pytest

from app.core.config import get_settings
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from tests.helpers import client_for, sign_in

PAGES = {"/termsofservice": "Terms of service", "/login/policy": "Privacy policy"}
# Vendors, models, hosting, storage, frameworks and internal mechanisms must not appear on these pages.
FORBIDDEN = ["groq", "openai", "gpt", "claude", "anthropic", "gemini", "llama", "qwen", "tavily", "hunter", "render",
             "neon", "postgres", "sqlite", "database", "fastapi", "python", "htmx", "docker", "aws", "vercel", "agent",
             "llm", "language model", "model", "provider", "vendor", "hosting", "server", "fernet", "jwt", "oauth",
             "cookie", "workflow", "orchestrat", "guardrail", "prompt", "growthos-b0t9"]


def visible_text(html: str) -> str:
    html = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<head>.*?</head>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()


@pytest.fixture
async def client(db):
    set_gateway(LLMGateway())
    async with client_for(app) as c:
        yield c


@pytest.mark.parametrize("path,title", PAGES.items())
async def test_pages_are_public_short_phone_ready_and_reveal_nothing_about_the_build(client, path, title):
    r = await client.get(path)
    assert r.status_code == 200 and title in r.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in r.text
    text = visible_text(r.text)
    found = [w for w in FORBIDDEN if re.search(rf"\b{re.escape(w)}", text)]
    assert found == [], found
    assert len(text.split()) < 350                                                   # one screen, plain


async def test_privacy_page_shows_the_contact_email_only_when_configured(client, monkeypatch):
    assert "mailto:" not in (await client.get("/login/policy")).text
    monkeypatch.setattr(get_settings(), "contact_email", "privacy@example.org")
    assert 'href="mailto:privacy@example.org"' in (await client.get("/login/policy")).text


async def test_both_pages_are_linked_from_sign_in_and_every_signed_in_page(client):
    for html in ((await client.get("/login")).text,):
        assert 'href="/termsofservice"' in html and 'href="/login/policy"' in html
    await sign_in(client)
    for path in ("/clients", "/account", "/clients/new"):
        html = (await client.get(path)).text
        assert 'href="/termsofservice"' in html and 'href="/login/policy"' in html, path
    assert (await client.get("/termsofservice")).status_code == 200                # also fine when signed in


async def test_the_reset_warning_banner_reveals_nothing_either(client, monkeypatch):
    monkeypatch.setattr(type(get_settings()), "ephemeral_storage", property(lambda self: True))
    for path in PAGES:
        text = visible_text((await client.get(path)).text)
        assert "wiped" in text and not [w for w in FORBIDDEN if re.search(rf"\b{re.escape(w)}", text)]
