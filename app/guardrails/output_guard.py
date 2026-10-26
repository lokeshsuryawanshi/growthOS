"""Output guard: deterministic policy checks on every post and outreach message before a human sees it.

Severity decides the outcome:
* critical  -> blocked, never auto-fixed (leakage, canary, PII)
* high      -> blocked; one automatic rewrite is attempted with the violations as instructions
* medium/low -> shown to the reviewer as warnings
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.agents.runtime.loop import CANARY

BLOCKING = {"critical", "high"}

GLOBAL_BANNED_PHRASES = [
    "game-changer", "game changer", "revolutionize", "in today's fast-paced world", "unlock the power",
    "synergy", "guaranteed results", "10x your", "secret hack", "nobody is talking about",
]
NEGATIVE_WORDS = ["terrible", "scam", "worse", "worst", "fails", "failing", "garbage", "lying", "liars", "overpriced",
                  "incompetent", "useless", "rip-off", "rip off", "leaks", "fraud", "awful", "broken"]
SPAM_SIGNALS = ["act now", "limited time", "100% free", "click here", "buy now", "don't miss out", "!!!", "risk-free"]
FALSE_FAMILIARITY = ["as we discussed", "following up on our call", "great to meet you", "great meeting you",
                     "we met at", "per our conversation", "as promised"]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,2}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
_STAT = re.compile(r"(?:[$£€]\s?\d[\d,.]*\s?(?:k|m|bn|million|billion)?|\d[\d,.]*\s?(?:%|percent|x\b|times\b|k\b|million|billion))", re.I)


class Violation(BaseModel):
    rule: str
    severity: str
    detail: str


class GuardReport(BaseModel):
    passed: bool
    violations: list[Violation] = []
    warnings: list[Violation] = []

    def instructions(self) -> str:
        return "\n".join(f"- [{v.rule}] {v.detail}" for v in self.violations + self.warnings)

    @property
    def critical(self) -> bool:
        return any(v.severity == "critical" for v in self.violations)


@dataclass
class PolicyContext:
    tenant_name: str
    competitors: list[str] = field(default_factory=list)
    banned_topics: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    required_disclaimer: dict | None = None
    other_tenant_terms: list[str] = field(default_factory=list)  # names/founders/products of other clients
    source_texts: list[str] = field(default_factory=list)        # facts + fetched sources a stat may cite
    previous_posts: list[str] = field(default_factory=list)


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I) is not None


def _norm_num(s: str) -> str:
    return re.sub(r"[^\d.]", "", s).rstrip(".")


def _shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _common(text: str, p: PolicyContext) -> list[Violation]:
    v: list[Violation] = []
    if CANARY in text:
        v.append(Violation(rule="system_prompt_leak", severity="critical", detail="internal marker found in output"))
    for term in p.other_tenant_terms:
        if len(term) > 3 and _contains(text, term):
            v.append(Violation(rule="cross_tenant_leak", severity="critical", detail=f"mentions another client: '{term}'"))
    if _EMAIL.search(text):
        v.append(Violation(rule="pii", severity="critical", detail="contains an email address"))
    if _PHONE.search(text):
        v.append(Violation(rule="pii", severity="critical", detail="contains a phone number"))
    for topic in p.banned_topics:
        if _contains(text, topic):
            v.append(Violation(rule="banned_topic", severity="high", detail=f"touches banned topic '{topic}'"))
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        for comp in p.competitors:
            if _contains(sentence, comp) and any(_contains(sentence, w) for w in NEGATIVE_WORDS):
                v.append(Violation(rule="competitor_disparagement", severity="high",
                                   detail=f"negative claim about competitor '{comp}'"))
    # Every statistic must appear in a source we can show the reviewer.
    corpus = " ".join(p.source_texts)
    corpus_nums = {_norm_num(m.group(0)) for m in _STAT.finditer(corpus)} | set(re.findall(r"\d[\d,.]*", corpus))
    corpus_nums = {_norm_num(x) for x in corpus_nums}
    for m in _STAT.finditer(text):
        num = _norm_num(m.group(0))
        if num and num not in corpus_nums:
            v.append(Violation(rule="uncited_stat", severity="high",
                               detail=f"'{m.group(0).strip()}' does not appear in any source"))
    return v


def _split(violations: list[Violation]) -> GuardReport:
    blocking = [x for x in violations if x.severity in BLOCKING]
    warn = [x for x in violations if x.severity not in BLOCKING]
    return GuardReport(passed=not blocking, violations=blocking, warnings=warn)


def check_post(text: str, hashtags: list[str], p: PolicyContext) -> GuardReport:
    v = _common(text, p)
    if not 300 <= len(text) <= 3000:
        v.append(Violation(rule="length", severity="medium", detail=f"{len(text)} chars (LinkedIn sweet spot 300-3000)"))
    if len(hashtags) > 5:
        v.append(Violation(rule="hashtags", severity="low", detail=f"{len(hashtags)} hashtags; keep to 5 or fewer"))
    for phrase in GLOBAL_BANNED_PHRASES + p.banned_phrases:
        if phrase.lower() in text.lower():
            v.append(Violation(rule="banned_phrase", severity="medium", detail=f"uses '{phrase}'"))
    for prev in p.previous_posts:
        if (sim := jaccard(text, prev)) >= 0.5:
            v.append(Violation(rule="near_duplicate", severity="high", detail=f"{sim:.0%} overlap with an earlier post"))
            break
    d = p.required_disclaimer
    if d and any(_contains(text, t) for t in d.get("triggers", [])) and d["text"].lower() not in text.lower():
        v.append(Violation(rule="disclaimer_missing", severity="high", detail=f"add: \"{d['text']}\""))
    return _split(v)


def check_outreach(text: str, lead_company: str, lead_signal: str, p: PolicyContext) -> GuardReport:
    v = _common(text, p)
    if not 150 <= len(text) <= 900:
        v.append(Violation(rule="length", severity="medium", detail=f"{len(text)} chars (aim for 150-900)"))
    low = text.lower()
    for s in SPAM_SIGNALS:
        if s in low:
            v.append(Violation(rule="spam_signal", severity="high", detail=f"spammy phrase '{s}'"))
    if len(re.findall(r"\b[A-Z]{4,}\b", text)) > 2:
        v.append(Violation(rule="spam_signal", severity="medium", detail="shouting in capitals"))
    for s in FALSE_FAMILIARITY:
        if s in low:
            v.append(Violation(rule="false_familiarity", severity="high", detail=f"implies prior contact: '{s}'"))
    if lead_company and lead_company.lower() not in low and not any(
            w in low for w in re.findall(r"\w{5,}", (lead_signal or "").lower())[:6]):
        v.append(Violation(rule="not_personalised", severity="medium", detail="doesn't reference the company or its signal"))
    if text.count("?") > 2:
        v.append(Violation(rule="multiple_ctas", severity="low", detail="more than two questions; keep one clear ask"))
    return _split(v)
