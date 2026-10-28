"""How a founder writes, measured in code from their own samples, and the generic LinkedIn template, detected in code.

The same features serve three purposes:
* the writer gets a short "style card" (length, paragraph shape, how posts open and close, lists, emojis, person),
  measured from the founder's samples, instead of a fixed formula;
* template_markers() flags the generic skeleton (one-line hook, a line per sentence, listicle, "Agree?" ending,
  stock phrases, emojis, hashtag tails) when the founder's own samples do not do it;
* evals/voice.py compares generated posts with held-out samples (voice distance) and with each other (template
  similarity across clients).
"""
from __future__ import annotations

import math
import re
from statistics import mean, median, pstdev

FIRST = re.compile(r"\b(i|i'm|i've|i'd|i'll|me|my|mine)\b", re.I)
WE = re.compile(r"\b(we|we're|we've|us|our|ours)\b", re.I)
YOU = re.compile(r"\b(you|you're|you've|your|yours)\b", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")
LIST_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*•→▪✓✔])\s+")
STOCK = re.compile(r"(?i)here'?s what i (?:learned|learnt)|here'?s the thing|let that sink in|game[- ]changer|"
                   r"unpopular opinion|hot take|the truth is|plot twist|read that again|this is huge|"
                   r"i'?m (?:thrilled|excited|humbled) to|what do you think\??|agree\?|thoughts\?|drop a comment|"
                   r"comment below|follow for more|♻️|repost if")
CLOSING_ASK = re.compile(r"(?i)(agree|thoughts|what do you think|what would you add|how about you|"
                         r"what's your take|have you|do you|would you|what about you)[^.!]*\?\s*$")
FUNCTION_WORDS = ("the of and to a in is that it for you this with on be not are as but have i we they at or what "
                  "if so can do your more all one about just when there about than then my our an by from was will "
                  "most people how no it's don't").split()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if re.search(r"[A-Za-z]", s)]


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:\d+[.)]|[-*•]))", text.strip()) if p.strip()]


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def features(text: str) -> dict[str, float]:
    """Numbers that describe how a text is written, independent of what it is about."""
    words = re.findall(r"[A-Za-z']+", text)
    nw = max(1, len(words))
    sents = sentences(text)
    paras = paragraphs(text)
    lines = [l for l in text.splitlines() if l.strip()]
    sl = [len(re.findall(r"[A-Za-z']+", s)) for s in sents] or [0]
    first_para = paras[0] if paras else ""
    last = sents[-1] if sents else ""
    syl = sum(_syllables(w) for w in words)
    return {
        "words": float(len(words)),
        "sentence_len": mean(sl),
        "sentence_len_sd": pstdev(sl) if len(sl) > 1 else 0.0,
        "sentences_per_paragraph": len(sents) / max(1, len(paras)),
        "one_sentence_paragraphs": sum(1 for p in paras if len(sentences(p)) <= 1) / max(1, len(paras)),
        "list_lines": sum(1 for l in lines if LIST_LINE.match(l)) / max(1, len(lines)),
        "questions": text.count("?") / max(1, len(sents)),
        "exclamations": text.count("!") / max(1, len(sents)),
        "first_person": len(FIRST.findall(text)) / nw * 100,
        "we": len(WE.findall(text)) / nw * 100,
        "you": len(YOU.findall(text)) / nw * 100,
        "dashes": (text.count("—") + text.count(" - ") + text.count("–")) / nw * 100,
        "colons": text.count(":") / nw * 100,
        "parens": text.count("(") / nw * 100,
        "emojis": len(EMOJI.findall(text)) / nw * 100,
        "hashtags": len(re.findall(r"(?<!\w)#\w+", text)) / nw * 100,
        "word_len": sum(len(w) for w in words) / nw,
        "type_token": len({w.lower() for w in words}) / nw,
        "grade": 0.39 * (nw / max(1, len(sents))) + 11.8 * (syl / nw) - 15.59,
        "opening_len": float(len(re.findall(r"[A-Za-z']+", first_para))),
        "opens_with_question": 1.0 if first_para.rstrip().endswith("?") else 0.0,
        "ends_with_question": 1.0 if last.rstrip().endswith("?") else 0.0,
    }


def function_profile(text: str) -> list[float]:
    words = [w.lower() for w in re.findall(r"[A-Za-z']+", text)]
    n = max(1, len(words))
    return [words.count(f) / n for f in FUNCTION_WORDS]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


# ---------------------------------------------------------------- the generic template
def template_markers(text: str) -> set[str]:
    f = features(text)
    paras = paragraphs(text)
    out = set()
    if paras and len(sentences(paras[0])) == 1 and f["opening_len"] <= 14 and len(paras) >= 3:
        out.add("one-line hook")
    if len(paras) >= 5 and f["one_sentence_paragraphs"] >= 0.7:
        out.add("a line per sentence")
    if f["list_lines"] >= 0.2:
        out.add("numbered or bulleted list")
    if CLOSING_ASK.search(text.strip()):
        out.add("engagement question at the end")
    if STOCK.search(text):
        out.add("stock LinkedIn phrase")
    if f["emojis"] > 0:
        out.add("emojis")
    if re.search(r"(?:#\w+\s*){2,}$", text.strip()):
        out.add("hashtag tail")
    return out


def structure_vector(text: str) -> list[float]:
    """The skeleton of a post (not its words): used to see whether posts for different clients share one template."""
    f = features(text)
    m = template_markers(text)
    return [min(f["words"] / 300, 2), min(f["sentences_per_paragraph"] / 4, 2), f["one_sentence_paragraphs"],
            f["list_lines"] * 3, f["ends_with_question"], f["opens_with_question"], min(f["opening_len"] / 20, 2),
            1.0 if "one-line hook" in m else 0.0, 1.0 if "stock LinkedIn phrase" in m else 0.0, min(f["emojis"], 2),
            min(f["sentence_len"] / 20, 2)]


# ---------------------------------------------------------------- the style card the writer gets
def style_card(samples: list[str]) -> dict:
    fs = [features(s) for s in samples if s.strip()]
    if not fs:
        return {}
    allowed = set().union(*(template_markers(s) for s in samples))

    def share(key: str) -> float:
        return sum(1 for f in fs if f[key] > 0) / len(fs)
    return {"samples": len(fs), "words": [int(min(f["words"] for f in fs)), int(max(f["words"] for f in fs))],
            "median_words": int(median(f["words"] for f in fs)),
            "sentence_len": round(mean(f["sentence_len"] for f in fs), 1),
            "sentences_per_paragraph": round(mean(f["sentences_per_paragraph"] for f in fs), 1),
            "one_sentence_paragraphs": round(mean(f["one_sentence_paragraphs"] for f in fs), 2),
            "ends_with_question": round(mean(f["ends_with_question"] for f in fs), 2),
            "opens_with_question": round(mean(f["opens_with_question"] for f in fs), 2),
            "lists": round(share("list_lines"), 2), "emojis": round(share("emojis"), 2),
            "hashtags": round(share("hashtags"), 2),
            "first_person": round(mean(f["first_person"] for f in fs), 1), "we": round(mean(f["we"] for f in fs), 1),
            "you": round(mean(f["you"] for f in fs), 1), "dashes": round(mean(f["dashes"] for f in fs), 2),
            "allowed_patterns": sorted(allowed)}


def describe(card: dict) -> str:
    """The style card in words, for the writer's brief."""
    if not card:
        return "No samples to measure."
    lo, hi = card["words"]

    def often(x: float) -> str:
        return "never" if x == 0 else "rarely" if x < 0.25 else "sometimes" if x < 0.6 else "usually"
    person = "first person singular (I)" if card["first_person"] >= max(card["we"], 1.0) else \
        "first person plural (we)" if card["we"] >= 1.0 else "mostly addresses the reader (you)" if card["you"] >= 2 else "impersonal"
    shape = "a line per sentence" if card["one_sentence_paragraphs"] >= 0.7 else \
        f"paragraphs of about {card['sentences_per_paragraph']:.0f} sentences"
    lines = [f"Length: {lo}-{hi} words (median {card['median_words']}). Write about {card['median_words']} words.",
             f"Shape: {shape}; sentences average {card['sentence_len']:.0f} words.",
             f"Voice: {person}.",
             f"Opens with a question: {often(card['opens_with_question'])}. Ends with a question: {often(card['ends_with_question'])}.",
             f"Lists: {often(card['lists'])}. Emojis: {often(card['emojis'])}. Hashtags: {often(card['hashtags'])}."]
    if card["dashes"] >= 0.5:
        lines.append("Uses dashes for asides.")
    return "\n".join(lines)


def template_problems(text: str, card: dict) -> list[str]:
    """Generic-template patterns in a draft that this founder's own samples never use."""
    allowed = set(card.get("allowed_patterns", [])) if card else set()
    return sorted(template_markers(text) - allowed)
