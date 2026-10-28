"""Open in Gmail / Copy email: the links must carry exactly the approved text, whatever characters it contains."""
from urllib.parse import parse_qs, urlparse, unquote

from app.services.compose import MAX_URL, SHORT_NOTE, build


def gmail_params(url: str) -> dict:
    u = urlparse(url)
    assert (u.scheme, u.netloc, u.path) == ("https", "mail.google.com", "/mail/")
    return {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}


def test_unicode_ampersands_and_newlines_survive_the_round_trip():
    body = "Hi Zoë,\n\nR&D at Müller & Söhne: 30% faster? #1 = yes + more.\nLine 3 → done ✓\n\n— Priya"
    c = build("zoe@müller.example", "Q&A: 2 ideas = 1 call?", body)
    q = gmail_params(c.gmail_url)
    assert q == {"view": "cm", "fs": "1", "to": "zoe@müller.example", "su": "Q&A: 2 ideas = 1 call?", "body": body}
    assert " " not in c.gmail_url and "\n" not in c.gmail_url                     # fully encoded
    assert not c.shortened and c.copy_text == f"Subject: Q&A: 2 ideas = 1 call?\n\n{body}"
    m = urlparse(c.mailto_url)
    assert m.scheme == "mailto" and unquote(m.path) == "zoe@müller.example"
    assert parse_qs(m.query)["body"][0] == body


def test_no_recipient_still_gives_working_links():
    c = build("", "Hello", "Body")
    assert gmail_params(c.gmail_url)["to"] == "" and c.copy_text.endswith("Body")


def test_newlines_in_the_subject_cannot_inject_headers():
    c = build("a@b.example", "Hi\r\nBcc: victim@x.example", "x")
    assert gmail_params(c.gmail_url)["su"] == "Hi  Bcc: victim@x.example"
    assert "%0A" not in c.mailto_url.split("body=")[0] and "%0D" not in c.mailto_url


def test_a_long_email_gets_a_shortened_link_with_a_note_and_copy_keeps_everything():
    body = ("This paragraph is long enough to matter, with some ümlauts & symbols. " * 60).strip()
    c = build("pat@acme.example", "Subject", body)
    assert c.shortened and len(c.gmail_url) <= MAX_URL
    sent = gmail_params(c.gmail_url)["body"]
    assert sent.endswith(SHORT_NOTE) and body.startswith(sent[: -len(SHORT_NOTE)])
    assert len(sent) > 200                                                          # as much as fits, not a stub
    assert c.copy_text.endswith(body) and c.body == body                           # copy has the full text
