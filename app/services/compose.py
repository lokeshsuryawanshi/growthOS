"""Hand an approved email to the operator's own mail client. Nothing is sent from the server.

Two ways, always both: an "Open in Gmail" compose link (opens a new tab with recipient, subject and body filled in) and
"Copy email" (subject and body to the clipboard). A mailto: link is offered as a third choice.

Browsers and Gmail truncate or refuse very long URLs. Above MAX_URL characters the Gmail link carries a shortened body
with a note saying so, and Copy still copies the full text.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

GMAIL = "https://mail.google.com/mail/"
MAX_URL = 1900          # conservative: some browsers and proxies cut URLs past ~2,000 characters
SHORT_NOTE = "\n\n[Shortened to fit the link: paste the full text from Growth OS with Copy email.]"


@dataclass
class Compose:
    to: str
    subject: str
    body: str               # the full body, footer included
    gmail_url: str
    mailto_url: str
    copy_text: str          # "Subject: ...\n\nbody"
    shortened: bool         # the Gmail link carries a shortened body


def _gmail(to: str, subject: str, body: str) -> str:
    return GMAIL + "?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}, quote_via=quote)


def build(to: str, subject: str, body: str) -> Compose:
    to, subject = (to or "").strip(), (subject or "").replace("\r", " ").replace("\n", " ").strip()
    body = (body or "").replace("\r\n", "\n")
    url, shortened = _gmail(to, subject, body), False
    if len(url) > MAX_URL:
        shortened = True
        lo, hi = 0, len(body)
        while lo < hi:                               # longest prefix that still fits once the note is added
            mid = (lo + hi + 1) // 2
            if len(_gmail(to, subject, body[:mid].rstrip() + SHORT_NOTE)) <= MAX_URL:
                lo = mid
            else:
                hi = mid - 1
        url = _gmail(to, subject, body[:lo].rstrip() + SHORT_NOTE)
    mailto = "mailto:" + quote(to, safe="@") + "?" + urlencode({"subject": subject, "body": body}, quote_via=quote)
    return Compose(to=to, subject=subject, body=body, gmail_url=url, mailto_url=mailto,
                   copy_text=f"Subject: {subject}\n\n{body}", shortened=shortened)


def for_message(msg, lead, intake: dict) -> Compose:
    from app.agents.leads.agents import email_footer
    subject = msg.subject or f"Quick question for {lead.company}"
    return build(lead.email or "", subject, (msg.approved_text or msg.text) + email_footer(intake))
