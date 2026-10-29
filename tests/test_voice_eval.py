"""The voice and template measures are instruments: check they measure something before trusting their verdicts."""
import pytest
from app.agents.content.style import describe, style_card, template_markers, template_problems
from evals.voice import load_people, sanity, template_baseline, template_report, voice_distance


def test_the_dataset_has_six_people_with_held_out_samples_and_provenance():
    import json
    from evals.voice import DATA
    people = load_people()
    assert len(people) == 6 and all(len(p["train"]) == 6 and len(p["held_out"]) == 2 for p in people.values())
    for f in DATA.glob("*.json"):
        if f.stem == "template_baseline":
            continue
        d = json.loads(f.read_text())
        assert "never" not in d["note"] or "attributed" in d["note"]
        for smp in d["samples"]:
            assert smp["url"].startswith("https://") and "linkedin.com" not in smp["url"] and smp["retrieved"]
            assert 60 <= smp["words"] <= 230                               # short excerpts, not whole works
        held = {s["excerpt"] for s in d["samples"] if s["split"] == "held_out"}
        shown = {d["client_profile"][f"sample_{i}"] for i in range(1, 6)}
        assert not held & shown                                             # the system never sees the held-out two


def test_the_style_measure_tells_real_people_apart_better_than_chance():
    r = sanity(load_people())
    assert r["leave_one_out_nearest_own_rate"] >= 2 * r["chance"], r


def test_the_template_detector_flags_the_template_and_spares_real_writing():
    people = load_people()
    base = template_baseline()
    assert all(len(template_markers(t)) >= 4 for t in base.values())
    real = [t for p in people.values() for t in p["train"]]
    assert sum(len(template_markers(t)) >= 4 for t in real) == 0
    r = template_report(people, {pid: [base[pid]] for pid in people})
    assert r["generated_cross_client_structure_similarity"] >= 1.3 * r["human_cross_person_structure_similarity"]
    assert r["generic_template_post_rate"] == 1.0


def test_real_held_out_writing_is_closer_to_its_author_than_the_template_is():
    people = load_people()
    r = voice_distance(people, {pid: p["train"][:2] for pid, p in people.items()})
    assert r["voice_posts_closer_than_template_rate"] >= 0.8, r


def test_the_style_card_describes_what_the_founder_does_and_allows_their_own_habits():
    people = load_people()
    card = style_card(people["paras_chopra"]["train"])
    text = describe(card)
    assert "words" in text and "Ends with a question" in text
    listicle = "Short hook.\n\n1. one\n2. two\n3. three\n\nAgree?"
    assert "numbered or bulleted list" not in template_problems(listicle, card)          # Paras does use lists
    assert "numbered or bulleted list" in template_problems(listicle, style_card(people["seth_godin"]["train"]))


async def test_the_blind_review_kit_round_trips(db, tmp_path):
    """Export a shuffled, unlabelled sheet; fill it with made-up test ratings; import: the groups come back apart.
    These ratings are a test fixture, never a result."""
    if not db.url.startswith("sqlite"):
        pytest.skip("the review kit reads live runs' SQLite files")
    import csv
    import json
    import sys
    from pathlib import Path
    from app.core.tenancy import tenant_scope
    from app.db.models import Post
    from tests.helpers import bare_tenants
    from app.db.models import Tenant
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import export_blind_review as ex
    import import_ratings as im
    await bare_tenants(db, "sg")
    async with db.session() as s:
        (await s.get(Tenant, "sg")).name = "Seth Godin Workshops"
    with tenant_scope("sg"):
        async with db.session() as s:
            for i in range(4):
                s.add(Post(text=f"A system post number {i}. It talks about trust and small audiences.", status="pending_approval",
                           rating_voice=4, rating_would_post=True, rating_template=False))
    path = db.url.split("///", 1)[1]
    sections, key = ex.build([path], seed=1)
    ex.write(sections, key, tmp_path)
    page = (tmp_path / "review.html").read_text()
    for label in ("real_heldout", "template_baseline", "held out", "baseline"):
        assert label not in page.lower(), label                              # nothing says which item is which
    seth = next(s for s in sections if s["person"] == "seth_godin")
    groups = [key[i["id"]]["group"] for i in seth["items"]]
    assert sorted(groups) == ["real_heldout", "real_heldout", "system", "system", "system", "template_baseline"]
    rows = list(csv.DictReader((tmp_path / "ratings.csv").open()))
    fake = {"real_heldout": "5", "system": "3", "template_baseline": "1"}
    for r in rows:
        r.update(rater="test-fixture", sounds_like_1_to_5=fake[key[r["item"]]["group"]], would_post_yes_no="no",
                 reads_like_template_yes_no="yes" if key[r["item"]]["group"] == "template_baseline" else "no")
    rep = im.summarise(json.loads((tmp_path / "key.json").read_text()), rows, im.in_app_ratings([path]))
    g = rep["groups"]
    assert (g["real_heldout"]["sounds_like_mean"], g["system"]["sounds_like_mean"], g["template_baseline"]["sounds_like_mean"]) == (5, 3, 1)
    assert g["template_baseline"]["template_rate"] == 1.0 and g["system_operator_in_app"]["n"] == 4
    assert im.summarise({}, [])["headline"] == "no human ratings yet"
