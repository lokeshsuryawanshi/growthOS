"""Collect short verbatim excerpts of public writing for the voice eval (evals/datasets/voice_samples).

  python -I scripts/collect_voice_samples.py <person> ...      # writes raw_samples.json in the current directory

Feeds only (plus article pages where a feed has summaries), robots.txt checked before every fetch, never linkedin.com,
paragraphs copied as written (120-220 words from the start of a post). Announcement and puzzle posts are skipped.
"""
import json, re, sys, html, urllib.robotparser
from datetime import date
from urllib.parse import urlparse
import httpx
from xml.etree import ElementTree as ET

UA = "GrowthOS-eval/1.0 (style test samples; github.com/lokeshsuryawanshi/growthOS)"
PEOPLE = {
    "seth_godin": {"feeds": ["https://seths.blog/feed/", "https://seths.blog/feed/?paged=2"], "author": None},
    "jason_cohen": {"feeds": ["https://longform.asmartbear.com/index.xml"], "author": None},
    "april_dunford": {"feeds": ["https://aprildunford.substack.com/feed"], "author": None},
    "paras_chopra": {"feeds": ["https://invertedpassion.com/feed/", "https://invertedpassion.com/feed/?paged=2"], "author": None},
    "arvid_kahl": {"feeds": ["https://thebootstrappedfounder.com/rss/"], "author": None},
    "bunnie_huang": {"feeds": [f"https://www.bunniestudios.com/blog/?feed=rss2&paged={n}" for n in range(1, 21)], "author": None},
}
SKIP = re.compile(r"(?i)name that ware|announcement|available|guest post|^winner|^solution|subscribers only")
NS = {"content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/", "a": "http://www.w3.org/2005/Atom"}


def text_of(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|figure|pre|code|table|blockquote)\b.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def excerpt(t: str, lo=120, hi=220) -> str:
    """Whole paragraphs from the start of the post, between lo and hi words; nothing rewritten."""
    out, n = [], 0
    for para in t.split("\n"):
        w = len(para.split())
        if w < 4 and not out:
            continue                      # skip bylines and captions at the top
        if n + w > hi and n >= lo:
            break
        if n + w > hi:                    # a single long paragraph: cut at the last sentence that fits
            sents = re.split(r"(?<=[.!?])\s+", para)
            for s in sents:
                if n + len(s.split()) > hi:
                    break
                out.append(s); n += len(s.split())
            break
        out.append(para); n += w
    return "\n\n".join(out) if n >= lo * 0.8 else ""


def allowed(url):
    u = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    r = httpx.get(f"{u.scheme}://{u.netloc}/robots.txt", headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    return rp.can_fetch(UA, url)


def collect(person: str, lo: int = 120) -> list[dict]:
    cfg = PEOPLE[person]
    items, seen = [], set()
    for f in cfg["feeds"]:
        if not allowed(f):
            print(person, "robots disallows", f); continue
        r = httpx.get(f, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            print(person, "HTTP", r.status_code, f); continue
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            author = (it.findtext("dc:creator", namespaces=NS) or it.findtext("author") or "").lower()
            if cfg["author"] and cfg["author"] not in author:
                continue
            body = it.findtext("content:encoded", namespaces=NS) or it.findtext("description") or ""
            items.append({"title": (it.findtext("title") or "").strip(), "url": link,
                          "published": (it.findtext("pubDate") or "")[:16], "text": text_of(body)})
    samples = []
    for it in items:
        if it["url"] in seen or "linkedin.com" in it["url"] or SKIP.search(it["title"]):
            continue
        seen.add(it["url"])
        ex = excerpt(it["text"], lo=lo)
        if ex:
            samples.append({"title": it["title"], "url": it["url"], "published": it["published"], "excerpt": ex,
                            "words": len(ex.split())})
        if len(samples) >= 8:
            break
    return samples


def from_pages(feed: str, lo: int = 120) -> list[dict]:
    """For feeds that carry only summaries (Jason Cohen's): read each article page, one request every 3 seconds."""
    import time
    r = httpx.get(feed, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    samples = []
    for it in ET.fromstring(r.content).iter("item"):
        title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
        if len(samples) >= 8:
            break
        if not link.startswith("http") or "linkedin.com" in link or SKIP.search(title) or not allowed(link):
            continue
        time.sleep(3)
        page = httpx.get(link, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        if page.status_code != 200:
            continue
        m = re.search(r"(?is)<article\b.*?</article>", page.text) or re.search(r"(?is)<main\b.*?</main>", page.text)
        body = re.sub(r"(?is)<(nav|header|footer|aside|form)\b.*?</\1>", " ", m.group(0) if m else page.text)
        ex = excerpt(text_of(body), lo=lo)
        ex = re.sub(r"^.*?by [A-Z][a-z]+ [A-Z][a-z]+ on [A-Z][a-z]+ \d{1,2}, \d{4}\s*", "", ex, count=1, flags=re.S)  # byline
        if ex:
            samples.append({"title": title, "url": link, "published": (it.findtext("pubDate") or "")[:16], "excerpt": ex,
                            "words": len(ex.split())})
    return samples


if __name__ == "__main__":
    import os
    data = json.load(open("raw_samples.json")) if os.path.exists("raw_samples.json") else {"people": {}}
    data["retrieved"] = date.today().isoformat()
    for person in sys.argv[1:]:
        got = from_pages(PEOPLE[person]["feeds"][0]) if person == "jason_cohen" else collect(person)
        if got:
            data["people"][person] = got
        print(person, "->", len(got), [s["words"] for s in got])
    json.dump(data, open("raw_samples.json", "w"), indent=1, ensure_ascii=False)
