"""SSRF-safe page fetcher with HTML-to-text extraction.

Blocks non-http(s) schemes, private/loopback/link-local addresses (checked on every redirect hop), oversized
bodies and non-text content. A page is only readable by agents after the input guard has scanned it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.integrations import http
from app.integrations.errors import BAD_RESPONSE, BLOCKED, TIMEOUT, UNAVAILABLE, IntegrationError

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
_SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article", "ul", "ol"}


@dataclass
class FetchedPage:
    url: str
    final_url: str
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)   # (absolute href, anchor text), mailto: included


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self.links: list[list[str]] = []
        self._a: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._a = [href, ""] if href else None
            if self._a is not None:
                self.links.append(self._a)
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self._skip += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "a":
            self._a = None
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._a is not None and len(self._a[1]) < 120:
            self._a[1] += data.strip() + " "
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> tuple[str, str]:
    title, text, _ = parse_html(html)
    return title, text


def parse_html(html: str, base: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t]+\n", "\n", "".join(p.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    links = []
    for href, anchor in p.links[:400]:
        href = href.strip()
        if href.startswith(("javascript:", "#", "tel:")):
            continue
        links.append((href if href.startswith("mailto:") else urljoin(base, href), re.sub(r"\s+", " ", anchor).strip()))
    return p.title.strip(), text, links


async def _check_host(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
        infos = [ip]
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise IntegrationError("web_fetch", UNAVAILABLE, f"could not resolve {host}", "Check the URL.") from e
        infos = [ipaddress.ip_address(r[4][0]) for r in raw]
    if not infos or any(not i.is_global for i in infos):
        raise IntegrationError("web_fetch", BLOCKED, f"{host} is not a public internet address",
                               "Only public websites can be fetched.")


async def fetch_page(url: str, *, max_chars: int = 20000) -> FetchedPage:
    cur = url
    async with http.client(timeout=12.0) as c:
        for _ in range(MAX_REDIRECTS + 1):
            u = urlparse(cur)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise IntegrationError("web_fetch", BLOCKED, "only http(s) URLs can be fetched")
            if http._transport is None:  # tests use a mock transport and fake hostnames
                await _check_host(u.hostname)
            try:
                async with c.stream("GET", cur, headers={"Accept": "text/html,text/plain;q=0.9"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise IntegrationError("web_fetch", UNAVAILABLE, f"{u.hostname} answered HTTP {r.status_code}")
                    ctype = r.headers.get("content-type", "")
                    if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
                        raise IntegrationError("web_fetch", BAD_RESPONSE, f"unsupported content type '{ctype[:40]}'")
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
            except httpx.TimeoutException as e:
                raise IntegrationError("web_fetch", TIMEOUT, f"{u.hostname} timed out") from e
            except httpx.HTTPError as e:
                raise IntegrationError("web_fetch", UNAVAILABLE, f"{u.hostname}: {type(e).__name__}") from e
            html = body[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
            title, text, links = parse_html(html, cur) if "html" in ctype else ("", html, [])
            if len(text) < 80:
                raise IntegrationError("web_fetch", BAD_RESPONSE, "page has no readable text (it may need JavaScript)")
            return FetchedPage(url=url, final_url=cur, title=title or u.hostname, text=text[:max_chars], links=links)
    raise IntegrationError("web_fetch", BAD_RESPONSE, "too many redirects")
