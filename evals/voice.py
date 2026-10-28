"""Does the output sound like the founder, or like a LinkedIn template? Deterministic measures first; humans decide.

  python -m evals.voice --db run1.db --db run2.db ...      # posts from live runs of the voice_samples profiles
  python -m evals.voice --samples-only                      # only the sanity check on the real writing

Data: evals/datasets/voice_samples/<person>.json (excerpts of six real people's public writing: 6 the system may
see, 2 held out it never sees) and template_baseline.json (deliberately generic posts, the control group).

1. Voice distance. Each text becomes a vector of style features (sentence length and spread, paragraph shape, lists,
   questions, person, punctuation, word length, reading grade...) plus function-word frequencies, standardised over
   all real samples. The distance from a text to a person is the mean absolute difference from that person's
   held-out samples (Burrows' Delta, extended with the style features). A post should be closer to its own founder
   than to the other five. Sanity check first: do held-out samples land nearest their own author, measured against
   the training samples? If the measure cannot tell the real people apart, it proves nothing about generated posts.
2. Template detector. The skeleton of each post (structure_vector) is compared across clients: posts for different
   founders should be no more alike than the six real people's own writing is. Plus the share of posts using a
   generic-template pattern their founder never uses.
3. Human verdict: scripts/export_blind_review.py and scripts/import_ratings.py. The model-graded critic score is not
   used here; it is the product's editor's check, not evidence.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean, pstdev

from app.agents.content.style import features, function_profile, structure_vector, style_card, template_problems

HERE = Path(__file__).parent
DATA = HERE / "datasets" / "voice_samples"


def load_people() -> dict[str, dict]:
    out = {}
    for f in sorted(DATA.glob("*.json")):
        if f.stem == "template_baseline":
            continue
        d = json.loads(f.read_text())
        out[f.stem] = {"person": d["person"], "company": d["client_profile"]["company_name"],
                       "train": [s["excerpt"] for s in d["samples"] if s["split"] == "train"],
                       "held_out": [s["excerpt"] for s in d["samples"] if s["split"] == "held_out"]}
    return out


def template_baseline() -> dict[str, str]:
    return json.loads((DATA / "template_baseline.json").read_text())["posts"]


class Space:
    """Standardised feature space fitted on every real sample (train and held-out) of every person."""

    def __init__(self, texts: list[str]):
        rows = [self.raw(t) for t in texts]
        self.mu = [mean(c) for c in zip(*rows)]
        self.sd = [pstdev(c) or 1.0 for c in zip(*rows)]

    @staticmethod
    def raw(text: str) -> list[float]:
        f = features(text)
        style = [f[k] for k in ("sentence_len", "sentence_len_sd", "sentences_per_paragraph", "one_sentence_paragraphs",
                                "list_lines", "questions", "exclamations", "first_person", "we", "you", "dashes",
                                "colons", "parens", "emojis", "hashtags", "word_len", "type_token", "grade")]
        return style + function_profile(text)

    def z(self, text: str) -> list[float]:
        return [(x - m) / s for x, m, s in zip(self.raw(text), self.mu, self.sd)]

    def centroid(self, texts: list[str]) -> list[float]:
        return [mean(c) for c in zip(*[self.z(t) for t in texts])]

    def delta(self, text: str, centroid: list[float]) -> float:
        return mean(abs(a - b) for a, b in zip(self.z(text), centroid))


def sanity(people: dict[str, dict]) -> dict:
    """Held-out samples vs each person's training samples: is the nearest person the right one?"""
    space = Space([t for p in people.values() for t in p["train"] + p["held_out"]])
    refs = {pid: space.centroid(p["train"]) for pid, p in people.items()}
    hits, total, margins = 0, 0, []
    for pid, p in people.items():
        for t in p["held_out"]:
            d = {q: space.delta(t, c) for q, c in refs.items()}
            hits += min(d, key=d.get) == pid
            total += 1
            margins.append(min(v for q, v in d.items() if q != pid) - d[pid])
    every = [(pid, t) for pid, p in people.items() for t in p["train"] + p["held_out"]]
    loo = 0
    for i, (pid, t) in enumerate(every):            # leave one out over all samples: a steadier estimate
        c = {q: space.centroid([x for j, (qq, x) in enumerate(every) if qq == q and j != i]) for q in people}
        loo += min(c, key=lambda q: space.delta(t, c[q])) == pid
    return {"heldout_nearest_own_rate": round(hits / total, 3), "heldout_samples": total,
            "leave_one_out_nearest_own_rate": round(loo / len(every), 3), "samples": len(every),
            "chance": round(1 / len(people), 3), "mean_margin": round(mean(margins), 3)}


def voice_distance(people: dict[str, dict], generated: dict[str, list[str]]) -> dict:
    """Generated posts and the template baseline, against every person's held-out samples."""
    space = Space([t for p in people.values() for t in p["train"] + p["held_out"]])
    refs = {pid: space.centroid(p["held_out"]) for pid, p in people.items()}
    baseline = template_baseline()
    per, hits, total = {}, 0, 0
    for pid, posts in generated.items():
        if pid not in refs or not posts:
            continue
        own = [space.delta(t, refs[pid]) for t in posts]
        others = [min(space.delta(t, refs[q]) for q in refs if q != pid) for t in posts]
        nearest = [min(refs, key=lambda q: space.delta(t, refs[q])) == pid for t in posts]
        hits += sum(nearest)
        total += len(posts)
        per[pid] = {"posts": len(posts), "to_own_founder": round(mean(own), 3), "to_nearest_other": round(mean(others), 3),
                    "nearest_is_own_rate": round(sum(nearest) / len(posts), 3),
                    "template_baseline_to_own_founder": round(space.delta(baseline[pid], refs[pid]), 3) if pid in baseline else None,
                    "real_heldout_to_own_train": round(mean(space.delta(t, space.centroid(people[pid]["train"]))
                                                            for t in people[pid]["held_out"]), 3)}
    closer_than_template = [v for v in per.values() if v["template_baseline_to_own_founder"] is not None]
    return {"per_person": per, "voice_nearest_own_rate": round(hits / total, 3) if total else None,
            "voice_posts_closer_than_template_rate": round(sum(v["to_own_founder"] < v["template_baseline_to_own_founder"]
                                                               for v in closer_than_template) / len(closer_than_template), 3)
            if closer_than_template else None}


def skeleton_similarity(a: list[float], b: list[float]) -> float:
    """1 = the same skeleton; falls toward 0 as two posts are built differently (exp of minus the Euclidean distance;
    a plain cosine of these all-positive vectors put almost every pair near 1)."""
    import math
    return math.exp(-math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b))))


def _pairwise(vecs: list[list[float]]) -> float | None:
    sims = [skeleton_similarity(a, b) for a, b in itertools.combinations(vecs, 2)]
    return round(mean(sims), 3) if sims else None


def template_report(people: dict[str, dict], generated: dict[str, list[str]], weeks: dict[str, list[list[str]]] | None = None) -> dict:
    """Are posts for different founders built on one skeleton? Compared with the real people's own writing."""
    human_cross = []
    for a, b in itertools.combinations(people, 2):
        human_cross += [skeleton_similarity(structure_vector(x), structure_vector(y))
                        for x in people[a]["train"] for y in people[b]["train"]]
    gen_cross = []
    pids = [p for p in generated if generated[p]]
    for a, b in itertools.combinations(pids, 2):
        gen_cross += [skeleton_similarity(structure_vector(x), structure_vector(y))
                      for x in generated[a] for y in generated[b]]
    base = template_baseline()
    flagged, n = 0, 0
    per = {}
    for pid in pids:
        card = style_card(people[pid]["train"]) if pid in people else {}
        probs = [template_problems(t, card) for t in generated[pid]]
        flagged += sum(1 for pr in probs if pr)
        n += len(probs)
        per[pid] = {"posts": len(probs), "with_generic_pattern": sum(1 for pr in probs if pr),
                    "patterns": sorted({x for pr in probs for x in pr})}
    out = {"human_cross_person_structure_similarity": round(mean(human_cross), 3),
           "generated_cross_client_structure_similarity": round(mean(gen_cross), 3) if gen_cross else None,
           "template_baseline_cross_structure_similarity": _pairwise([structure_vector(t) for t in base.values()]),
           "generic_template_post_rate": round(flagged / n, 3) if n else None, "per_client": per}
    if out["generated_cross_client_structure_similarity"] is not None:
        out["template_similarity_excess"] = round(out["generated_cross_client_structure_similarity"] -
                                                  out["human_cross_person_structure_similarity"], 3)
    if weeks:
        out["same_client_across_weeks_similarity"] = {pid: _pairwise([structure_vector(t) for wk in w for t in wk])
                                                      for pid, w in weeks.items()}
    return out


# ---------------------------------------------------------------- posts from live-run databases
def posts_from_db(path: str) -> tuple[str | None, list[str], list[list[str]]]:
    """(person id, delivered post texts, the same grouped by calendar week) from a live run's SQLite database."""
    people = {p["company"]: pid for pid, p in load_people().items()}
    con = sqlite3.connect(path)
    rows = con.execute("select t.name, p.text, s.week from posts p join tenants t on t.id = p.tenant_id "
                       "left join calendar_slots s on s.id = p.slot_id "
                       "where p.status not in ('blocked', 'drafting') order by s.publish_date").fetchall()
    con.close()
    if not rows:
        return None, [], []
    pid = people.get(rows[0][0])
    by_week: dict = {}
    for _, text, week in rows:
        by_week.setdefault(week, []).append(text)
    return pid, [r[1] for r in rows], list(by_week.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", default=[])
    ap.add_argument("--samples-only", action="store_true")
    ap.add_argument("--out", default=str(HERE / "reports" / "voice-live"))
    args = ap.parse_args()
    people = load_people()
    report = {"people": {pid: p["person"] for pid, p in people.items()}, "sanity": sanity(people)}
    if not args.samples_only:
        generated, weeks = {}, {}
        for db in args.db:
            pid, posts, wk = posts_from_db(db)
            if pid:
                generated.setdefault(pid, []).extend(posts)
                weeks.setdefault(pid, []).extend(wk)
        report["voice"] = voice_distance(people, generated)
        report["template"] = template_report(people, generated, weeks)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
