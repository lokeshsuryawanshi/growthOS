"""Unblind the review: mean ratings per group (the person's real writing, the system, the generic template).

  python scripts/import_ratings.py --key review/key.json --ratings review/ratings-alex.csv [--ratings more.csv]
                                   [--db run.db ...]       # also the operator's in-app ratings stored on posts

The headline is the system's mean against the real writing and the template, per person and overall. No ratings
means no result: the report says so instead of filling the gap.
"""
import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summarise(key: dict, rows: list[dict], in_app: list[dict] | None = None) -> dict:
    groups: dict = defaultdict(lambda: {"sounds_like": [], "would_post": [], "template": []})
    per_person: dict = defaultdict(lambda: defaultdict(list))
    raters = set()
    for r in rows:
        k = key.get((r.get("item") or "").strip())
        score = (r.get("sounds_like_1_to_5") or "").strip()
        if not k or not score:
            continue
        raters.add(r.get("rater", "").strip() or "anonymous")
        g = groups[k["group"]]
        g["sounds_like"].append(int(score))
        g["would_post"].append((r.get("would_post_yes_no") or "").strip().lower().startswith("y"))
        g["template"].append((r.get("reads_like_template_yes_no") or "").strip().lower().startswith("y"))
        per_person[k["person"]][k["group"]].append(int(score))
    for r in in_app or []:
        g = groups["system_operator_in_app"]
        g["sounds_like"].append(r["voice"])
        g["would_post"].append(bool(r["would_post"]))
        g["template"].append(bool(r["template"]))

    def agg(g):
        n = len(g["sounds_like"])
        return {"n": n, "sounds_like_mean": round(mean(g["sounds_like"]), 2) if n else None,
                "would_post_rate": round(sum(g["would_post"]) / n, 2) if n else None,
                "template_rate": round(sum(g["template"]) / n, 2) if n else None}
    out = {"raters": sorted(raters), "groups": {k: agg(v) for k, v in groups.items()},
           "per_person": {p: {g: round(mean(v), 2) for g, v in gs.items()} for p, gs in per_person.items()}}
    out["headline"] = "no human ratings yet" if not out["groups"] else \
        f"system {out['groups'].get('system', {}).get('sounds_like_mean')} vs real writing " \
        f"{out['groups'].get('real_heldout', {}).get('sounds_like_mean')} vs template " \
        f"{out['groups'].get('template_baseline', {}).get('sounds_like_mean')} (sounds like the person, 1-5)"
    return out


def in_app_ratings(dbs: list[str]) -> list[dict]:
    out = []
    for db in dbs:
        con = sqlite3.connect(db)
        out += [{"voice": v, "would_post": w, "template": t} for v, w, t in con.execute(
            "select rating_voice, rating_would_post, rating_template from posts where rating_voice is not null")]
        con.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--ratings", action="append", default=[])
    ap.add_argument("--db", action="append", default=[])
    ap.add_argument("--out", default=str(ROOT / "evals" / "reports" / "human-ratings.json"))
    a = ap.parse_args()
    rows = [r for f in a.ratings for r in csv.DictReader(open(f))]
    rep = summarise(json.loads(Path(a.key).read_text()), rows, in_app_ratings(a.db))
    Path(a.out).write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
