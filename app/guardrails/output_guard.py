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
# Any figure a reader could take as a factual claim: percentages, money, multipliers, thousands separators,
# or a bare count above 10. Years and small list numbers ("3 lessons") are not claims.
_CLAIM_NUM = re.compile(
    r"(?<![\w.,])(?:[$£€]\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|bn|million|billion)?"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s?(?:%|percent|x\b|times\b|k\b|million|billion|bn\b)"
    r"|\d{2,}(?:\.\d+)?)(?![\w,]*\d)", re.I)
_STOP = set("a an the and or but of in on at to for from by with as is are was were be been being it its this that these those "
            "we our you your i my me they their them he she his her not no so if than then too very just also more most "
            "over into about up out how what when why who which can could would should will do does did has have had".split())
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


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _window_words(text: str, start: int, end: int, span: int = 3) -> set[str]:
    """Content words within `span` words either side of a figure, stemmed. This is what the figure is *about*."""
    before = re.findall(r"[a-z]+", text[:start].lower())[-span:]
    after = re.findall(r"[a-z]+", text[end:].lower())[:span]
    return {_stem(w) for w in before + after if w not in _STOP and len(w) > 1}


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


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
    source_docs: dict[str, str] = field(default_factory=dict)    # url -> full text, for claim-level checks
    founder_samples: list[str] = field(default_factory=list)     # the founder's own posts: never to be copied
    example_hooks: list[str] = field(default_factory=list)


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


def check_claims(text: str, claims: list, p: PolicyContext) -> list[Violation]:
    """Claim-level grounding. Every figure in the post needs a claim with a verbatim quote from a known source,
    and the figure must be used in the same sense as the quote (what it counts, not just that the number exists)."""
    v: list[Violation] = []
    by_num = {_norm_num(c.number): c for c in claims if _norm_num(c.number)}
    flat_docs = {url: _flat(t) for url, t in p.source_docs.items()}
    for m in _CLAIM_NUM.finditer(text):
        raw = m.group(0).strip()
        num = _norm_num(raw)
        if not num or (len(num) == 4 and 1900 <= int(num.split(".")[0]) <= 2100 and not re.search(r"[%$£€]", raw)):
            continue                                         # a year
        claim = by_num.get(num)
        if claim is None:
            v.append(Violation(rule="unclaimed_stat", severity="high",
                               detail=f"'{raw}' is used without a source quote; add it to claims or remove it"))
            continue
        quote = _flat(claim.source_quote)
        doc = flat_docs.get(claim.source_url)
        haystacks = [doc] if doc is not None else list(flat_docs.values())
        if doc is None and claim.source_url not in flat_docs:
            v.append(Violation(rule="claim_unknown_source", severity="high",
                               detail=f"'{raw}' cites {claim.source_url or 'no source'}, which is not one of the provided sources"))
            continue
        if not quote or not any(quote in h for h in haystacks):
            v.append(Violation(rule="claim_quote_not_in_source", severity="high",
                               detail=f"the quote given for '{raw}' does not appear verbatim in {claim.source_url}"))
            continue
        qm = next((x for x in _CLAIM_NUM.finditer(quote) if _norm_num(x.group(0)) == num), None) or \
            next((x for x in re.finditer(re.escape(raw.lower()), quote)), None)
        if qm is None:
            v.append(Violation(rule="claim_quote_missing_number", severity="high",
                               detail=f"the quote given for '{raw}' does not contain that figure"))
            continue
        used, source = _window_words(text, m.start(), m.end()), _window_words(quote, qm.start(), qm.end(), span=5)
        if used and len(used & source) / len(used) < 0.5:
            v.append(Violation(rule="metric_misattributed", severity="high",
                               detail=f"'{raw}' is used for \"{' '.join(sorted(used))}\" but the source says it measures "
                                      f"\"{' '.join(sorted(source))}\". Use the figure only for what the source says"))
    return v


def check_copying(text: str, p: PolicyContext) -> list[Violation]:
    """The founder's own sample posts and example hooks are style references, not material to reuse."""
    from difflib import SequenceMatcher
    v: list[Violation] = []
    first = _flat(re.split(r"(?<=[.!?\n])\s", text.strip(), maxsplit=1)[0])
    refs = [(re.split(r"(?<=[.!?\n])\s", s.strip(), maxsplit=1)[0], s) for s in p.founder_samples] + [(h, h) for h in p.example_hooks]
    for hook, full in refs:
        h = _flat(hook)
        if len(h) > 15 and (SequenceMatcher(None, first, h).ratio() >= 0.75 or h in _flat(text)):
            v.append(Violation(rule="copied_founder_hook", severity="high",
                               detail="opens with a line taken from the founder's own sample posts; write a new hook"))
            break
    for full in p.founder_samples:
        if jaccard(text, full) >= 0.3:
            v.append(Violation(rule="copies_founder_sample", severity="high",
                               detail="overlaps heavily with one of the founder's existing posts"))
            break
    return v


def check_post(text: str, hashtags: list[str], p: PolicyContext, claims: list | None = None) -> GuardReport:
    """`claims` is the writer's claim list for model drafts. None means a human edit: no claim bookkeeping is demanded
    of the founder, but figures still have to exist in a source."""
    v = _common(text, p)
    if claims is not None:
        v += check_claims(text, claims, p)
    v += check_copying(text, p)
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
