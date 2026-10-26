"""The research backend agents use: live web search + safe fetch, or fixtures when (and only when) models are mocked."""
from __future__ import annotations

from typing import Protocol

from app.core.config import get_settings
from app.integrations.fetch import fetch_page
from app.integrations.search import SearchResponse, get_web_search


class ResearchBackend(Protocol):
    name: str
    synthetic: bool

    async def search(self, query: str) -> SearchResponse: ...
    async def fetch(self, url: str) -> tuple[str, str]: ...           # (title, text)
    def may_fetch(self, url: str, allowed: set[str]) -> bool: ...


class LiveResearch:
    name = "live"
    synthetic = False

    async def search(self, query: str) -> SearchResponse:
        return await get_web_search().search(query)

    async def fetch(self, url: str) -> tuple[str, str]:
        page = await fetch_page(url)
        return page.title, page.text

    def may_fetch(self, url: str, allowed: set[str]) -> bool:
        return url in allowed          # only URLs that came from a search result or from the client's own intake


_live = LiveResearch()


def get_research() -> ResearchBackend:
    if get_settings().uses_fixture_sources:
        from app.fixtures.sources import FixtureResearch   # raises if fixtures are disabled
        return FixtureResearch()
    return _live
