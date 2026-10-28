"""Shared HTTP client factory with sane timeouts. Tests inject a mock transport here.

The Groq and OpenAI SDKs run on httpx; the Anthropic SDK runs on httpx2. Both get the test transport through the
sdk_client helpers below, so no test ever reaches a real provider.
"""
from __future__ import annotations

import httpx

_transport: httpx.AsyncBaseTransport | None = None
_transport2 = None          # httpx2.AsyncBaseTransport, for the Anthropic SDK
USER_AGENT = "GrowthOS/1.0 (+https://github.com/lokeshsuryawanshi/growthOS; research bot)"


def set_transport(transport: httpx.AsyncBaseTransport | None, transport2=None) -> None:
    """Automated tests only: route every integration call through a mock transport."""
    global _transport, _transport2
    _transport, _transport2 = transport, transport2


def client(timeout: float = 15.0, **kw) -> httpx.AsyncClient:
    kw.setdefault("headers", {"User-Agent": USER_AGENT})
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)), transport=_transport,
                             follow_redirects=False, **kw)


def sdk_client(provider: str):
    """The http_client to hand a provider SDK: None (the SDK's own default) unless a test transport is installed."""
    if provider == "anthropic":
        if _transport2 is None:
            return None
        import httpx2
        return httpx2.AsyncClient(transport=_transport2)
    return httpx.AsyncClient(transport=_transport) if _transport is not None else None
