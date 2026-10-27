"""Deterministic offline responders for every agent (LLM_MODE=mock).

They build schema-valid outputs from the real inputs passed in request
metadata, and tool-using mocks really call the tools (search, fetch) and cite
what came back. A few deliberately imperfect drafts are planted so the critic
loop and the guardrail auto-fix path are exercised in tests and the stress test.
"""
from __future__ import annotations

import json
import re
import zlib
from collections import Counter
from typing import Any

from app.agents.schemas import FORMATS
from app.guardrails.output_guard import _CLAIM_NUM, _norm_num
from app.llm.providers import register_mock
from app.llm.types import LLMRequest

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
GENERIC_ROLE_WORDS = {"head", "director", "directors", "manager", "managers", "officer", "chief", "vice", "president",
                      "lead", "leads", "senior", "of", "and", "the", "at"}


def h(s: str) -> int:
    return zlib.crc32(s.encode())


def _tool_payloads(req: LLMRequest, tool: str) -> list[Any]:
    out = []
    for m in req.messages:
        if m.get("role") != "tool":
            continue
        mt = re.search(rf'<untrusted_data source="tool:{tool}">\n(.*)\n</untrusted_data>', m.get("content") or "", re.S)
        if mt:
            try:
                out.append(json.loads(mt.group(1)))
            except json.JSONDecodeError:
                pass
    return out


def _calls(*calls: tuple[str, dict]) -> dict:
    return {"tool_calls": [{"name": n, "arguments": a} for n, a in calls]}


def _submit(obj: dict) -> dict:
    return _calls(("submit_result", obj))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


# ---------------------------------------------------------------- research ----
@register_mock("voice_analyst")
def _voice(req: LLMRequest) -> dict:
    md = req.metadata
    intake, samples = md.get("intake", {}), md.get("samples", [])
    words = Counter(w for s in samples for w in re.findall(r"[a-z]{6,}", s.lower()))
    notes = (intake.get("tone") or {}).get("notes", "Clear and direct.")
    tone = [t.strip().lower() for t in re.split(r"[,.]", notes) if t.strip()][:4] or ["direct", "practical"]
    if len(tone) < 2:
        tone.append("practical")
    return {"content": {
        "summary": f"{intake.get('founder_name', 'The founder')} writes short, concrete posts built on one idea. "
                   f"{notes}",
        "tone": tone, "sentence_style": "Short declarative sentences, one idea per paragraph, frequent line breaks.",
        "formatting_rules": ["Hook in the first line", "Paragraphs of 1-2 sentences", "No emojis", "At most 3 hashtags"],
        "vocabulary_use": [w for w, _ in words.most_common(8)] or ["practical"],
        "vocabulary_avoid": ["synergy", "game-changer", "revolutionize", "leverage"],
        "signature_themes": (intake.get("topics_to_own") or ["lessons from building the company"])[:4],
        "example_hooks": [s.split("\n")[0][:120] for s in samples[:3]] or ["Here's what we learned."],
        "learned_rules": md.get("learned_rules", []),
    }}


def _cut(text: str, n: int) -> str:
    """Shorten at a word boundary, so a figure is never cut in half."""
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0]


def _research_step(req: LLMRequest, queries: list[str], reads: int) -> dict:
    """The research loop's decision step: search first (and read client-given sites), then read the top results."""
    md = req.metadata
    if md["step"] == 1:
        return {"content": {"queries": queries, "read_urls": md.get("seeds", [])[:4], "done": False}}
    unread = [u for u in md.get("results", []) if u not in md.get("pages", [])]
    if md["step"] == 2 and unread:
        return {"content": {"queries": [], "read_urls": unread[:reads], "done": True}}
    return {"content": {"queries": [], "read_urls": [], "done": True}}


def _intake(req: LLMRequest) -> dict:
    return req.metadata.get("intake", {})


def _industry(req: LLMRequest) -> str:
    """The brief starts 'Client: Name (industry).'"""
    m = re.match(r".*? \((.*)\)\.?$", _brief_field(req, "Client"))
    return m.group(1) if m else "industry"


@register_mock("market_analyst_step")
def _market_step(req: LLMRequest) -> dict:
    ind = _industry(req)
    return _research_step(req, [f"{ind} survey", f"{ind} benchmark"], 3)


@register_mock("competitor_analyst_step")
def _competitor_step(req: LLMRequest) -> dict:
    names = _brief_field(req, "Known competitors").split(", ")
    names = [n for n in names if n and not n.startswith("(")][:4] or ["Main competitor"]
    return _research_step(req, names, len(names) + 1)


@register_mock("trend_scout_step")
def _trend_step(req: LLMRequest) -> dict:
    ind = _industry(req)
    return _research_step(req, [f"{ind} trends", f"linkedin content {ind.split(':')[0]} trends"], 3)


def _brief_field(req: LLMRequest, label: str) -> str:
    m = re.search(rf"^{label}: (.*)$", req.messages[-1]["content"], re.M)
    return m.group(1).strip() if m else ""


@register_mock("market_analyst")
def _market(req: LLMRequest) -> dict:
    intake, pages = _intake(req), req.metadata.get("pages", [])
    ind = intake.get("industry", "the industry")
    facts = [{"claim": s, "source_url": p["url"]} for p in pages for s in _sentences(p["text"]) if re.search(r"\d", s)][:6]
    pains = [s for p in pages for s in _sentences(p["text"]) if not re.search(r"\d", s)][:4] or \
        [f"Buyers in {ind} struggle to get reliable answers fast."]
    segs = [x.strip() for x in re.split(r",| and ", intake.get("target_customers", "core buyers"))][:4]
    return {"content": {"industry": ind, "summary": f"{ind}: buyers want faster, more reliable outcomes with less manual work.",
                        "buyer_segments": [x for x in segs if x] or ["core buyers"], "buyer_pains": pains, "key_facts": facts}}


@register_mock("competitor_analyst")
def _competitors(req: LLMRequest) -> dict:
    intake, pages = _intake(req), req.metadata.get("pages", [])
    names = intake.get("known_competitors", [])[:4] or ["Main competitor"]
    comps = []
    for n in names:
        page = next((p for p in pages if n.lower().replace(" ", "") in (p.get("title", "") + p["url"]).lower().replace(" ", "")), None)
        page = page or (pages[0] if pages else None)   # mock only: attribute to the closest page it read
        sents = _sentences(page["text"]) if page else []
        focus = next((s for s in sents if "content focus" in s.lower()), "Content focus: not enough public data.")
        cadence = next((s for s in sents if re.search(r"post|publish", s, re.I)), "Unknown cadence")
        comps.append({"name": n, "positioning": sents[0] if sents else f"{n} competes for the same buyers.",
                      "offerings": [_cut(sents[0], 80)] if sents else [], "content_themes": [focus.split(":", 1)[-1].strip()[:100]],
                      "posting_cadence": cadence[:120], "strengths": [_cut(sents[1], 100)] if len(sents) > 1 else [],
                      "weaknesses": ["Little founder-led content"], "source_urls": [page["url"]] if page else []})
    return {"content": {"competitors": comps,
                        "whitespace": ["Founder-led stories with real operating numbers",
                                       f"Practical playbooks for {intake.get('target_customers', 'buyers')[:60]}",
                                       "Honest takes on what technology should and shouldn't do"]}}


@register_mock("trend_scout")
def _trends(req: LLMRequest) -> dict:
    intake, pages = _intake(req), req.metadata.get("pages", [])
    ind = intake.get("industry", "industry")
    trends = []
    for p in pages:
        parts = [x.strip() for x in re.split(r"Trend \d+:|\d\.\s", p["text"]) if len(x.strip()) > 25]
        for part in parts[:3]:
            name = " ".join(part.split()[:6]).rstrip(".,")
            trends.append({"name": name, "summary": _cut(part, 240), "evidence_url": p["url"],
                           "relevance": 3 + h(name) % 3, "content_angle": f"What '{name.lower()}' means for our buyers"})
    return {"content": {"trends": trends[:6] or [{"name": f"Change in {ind}", "summary": "Buyers expect more for less.",
                                                  "evidence_url": "", "relevance": 3,
                                                  "content_angle": "What we see with customers"}]}}


# ---------------------------------------------------------------- strategy ----
def _shares(n: int) -> list[int]:
    return {3: [40, 30, 30], 4: [30, 25, 25, 20], 5: [25, 20, 20, 20, 15]}[n]


@register_mock("strategist")
def _strategist(req: LLMRequest) -> dict:
    md = req.metadata
    intake = md.get("intake", {})
    topics = (intake.get("topics_to_own") or ["Industry insight", "Practical how-to", "Founder lessons"])[:4]
    trends = [t["name"] for t in (md.get("trend_report") or {}).get("trends", [])]
    white = (md.get("competitor_matrix") or {}).get("whitespace", [])
    shares = _shares(len(topics))
    pillars = [{"name": t[:1].upper() + t[1:], "description": f"Posts that make {intake.get('company_name')} the go-to voice on {t}.",
                "share_pct": s, "example_angles": [f"Where teams go wrong on {t}", f"How we think about {t}",
                                                   *(f"{t} and {tr.lower()}" for tr in trends[i:i + 1])]}
               for i, (t, s) in enumerate(zip(topics, shares))]
    return {"content": {"positioning": f"{intake.get('company_name')}: the practical, founder-led voice. "
                                       f"Owns: {white[0] if white else 'clear operating advice'}.",
                        "pillars": pillars, "format_mix": {"story": 30, "how_to": 25, "opinion": 20, "data_insight": 15,
                                                           "carousel_outline": 10},
                        "posts_per_week": 5, "goals": [intake.get("goals") or "Build authority and pipeline"]}}


TITLE_EXPANSION = {
    "finance": ["CFO", "Controller", "Finance Director", "Head of Finance", "Founder", "CEO", "Owner", "COO"],
    "nurs": ["Chief Nursing Officer", "Director of Nursing", "VP of Operations", "VP Patient Care Services",
             "HR Director", "Nurse Manager"],
}


@register_mock("icp_builder")
def _icp(req: LLMRequest) -> dict:
    intake = req.metadata.get("intake", {})
    tc = intake.get("target_customers", "")
    left, _, right = tc.partition(" at ")
    titles = [t.strip().rstrip("s") for t in re.split(r",| and ", left) if t.strip()]
    for key, extra in TITLE_EXPANSION.items():
        if key in tc.lower():
            titles = list(dict.fromkeys(titles + extra))
    size = re.search(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s*employee", right)
    lo, hi = (int(size.group(1).replace(",", "")), int(size.group(2).replace(",", ""))) if size else (1, 100000)
    ind_text = re.sub(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s*employee|in the (US|UK)( and (US|UK))?|\.$", "", right)
    industries = [i.strip() for i in re.split(r",| and ", ind_text) if len(i.strip()) > 3] or ["General"]
    if "nurs" in tc.lower():
        industries = list(dict.fromkeys(industries + ["Hospital", "Health System", "Medical Center"]))
    geos = [g for g in ("US", "UK") if g in tc] or ["US"]
    return {"content": {"segments": [{"name": "Core buyers", "titles": titles[:10] or ["Founder"], "industries": industries[:8],
                                      "min_employees": lo, "max_employees": hi, "geos": geos,
                                      "pains": ["Slow, manual work", "Low visibility"],
                                      "buying_triggers": ["hiring", "expansion", "new leadership", "funding"]}],
                        "exclusions": intake.get("known_competitors", []),
                        "scoring_notes": "Decision-makers in target industries and size band with a recent trigger."}}


TWISTS = ["the mistake we see most", "a 3-step playbook", "what changed in 2026", "a customer story", "the myth",
          "what to measure", "the first 30 days", "a hard lesson", "the checklist", "the question to ask",
          "the hidden cost", "what good looks like", "a contrarian take", "how to start this week"]


def _pillar_sequence(pillars: dict[str, int], n: int) -> list[str]:
    """Smooth weighted round-robin: deterministic pillar order matching target shares."""
    current = {p: 0 for p in pillars}
    seq = []
    for _ in range(n):
        for p, w in pillars.items():
            current[p] += w
        best = max(current, key=lambda p: current[p])
        current[best] -= sum(pillars.values())
        seq.append(best)
    return seq


@register_mock("calendar_planner")
def _calendar(req: LLMRequest) -> dict:
    md = req.metadata
    weeks, pillars, per_week = md["weeks"], md["pillars"], md["per_week"]
    strategy = md.get("strategy") or {}
    angles = {p["name"]: p["example_angles"] for p in strategy.get("pillars", [])}
    trends = [t["name"] for t in (md.get("trends") or {}).get("trends", [])]
    seq = _pillar_sequence(pillars, max(weeks) * per_week)
    counts: Counter = Counter()
    slots = []
    for g, pillar in enumerate(seq):
        week, i = g // per_week + 1, g % per_week
        k = counts[pillar]
        counts[pillar] += 1
        if week not in weeks:
            continue
        a = angles.get(pillar) or [pillar]
        angle, twist = a[k % len(a)], TWISTS[(k // len(a)) % len(TWISTS)]
        angle = angle[0].upper() + angle[1:]
        slots.append({"week": week, "day": DAYS[i % 5] if i < 5 else ["Sat", "Sun"][i % 2], "pillar": pillar,
                      "format": FORMATS[(g + h(pillar)) % 5], "topic": f"{angle}: {twist}",
                      "angle": f"Lead with {twist}, told from the founder's own experience.",
                      "trend_ref": trends[g % len(trends)] if trends and g % 3 == 0 else ""})
    return {"content": {"slots": slots}}


# ----------------------------------------------------------------- content ----
OPENERS = ["Here's something I keep seeing.", "A quick story from this week.", "Unpopular opinion, maybe.",
           "I got this wrong for years.", "Most advice on this is too vague to use.", "Let me be specific.",
           "A customer asked me this on Tuesday.", "Three years ago I'd have disagreed with this.",
           "This one is for the operators.", "Short post, practical point."]
CLOSERS = ["What's the version of this you've seen?", "Curious how others handle it.",
           "If this is you, start small this week.", "Tell me where I'm wrong.", "Worth trying for one month.",
           "Steal this if it helps.", "That's the whole trick.", "More on this next week."]
MIDDLES = ["The fix wasn't clever. It was consistent.", "Nobody planned for it. Everyone paid for it.",
           "Small habit, big compounding effect.", "Simple to explain, hard to keep doing.",
           "It only works when someone owns it.", "The tools were never the bottleneck.",
           "Once it was visible, people fixed it themselves.", "Most of the work happened before anyone noticed."]
BRIDGES = ["The teams that get this right treat {p} as a weekly routine, not a quarterly scramble.",
           "When {p} has an owner and a rhythm, the drama mostly disappears.",
           "I've watched {p} go from chaotic to boring in about a month. Boring is the goal.",
           "Ask who owns {p} on your team. If the answer takes more than a second, start there.",
           "The best version of {p} I've seen fits on one page and gets checked every Monday."]
STEPS = ["Concretely: pick one owner, one metric and one weekly check-in.",
         "Start with the smallest version you can run this Friday.",
         "Write the rule down where everyone can see it. Then keep it for four weeks.",
         "Measure it once before you change anything, so you know what moved."]


def _compose(slot: dict, key: int, fact: str, revised: bool) -> str:
    pick = lambda pool, salt: pool[(key // salt) % len(pool)]
    p = slot["pillar"].lower()
    lines = [pick(OPENERS, 1), "", f"{slot['topic']}.", ""]
    if fact:
        lines += [f"One number worth sitting with: {fact}", ""]
    lines += [pick(MIDDLES, 7), pick(BRIDGES, 13).format(p=p), ""]
    if revised:
        lines += [pick(STEPS, 17), ""]
    lines.append(pick(CLOSERS, 3))
    return "\n".join(lines)


@register_mock("writer")
def _writer(req: LLMRequest) -> dict:
    md = req.metadata
    slot, rnd = md["slot"], md.get("round", 0)
    key = h(slot["id"])
    facts = [(s, src["url"]) for src in md.get("sources", []) for s in _sentences(src["text"])
             if re.search(r"\d+%|\$\d|\b\d{2,}\b", s) and not re.match(r"(Trend \d|\d\.)", s)]
    fact, url = facts[key % len(facts)] if facts else ("", "")
    violations = md.get("violations", [])
    if any(v["rule"] == "near_duplicate" for v in violations):
        key, fact = h(slot["id"] + ":rewrite"), ""  # rewrite from different building blocks, drop the shared fact
    text = _compose(slot, key, fact, revised=rnd >= 1)
    if rnd == 0 and key % 11 == 0:  # planted flaw: an unsourced statistic for the guardrail to catch
        text += "\n\nRoughly 83% of teams we talk to admit they skip this."
    for v in violations:  # policy-fix round
        if v["rule"] in ("uncited_stat", "unclaimed_stat", "metric_misattributed", "claim_quote_not_in_source"):
            text = "\n".join(l for l in text.split("\n") if v["detail"].split("'")[1] not in l)
        if v["rule"] == "disclaimer_missing":
            text += "\n\n" + v["detail"].split('"')[1]
    claims, seen = [], set()
    if fact and fact in text:           # every figure the post uses is declared with a verbatim quote
        for m in _CLAIM_NUM.finditer(fact):
            num = _norm_num(m.group(0))
            if num and num not in seen and not (len(num) == 4 and 1900 <= int(num.split(".")[0]) <= 2100):
                seen.add(num)
                claims.append({"number": m.group(0).strip(), "statement": fact, "source_url": url, "source_quote": fact})
    return {"content": {"text": text, "hashtags": ["#" + re.sub(r"\W", "", slot["pillar"].title())[:20]],
                        "sources_used": [url] if fact else [], "claims": claims,
                        "rationale": f"Advances the '{slot['pillar']}' pillar with a {slot['format']} post."}}


@register_mock("critic")
def _critic(req: LLMRequest) -> dict:
    md = req.metadata
    key, rnd = h(md["slot"]["id"]), md.get("round", 1)
    weak = key % 3 == 0 and rnd == 1  # a third of first drafts need a revision
    s = (4, 3, 3, 3, 4) if weak else (4, 4, 4, 4, 4)
    return {"content": {"voice_match": s[0], "specificity": s[1], "hook": s[2], "insight": s[3], "cta_fit": s[4],
                        "must_fix": ["Add one concrete, actionable step", "Make the hook more specific"] if weak else [],
                        "verdict": "Needs a sharper, more concrete middle." if weak else "Ready for the founder."}}


@register_mock("voice_reflector")
def _reflector(req: LLMRequest) -> dict:
    rules = []
    for f in req.metadata.get("feedback", []):
        if f.get("note"):
            rules.append(f"Reviewer guidance: {f['note'][:120]}")
        o, r = f.get("original", ""), f.get("revised", "")
        if r and len(r) < 0.8 * len(o):
            rules.append("Keep posts tighter: cut setup sentences and get to the point faster.")
        if r and o.rstrip().endswith("?") and not r.rstrip().endswith("?"):
            rules.append("Don't end every post with a question.")
    return {"content": {"rules": list(dict.fromkeys(rules))[:8]}}


# ------------------------------------------------------------------- leads ----
@register_mock("prospector")
def _prospector(req: LLMRequest) -> dict:
    md = req.metadata
    if _tool_payloads(req, "search_prospects"):
        return _submit({"searches": [f"segment search, round {md.get('round')}"], "notes": "pool built"})
    calls = []
    for seg in md["icp"]["segments"]:
        broaden = md.get("round", 1) > 1
        calls.append(("search_prospects", {"titles": seg["titles"], "industries": seg["industries"],
                                           "min_employees": seg["min_employees"], "max_employees": seg["max_employees"],
                                           "geos": [] if broaden else seg["geos"], "limit": 50 if broaden else 40}))
    return _calls(*calls)


def _words(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]{2,}", s.lower()):
        w = w[:-3] + "y" if w.endswith("ies") else w[:-1] if w.endswith("s") and len(w) > 3 else w
        out.add(w)
    return out


@register_mock("qualifier")
def _qualifier(req: LLMRequest) -> dict:
    md = req.metadata
    segs = md["icp"]["segments"]
    title_words = set().union(*(_words(t) for s in segs for t in s["titles"])) - GENERIC_ROLE_WORDS
    ind_words = {w for s in segs for i in s["industries"] for w in _words(i) if len(w) >= 4}
    lo, hi = min(s["min_employees"] for s in segs), max(s["max_employees"] for s in segs)
    scores = []
    for p in md["batch"]:
        t_hit = bool(_words(p["title"]) & title_words)
        i_hit = bool(_words(p["industry"]) & ind_words)
        size_ok = lo <= p["employees"] <= hi
        score = 5 + 45 * t_hit + 30 * i_hit + 10 * size_ok + 10 * bool(p.get("signal"))
        ev = [x for x, ok in ((f"title '{p['title']}' matches ICP", t_hit), (f"industry '{p['industry']}'", i_hit),
                              (f"{p['employees']} employees in range", size_ok)) if ok]
        dq = [x for x, bad in (("role outside ICP", not t_hit), ("industry outside ICP", not i_hit),
                               ("company size out of range", not size_ok)) if bad]
        scores.append({"prospect_id": p["id"], "fit_score": min(score, 95), "segment": segs[0]["name"],
                       "evidence": ev[:4], "disqualifiers": dq[:4]})
    return {"content": {"scores": scores}}


@register_mock("outreach_writer")
def _outreach(req: LLMRequest) -> dict:
    md = req.metadata
    lead, intake = md["lead"], md["intake"]
    first = lead["name"].split()[0].title()
    what = intake.get("company", "").split(",")[0].rstrip(".") or f"{intake.get('company_name')} helps teams like yours"
    opener = f"I noticed {lead['company']} {lead['signal']}." if lead.get("signal") else \
        f"I've been following how teams like {lead['company']} approach this."
    msg = (f"Hi {first}, {opener} In my experience that's usually when the pressure on the {lead['title']} role goes up. "
           f"{what}. "
           f"Would it be useful if I shared how similar teams handled this stage?")
    alt = (f"Hi {first}, quick one: {opener} We work with teams at a similar stage on exactly this. "
           f"Open to a short exchange of notes?")
    return {"content": {"subject": f"Quick question about {lead['company']}", "message": msg, "alt_message": alt,
                        "personalization_hook": lead.get("signal") or lead["company"]}}


@register_mock("outreach_critic")
def _outreach_critic(req: LLMRequest) -> dict:
    sig = bool(req.metadata["lead"].get("signal"))
    return {"content": {"personalization": 4 if sig else 3, "relevance": 4, "clarity": 4,
                        "must_fix": [] if sig else ["Find a more specific hook"]}}
