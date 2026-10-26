"""Adapters to the outside world, behind small interfaces so real providers drop in later.

* LeadSource: seeded prospect dataset (Apollo / Sales Navigator / Clay would plug in here).
* ResearchProvider: Gemini with Google Search grounding when available, seeded corpus otherwise.
* Publisher / Sender: mock implementations; only reachable from human-approved actions.
"""
from __future__ import annotations

import json
import logging
import re
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.adapters.data.web_corpus import PAGES
from app.core.config import get_settings

log = logging.getLogger(__name__)
DATA = Path(__file__).parent / "data"

# Fields the dataset keeps for evals only. Agents must never see them.
_HIDDEN_FIELDS = {"_case", "vertical"}


@lru_cache
def _prospects() -> list[dict[str, Any]]:
    return json.loads((DATA / "prospects.json").read_text())


def _public(p: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in p.items() if k not in _HIDDEN_FIELDS}


class SeededLeadSource:
    name = "seeded"

    def search(self, *, titles: list[str], industries: list[str], min_employees: int = 0,
               max_employees: int = 10**7, geos: list[str] | None = None, exclude_ids: set[str] | None = None,
               limit: int = 40) -> list[dict[str, Any]]:
        """Loose keyword match, like a real people-search API: precision is the qualifier's job."""
        exclude_ids = exclude_ids or set()
        tw = {w for t in titles for w in re.findall(r"\w{3,}", t.lower())} - {"head", "the", "and", "of"}
        iw = {w for i in industries for w in re.findall(r"\w{4,}", i.lower())}
        out = []
        # Real people-search APIs return a mix; a stable hash order keeps it deterministic.
        for p in sorted(_prospects(), key=lambda r: zlib.crc32(r["id"].encode())):
            if p["id"] in exclude_ids:
                continue
            title_hit = any(w in p["title"].lower() for w in tw)
            ind_hit = any(w in p["industry"].lower() for w in iw)
            if not (title_hit or ind_hit):
                continue
            if geos and p["geo"] not in geos:
                continue
            if not (min_employees * 0.5 <= p["employees"] <= max_employees * 2):  # loose on size, like real filters
                continue
            out.append(_public(p))
            if len(out) >= limit:
                break
        return out

    def get(self, prospect_id: str) -> dict[str, Any] | None:
        return next((_public(p) for p in _prospects() if p["id"] == prospect_id), None)


def dataset_case(prospect_id: str, vertical: str | None = None) -> str:
    """Ground-truth label for evals/tests only. With `vertical`, a fit for another client counts as low_fit."""
    p = next((p for p in _prospects() if p["id"] == prospect_id), None)
    if p is None:
        return "unknown"
    if vertical and p["vertical"] != vertical and p["_case"] == "fit":
        return "low_fit"
    return p["_case"]


class SeededCorpus:
    name = "seeded_corpus"

    def search(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        q = set(re.findall(r"\w{3,}", query.lower()))
        scored = []
        for page in PAGES:
            title = set(re.findall(r"\w+", page["title"].lower()))
            tags = set(re.findall(r"\w+", " ".join(page["tags"]).lower()))
            body = set(re.findall(r"\w+", page["text"].lower()))
            score = 3 * len(q & title) + 2 * len(q & tags) + len(q & body)
            if score:
                scored.append((score, page))
        scored.sort(key=lambda x: -x[0])
        return [{"url": p["url"], "title": p["title"], "snippet": re.sub(r"<[^>]+>", " ", p["text"])[:180]}
                for _, p in scored[:limit]]

    def fetch(self, url: str) -> str | None:
        page = next((p for p in PAGES if p["url"] == url), None)
        return page["text"] if page else None

    def allowed(self, url: str) -> bool:
        return any(p["url"] == url for p in PAGES)


class ResearchProvider:
    """Live grounded search first; any failure (quota, outage, open circuit) falls back to the corpus."""

    def __init__(self):
        self.corpus = SeededCorpus()
        self.live_disabled_until: float = 0.0

    async def search(self, query: str) -> dict[str, Any]:
        from app.llm.gateway import get_gateway
        gw = get_gateway()
        gemini = gw.providers.get("gemini")
        now = datetime.now(timezone.utc).timestamp()
        if gemini is not None and now >= self.live_disabled_until:
            try:
                res = await gemini.grounded_search(get_settings().research_model, query)  # type: ignore[attr-defined]
                if res.get("answer"):
                    return {"provider": "gemini_search", **res}
            except Exception as e:  # quota/outage: stop trying for 10 minutes
                log.warning("grounded search unavailable, using seeded corpus: %s", str(e)[:120])
                self.live_disabled_until = now + 600
        return {"provider": "seeded_corpus", "results": self.corpus.search(query)}


_research: ResearchProvider | None = None


def get_research() -> ResearchProvider:
    global _research
    if _research is None:
        _research = ResearchProvider()
    return _research


class MockPublisher:
    async def publish(self, tenant_id: str, post_id: str, text: str) -> dict[str, str]:
        return {"external_id": f"li-post-{post_id}", "status": "published (mock)"}


class MockSender:
    async def send(self, tenant_id: str, message_id: str, to: str, text: str) -> dict[str, str]:
        return {"external_id": f"li-msg-{message_id}", "status": "sent (mock)"}
