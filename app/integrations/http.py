"""Shared HTTP client factory with sane timeouts. Tests inject a mock transport here."""
from __future__ import annotations

import httpx

_transport: httpx.AsyncBaseTransport | None = None
USER_AGENT = "GrowthOS/1.0 (+https://github.com/lokeshsuryawanshi/growthOS; research bot)"


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Automated tests only: route every integration call through a mock transport."""
    global _transport
    _transport = transport


def client(timeout: float = 15.0, **kw) -> httpx.AsyncClient:
    kw.setdefault("headers", {"User-Agent": USER_AGENT})
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)), transport=_transport,
                             follow_redirects=False, **kw)
