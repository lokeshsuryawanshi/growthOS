"""Web search with an honest, labelled fallback chain.

Order: Gemini Google-Search grounding -> Tavily. Both need a key; there is deliberately no scraping fallback.
Every response says which provider produced it and which providers were skipped and why, so the UI can show
provenance. If no provider can answer the caller gets an IntegrationError with the fix. There is no canned fallback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import get_settings
from app.integrations import http
from app.integrations.errors import (AUTH_FAILED, BAD_RESPONSE, NOT_CONFIGURED, QUOTA, RATE_LIMITED, TIMEOUT, UNAVAILABLE,
                                     IntegrationError)

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResponse:
    provider: str                      # gemini_search | tavily | fixture
    query: str
    answer: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # "gemini_search: quota exceeded"
    synthetic: bool = False


class WebSearch:
    def __init__(self):
        self._disabled_until: dict[str, float] = {}

    def _usable(self, name: str) -> bool:
        return time.time() >= self._disabled_until.get(name, 0.0)

    def _disable(self, name: str, seconds: float) -> None:
        self._disabled_until[name] = time.time() + seconds

    # -- providers -------------------------------------------------------------
    async def _gemini(self, query: str) -> SearchResponse:
        from app.llm.gateway import get_gateway
        from app.llm.providers import FatalError, RateLimited, TransientError
        gem = get_gateway().providers.get("gemini")
        if gem is None:
            raise IntegrationError("gemini_search", NOT_CONFIGURED, "GOOGLE_API_KEY is not set")
        try:
            res = await gem.grounded_search(get_settings().research_model, query)  # type: ignore[attr-defined]
        except RateLimited as e:
            raise IntegrationError("gemini_search", QUOTA, "Gemini search quota exceeded") from e
        except FatalError as e:
            raise IntegrationError("gemini_search", AUTH_FAILED, str(e)[:160]) from e
        except TransientError as e:
            raise IntegrationError("gemini_search", UNAVAILABLE, str(e)[:160]) from e
        if not res.get("answer"):
            raise IntegrationError("gemini_search", BAD_RESPONSE, "empty grounded answer")
        hits = [SearchHit(s["title"] or s["url"], s["url"]) for s in res["sources"]]
        return SearchResponse("gemini_search", query, answer=res["answer"], hits=hits)

    async def _tavily(self, query: str) -> SearchResponse:
        key = get_settings().tavily_api_key
        if not key:
            raise IntegrationError("tavily", NOT_CONFIGURED, "TAVILY_API_KEY is not set")
        try:
            async with http.client(timeout=20) as c:
                r = await c.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                                 json={"query": query, "max_results": 6, "include_answer": True})
        except httpx.TimeoutException as e:
            raise IntegrationError("tavily", TIMEOUT, "Tavily timed out") from e
        except httpx.HTTPError as e:
            raise IntegrationError("tavily", UNAVAILABLE, type(e).__name__) from e
        if r.status_code in (401, 403):
            raise IntegrationError("tavily", AUTH_FAILED, "Tavily rejected the API key")
        if r.status_code in (429, 432, 433):
            raise IntegrationError("tavily", QUOTA, "Tavily quota or rate limit reached")
        if r.status_code >= 400:
            raise IntegrationError("tavily", UNAVAILABLE, f"Tavily HTTP {r.status_code}")
        data = r.json()
        hits = [SearchHit(x.get("title") or x["url"], x["url"], (x.get("content") or "")[:300])
                for x in data.get("results", []) if x.get("url")]
        if not hits:
            raise IntegrationError("tavily", BAD_RESPONSE, "no results")
        return SearchResponse("tavily", query, answer=data.get("answer") or "", hits=hits)

    # -- chain -----------------------------------------------------------------
    async def search(self, query: str) -> SearchResponse:
        s = get_settings()
        chain = [("gemini_search", self._gemini, bool(s.google_api_key)),
                 ("tavily", self._tavily, bool(s.tavily_api_key))]
        skipped: list[str] = []
        last: IntegrationError | None = None
        for name, fn, configured in chain:
            if not configured or not self._usable(name):
                continue
            try:
                res = await fn(query)
            except IntegrationError as e:
                last = e
                skipped.append(f"{name}: {e.message}")
                self._disable(name, 600 if e.code in ("quota_exceeded", "auth_failed") else 60)
                log.warning("search provider %s failed: %s", name, e.message)
                continue
            res.skipped = skipped
            return res
        if not any(c[2] for c in chain):
            raise IntegrationError("web_search", NOT_CONFIGURED, "no web search provider is configured",
                                   "Set TAVILY_API_KEY (free tier at tavily.com) or GOOGLE_API_KEY with billing enabled "
                                   "for Google Search grounding.")
        raise IntegrationError("web_search", UNAVAILABLE, "no web search provider could answer: " + "; ".join(skipped),
                               "Retry in a few minutes. If Gemini reports quota exceeded, enable billing on the Google "
                               "key or set TAVILY_API_KEY.") from last


_search: WebSearch | None = None


def get_web_search() -> WebSearch:
    global _search
    if _search is None:
        _search = WebSearch()
    return _search


def reset_web_search() -> None:
    global _search
    _search = None
