"""Synthetic sources used only with mock models (tests, load test, labelled demo): a fictional prospect
dataset and a canned web corpus. Real runs use app.integrations instead and never touch this module."""
from __future__ import annotations

import json
import re
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.fixtures.web_corpus import PAGES
from app.integrations.search import SearchHit, SearchResponse

DATA = Path(__file__).parent

# Fields the dataset keeps for evals only. Agents must never see them.
_HIDDEN_FIELDS = {"_case", "vertical"}


@lru_cache
def _prospects() -> list[dict[str, Any]]:
    return json.loads((DATA / "prospects.json").read_text())


def _public(p: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in p.items() if k not in _HIDDEN_FIELDS}


GEO_ALIASES = {
    "us": "US", "usa": "US", "united states": "US", "united states of america": "US", "america": "US",
    "uk": "UK", "united kingdom": "UK", "great britain": "UK", "britain": "UK", "england": "UK",
}


def normalize_geo(g: str) -> str:
    return GEO_ALIASES.get(g.strip().lower().replace(".", ""), g.strip())


class SeededLeadSource:
    name = "seeded"

    GENERIC = {"head", "director", "manager", "officer", "chief", "vice", "president", "lead", "leads", "senior",
               "the", "and", "of", "for"}

    def search(self, *, titles: list[str], industries: list[str], min_employees: int = 0,
               max_employees: int = 10**7, geos: list[str] | None = None, exclude_ids: set[str] | None = None,
               limit: int = 40) -> list[dict[str, Any]]:
        """Loose match ranked by relevance, like a real people-search API. Precision is the qualifier's job."""
        exclude_ids = exclude_ids or set()
        tw = {w for t in titles for w in re.findall(r"\w{2,}", t.lower())} - {"the", "and", "of", "for"}
        distinctive = tw - self.GENERIC
        iw = {w for i in industries for w in re.findall(r"\w{4,}", i.lower())}
        ranked = []
        for p in _prospects():
            if p["id"] in exclude_ids:
                continue
            title = set(re.findall(r"\w{2,}", p["title"].lower()))
            ind = p["industry"].lower()
            title_hits = len({w for w in distinctive if any(x.startswith(w[:6]) for x in title)})
            generic_hit = bool((tw - distinctive) & title)
            ind_hit = any(w[:6] in ind for w in iw)
            if not (title_hits or generic_hit or ind_hit):
                continue
            if geos and p["geo"] not in {normalize_geo(g) for g in geos}:
                continue
            if not (min_employees * 0.5 <= p["employees"] <= max_employees * 2):  # loose on size, like real filters
                continue
            score = 3 * title_hits + 2 * ind_hit + 0.5 * generic_hit
            ranked.append((-score, zlib.crc32(p["id"].encode()), p))
        ranked.sort(key=lambda x: (x[0], x[1]))
        return [_public(p) for _, _, p in ranked[:limit]]

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


class FixtureResearch:
    """Same interface as the live research backend, answering from the canned corpus."""
    name = "fixture"
    synthetic = True

    def __init__(self):
        self.corpus = SeededCorpus()

    async def search(self, query: str) -> SearchResponse:
        hits = [SearchHit(r["title"], r["url"], r["snippet"]) for r in self.corpus.search(query)]
        return SearchResponse("fixture", query, hits=hits, synthetic=True)

    async def fetch(self, url: str) -> tuple[str, str]:
        text = self.corpus.fetch(url) or ""
        title = next((p["title"] for p in PAGES if p["url"] == url), url)
        return title, text

    def may_fetch(self, url: str, allowed: set[str]) -> bool:
        return self.corpus.allowed(url)
