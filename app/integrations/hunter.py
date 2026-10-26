"""Hunter.io: find and verify business emails. Optional until HUNTER_API_KEY is set."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.integrations import http
from app.integrations.errors import (AUTH_FAILED, BAD_RESPONSE, NOT_CONFIGURED, QUOTA, RATE_LIMITED, TIMEOUT, UNAVAILABLE,
                                     IntegrationError)

BASE = "https://api.hunter.io/v2"
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_sem = asyncio.Semaphore(4)   # bounded concurrency; Hunter rate limits are per second


@dataclass
class Contact:
    email: str
    first_name: str
    last_name: str
    position: str
    seniority: str
    department: str
    linkedin_url: str
    confidence: int
    status: str            # valid | accept_all | unknown | invalid | webmail | disposable

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def clean_domain(raw: str) -> str | None:
    d = re.sub(r"^https?://", "", (raw or "").strip().lower()).split("/")[0].split("?")[0]
    d = re.sub(r"^www\.", "", d)
    return d if DOMAIN_RE.match(d) else None


class HunterClient:
    def __init__(self, api_key: str | None = None):
        self.key = api_key if api_key is not None else get_settings().hunter_api_key

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def _get(self, path: str, params: dict) -> dict:
        if not self.key:
            raise IntegrationError("hunter", NOT_CONFIGURED, "HUNTER_API_KEY is not set",
                                   "Create a key at hunter.io -> API, then set HUNTER_API_KEY and restart.")
        last: IntegrationError | None = None
        for attempt in range(3):                                   # bounded retries for transient failures only
            try:
                async with _sem, http.client(timeout=20) as c:
                    r = await c.get(BASE + path, params=params, headers={"X-API-KEY": self.key})
            except httpx.TimeoutException:
                last = IntegrationError("hunter", TIMEOUT, "Hunter.io timed out")
            except httpx.HTTPError as e:
                last = IntegrationError("hunter", UNAVAILABLE, f"Hunter.io unreachable ({type(e).__name__})")
            else:
                if r.status_code == 200:
                    return r.json().get("data", {})
                err_id = ""
                try:
                    err_id = (r.json().get("errors") or [{}])[0].get("id", "")
                except ValueError:
                    pass
                if r.status_code == 401:
                    raise IntegrationError("hunter", AUTH_FAILED, "Hunter.io rejected the API key",
                                           "Check HUNTER_API_KEY at hunter.io -> API.")
                if r.status_code in (403, 429):
                    if err_id in ("rate_limit_exceeded",):
                        last = IntegrationError("hunter", RATE_LIMITED, "Hunter.io rate limit hit")
                        await asyncio.sleep(1.5 * (attempt + 1))
                    else:
                        raise IntegrationError("hunter", QUOTA, "Hunter.io plan quota used up",
                                               "Wait for the monthly reset or upgrade the Hunter.io plan.")
                elif r.status_code == 404:
                    return {}
                elif r.status_code >= 500:
                    last = IntegrationError("hunter", UNAVAILABLE, f"Hunter.io HTTP {r.status_code}")
                else:
                    raise IntegrationError("hunter", BAD_RESPONSE, f"Hunter.io HTTP {r.status_code}: {err_id or 'bad request'}")
            await asyncio.sleep(0.6 * (attempt + 1))
        assert last is not None
        raise last

    async def account(self) -> dict:
        d = await self._get("/account", {})
        req = d.get("requests", {})
        return {"plan": d.get("plan_name", ""), "searches": req.get("searches", {}),
                "verifications": req.get("verifications", {})}

    async def domain_search(self, domain: str, limit: int = 5) -> list[Contact]:
        dom = clean_domain(domain)
        if not dom:
            return []
        d = await self._get("/domain-search", {"domain": dom, "limit": limit, "seniority": "senior,executive",
                                               "type": "personal"})
        out = []
        for e in d.get("emails", []):
            if not e.get("value"):
                continue
            out.append(Contact(
                email=e["value"].lower(), first_name=e.get("first_name") or "", last_name=e.get("last_name") or "",
                position=e.get("position") or "", seniority=e.get("seniority") or "", department=e.get("department") or "",
                linkedin_url=e.get("linkedin") or "", confidence=int(e.get("confidence") or 0),
                status=((e.get("verification") or {}).get("status") or "unknown")))
        return out

    async def verify(self, email: str) -> tuple[str, int]:
        d = await self._get("/email-verifier", {"email": email})
        return d.get("status", "unknown"), int(d.get("score") or 0)


def best_contact(contacts: list[Contact], titles: list[str]) -> Contact | None:
    """Prefer verified addresses, then titles that overlap the ICP, then Hunter's confidence."""
    want = {w for t in titles for w in re.findall(r"[a-z]{3,}", t.lower())}

    def score(c: Contact) -> tuple:
        have = set(re.findall(r"[a-z]{3,}", c.position.lower()))
        return (c.status == "valid", len(want & have), c.confidence)

    usable = [c for c in contacts if c.status not in ("invalid", "disposable", "webmail") and c.first_name]
    return max(usable, key=score) if usable else None
