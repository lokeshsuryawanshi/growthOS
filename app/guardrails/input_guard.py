"""Input guard: everything from outside (scraped pages, uploads, lead data) passes through here.

1. Sanitise: strip zero-width characters, HTML comments, scripts/styles and
   hidden elements (common carriers for indirect prompt injection).
2. Score: regex heuristics + the Llama Prompt Guard classifier (live mode).
3. Decide: visible injection -> quarantine (agents never see the content);
   injection only in hidden text -> keep the cleaned text, log a high incident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.gateway import get_gateway
from app.llm.providers import register_mock
from app.llm.types import CallContext, LLMRequest

QUARANTINE_THRESHOLD = 0.8

_ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿]")
_HIDDEN_BLOCKS = [
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I),
    re.compile(r"<(\w+)[^>]*(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)[^>]*>.*?</\1>", re.S | re.I),
]
_TAGS = re.compile(r"<[^>]+>")

# (pattern, weight). Weights are additive and capped at 1.0.
_PATTERNS = [
    (r"ignore (all |any )?(your |the )?(previous|prior|above) (instructions|rules|prompts?)", 0.9),
    (r"disregard (all |your )?(previous|prior|system) (instructions|prompt)", 0.9),
    (r"(print|reveal|show|output) (your |the )?(system prompt|hidden instructions|instructions)", 0.8),
    (r"system override|developer mode|jailbreak|you are now (in|a|an) ", 0.8),
    (r"(ai|llm) (assistant|agent|model)s? reading this", 0.6),
    (r"(rate|score) this (lead|prospect|company) (100|10/10|highly)", 0.7),
    (r"(list|emails?) of (every|all) (other )?clients?", 0.7),
    (r"\bdo not tell (the )?(user|human)\b", 0.6),
]
_COMPILED = [(re.compile(p, re.I), w) for p, w in _PATTERNS]


@dataclass
class InputVerdict:
    clean_text: str
    score: float
    quarantined: bool
    hidden_injection: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.score >= 0.5 or self.hidden_injection


def sanitize(text: str) -> tuple[str, list[str]]:
    removed: list[str] = []
    out = _ZERO_WIDTH.sub("", text)
    if out != text:
        removed.append("zero-width characters")
    for pat in _HIDDEN_BLOCKS:
        for m in pat.finditer(out):
            removed.append(_TAGS.sub(" ", m.group(0))[:200].strip())
        out = pat.sub(" ", out)
    out = _TAGS.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip(), removed


def heuristic_score(text: str) -> tuple[float, list[str]]:
    score, hits = 0.0, []
    for pat, w in _COMPILED:
        m = pat.search(text)
        if m:
            score += w
            hits.append(m.group(0))
    return min(score, 1.0), hits


# Models are only ever shown the start of a stored page (research and writers read at most 1,800 characters of it),
# so the classifier scores exactly that part, one call per page; the regex heuristics still read everything.
CHUNK = 1800             # Prompt Guard's context is short: score chunks and keep the maximum
MODEL_VISIBLE_CHARS = CHUNK


async def _classify(chunk: str, ctx: CallContext) -> float:
    resp = await get_gateway().complete(LLMRequest(agent="input_guard", tier="guard",
                                                   messages=[{"role": "user", "content": chunk}],
                                                   max_tokens=16, cacheable=True), ctx)
    try:
        return float((resp.content or "0").strip())
    except ValueError:
        return 0.0


async def classifier_score(text: str, ctx: CallContext | None, max_chars: int = MODEL_VISIBLE_CHARS) -> float:
    """Prompt Guard returns a probability that the text is an injection/jailbreak."""
    if ctx is None or not text:
        return 0.0
    worst = 0.0
    for i in range(0, min(len(text), max_chars), CHUNK):
        worst = max(worst, await _classify(text[i:i + CHUNK], ctx))
    return worst


async def scan(text: str, ctx: CallContext | None = None, trusted: bool = False) -> InputVerdict:
    """`trusted` is text the client typed themselves (writing samples, facts): heuristics only, no classifier call."""
    clean, removed = sanitize(text)
    raw_h, raw_hits = heuristic_score(_ZERO_WIDTH.sub("", text))
    vis_h, vis_hits = heuristic_score(clean)
    vis_c = 0.0 if trusted else await classifier_score(clean, ctx)
    visible = max(vis_h, vis_c)
    hidden_injection = raw_h >= 0.5 and vis_h < 0.5 and bool(removed)
    reasons = [f"pattern: {h}" for h in (vis_hits or raw_hits)]
    if vis_c >= 0.5:
        reasons.append(f"classifier: {vis_c:.2f}")
    if hidden_injection:
        reasons.append("instructions hidden in markup were stripped")
    return InputVerdict(clean_text=clean, score=max(visible, raw_h if hidden_injection else 0.0),
                        quarantined=visible >= QUARANTINE_THRESHOLD, hidden_injection=hidden_injection,
                        reasons=reasons)


async def scan_many(texts: list[str], ctx: CallContext | None = None) -> list[InputVerdict]:
    """Scan many short third-party snippets (lead signals) with few classifier calls.

    Snippets are packed into chunks and classified together; only a chunk that scores as suspicious is re-scored
    snippet by snippet to find the culprit. Heuristics run on every snippet on its own."""
    verdicts = [await scan(t, None) for t in texts]          # sanitise + heuristics, no model call
    if ctx is None:
        return verdicts
    batch: list[int] = []
    size = 0

    async def flush(idx: list[int], score: float | None = None) -> None:
        """Classify a packed chunk; if it is suspicious, bisect to find the snippet(s) responsible."""
        if not idx:
            return
        if score is None:
            score = await _classify("\n---\n".join(verdicts[i].clean_text for i in idx), ctx)
        if score < 0.5:
            return
        if len(idx) == 1:
            v = verdicts[idx[0]]
            v.score = max(v.score, score)
            v.quarantined = v.quarantined or score >= QUARANTINE_THRESHOLD
            v.reasons.append(f"classifier: {score:.2f}")
            return
        mid = len(idx) // 2
        await flush(idx[:mid])
        await flush(idx[mid:])

    for i, v in enumerate(verdicts):
        n = len(v.clean_text)
        if not n:
            continue
        if n > CHUNK:                                        # too long to pack: score it on its own
            score = await classifier_score(v.clean_text, ctx)
            if score >= 0.5:
                v.score, v.quarantined = max(v.score, score), v.quarantined or score >= QUARANTINE_THRESHOLD
                v.reasons.append(f"classifier: {score:.2f}")
            continue
        if size + n > CHUNK and batch:
            await flush(batch)
            batch, size = [], 0
        batch.append(i)
        size += n + 5
    await flush(batch)
    return verdicts


@register_mock("input_guard")
def _mock_guard(req: LLMRequest) -> dict:
    # Offline stand-in for Prompt Guard: reuse the heuristics.
    score, _ = heuristic_score(req.messages[-1]["content"])
    return {"content": f"{score:.4f}"}
