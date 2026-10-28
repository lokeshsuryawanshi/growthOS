"""Web search through Tavily with the operator's own key.

There is deliberately no scraping fallback and no canned answer: if search cannot answer, the caller gets an
IntegrationError with the fix.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from app.integrations import http
from app.integrations.errors import AUTH_FAILED, NOT_CONFIGURED, QUOTA, TIMEOUT, UNAVAILABLE, IntegrationError

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResponse:
    provider: str                      # tavily | fixture
    query: str
    answer: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    synthetic: bool = False


def clean_query(q: str, max_words: int = 12) -> str:
    """Plain words only: models like quotes, site: filters and boolean operators, which make search engines return
    nothing (live run 2026-10-27: every lead query came back empty)."""
    q = re.sub(r"\b(site|inurl|intitle|filetype):\S+", " ", q, flags=re.I)
    q = re.sub(r"[\"\u201c\u201d()\[\]]", " ", q)
    q = re.sub(r"\s(AND|OR|NOT)\s", " ", f" {q} ")
    return " ".join(q.split()[:max_words])


class WebSearch:
    """Tavily, with the key of the account that owns the client being worked on. Never a server-wide key."""

    def __init__(self, api_key: str, secret=None):
        self.key = api_key

    async def search(self, query: str) -> SearchResponse:
        query = clean_query(query) or query
        try:
            async with http.client(timeout=20) as c:
                r = await c.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.key}"},
                                 json={"query": query, "max_results": 6, "include_answer": True})
        except httpx.TimeoutException as e:
            raise IntegrationError("tavily", TIMEOUT, "Tavily timed out") from e
        except httpx.HTTPError as e:
            raise IntegrationError("tavily", UNAVAILABLE, f"Tavily unreachable ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise IntegrationError("tavily", AUTH_FAILED, "Tavily rejected the saved API key",
                                   "Replace the Tavily key under API keys & spend, then retry the failed step.")
        if r.status_code in (429, 432, 433):
            raise IntegrationError("tavily", QUOTA, "Tavily quota or rate limit reached",
                                   "Wait for the quota to reset or upgrade the Tavily plan, then retry.")
        if r.status_code >= 400:
            raise IntegrationError("tavily", UNAVAILABLE, f"Tavily HTTP {r.status_code}")
        data = r.json()
        hits = [SearchHit(x.get("title") or x["url"], x["url"], (x.get("content") or "")[:300])
                for x in data.get("results", []) if x.get("url")]
        return SearchResponse("tavily", query, answer=data.get("answer") or "", hits=hits)   # no hits is an answer too


NO_SEARCH_KEY = IntegrationError("web_search", NOT_CONFIGURED, "No web search key is saved for this account",
                                 "Add your Tavily key under API keys & spend (free tier at tavily.com), then retry.")


async def search_for_client(tenant_id: str) -> WebSearch:
    from app.services import keys
    _, sec = await keys.for_client(tenant_id, "search")
    if sec is None:
        raise NO_SEARCH_KEY
    return WebSearch(sec.reveal())
