"""Blind review kit: can people tell the system's posts from the real person, and do they read like a template?

  python scripts/export_blind_review.py --db run1.db --db run2.db ... --out review/      # writes review.html,
                                                                                        # ratings.csv, key.json

For each person: two of their own excerpts as a reference ("this is how they write"), then a shuffled, unlabelled
set: their two held-out excerpts (never shown to the system), up to three of the system's posts for their profile,
and one generic-template post. Raters fill ratings.csv: sounds_like (1-5), would_post (yes/no), template (yes/no).
key.json says which item is which; keep it away from raters. Then: python scripts/import_ratings.py.
"""
import argparse
import csv
import html
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build(dbs: list[str], seed: int, per_person_posts: int = 3) -> tuple[list[dict], dict]:
    from evals.voice import load_people, posts_from_db, template_baseline
    people, base = load_people(), template_baseline()
    generated: dict[str, list[str]] = {}
    for db in dbs:
        pid, posts, _ = posts_from_db(db)
        if pid:
            generated.setdefault(pid, []).extend(posts)
    rng = random.Random(seed)
    sections, key = [], {}
    for n, (pid, p) in enumerate(sorted(people.items()), 1):
        items = [("real_heldout", t) for t in p["held_out"]]
        items += [("system", t) for t in rng.sample(generated.get(pid, []), min(per_person_posts, len(generated.get(pid, []))))]
        items.append(("template_baseline", base[pid]))
        rng.shuffle(items)
        rows = []
        for i, (group, text) in enumerate(items, 1):
            item_id = f"W{n}-{chr(64 + i)}"
            key[item_id] = {"person": pid, "group": group}
            rows.append({"id": item_id, "text": text})
        sections.append({"writer": f"Writer {n}", "person": pid, "reference": p["train"][:2], "items": rows})
    return sections, key


def write(sections: list[dict], key: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "key.json").write_text(json.dumps(key, indent=1))
    with (out / "ratings.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rater", "item", "sounds_like_1_to_5", "would_post_yes_no", "reads_like_template_yes_no"])
        for s in sections:
            for it in s["items"]:
                w.writerow(["", it["id"], "", "", ""])
    esc = lambda t: html.escape(t).replace("\n", "<br>")                    # noqa: E731
    parts = ["<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
             "<title>Blind voice review</title><style>body{font:16px/1.55 system-ui;max-width:760px;margin:24px auto;"
             "padding:0 16px}.ref{background:#f3f4f6;padding:12px;border-radius:8px;margin:8px 0}.item{border:1px solid #ddd;"
             "padding:12px;border-radius:8px;margin:12px 0}h2{margin-top:40px}</style>",
             "<h1>Blind voice review</h1><p>For each writer, read the two reference excerpts first. Then rate every item "
             "below it in <b>ratings.csv</b>: does it sound like this writer (1 = not at all, 5 = exactly)? Would they "
             "post it (yes/no)? Does it read like a template (yes/no)? Items are shuffled and unlabelled: some are the "
             "writer's own words, some are not.</p>"]
    for s in sections:
        parts.append(f"<h2>{s['writer']}</h2><p><b>How this writer writes (reference):</b></p>")
        parts += [f"<div class='ref'>{esc(r)}</div>" for r in s["reference"]]
        parts += [f"<div class='item'><b>{it['id']}</b><p>{esc(it['text'])}</p></div>" for it in s["items"]]
    (out / "review.html").write_text("\n".join(parts))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", default=[], help="a live run's SQLite database (repeatable)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    secs, k = build(a.db, a.seed)
    write(secs, k, Path(a.out))
    print(f"wrote {len(k)} items for {len(secs)} writers to {a.out} (review.html, ratings.csv, key.json)")
