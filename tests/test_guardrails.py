import pytest

from app.adapters.data.web_corpus import PAGES
from app.adapters.sources import SeededLeadSource, dataset_case
from app.agents.runtime.loop import CANARY
from app.guardrails.input_guard import scan
from app.guardrails.output_guard import PolicyContext, check_outreach, check_post
from app.guardrails.state import POST_TRANSITIONS, IllegalTransition, transition

POLICY = PolicyContext(
    tenant_name="LedgerLoop", competitors=["ClearTally", "BooksBridge"], banned_topics=["crypto"],
    banned_phrases=["guaranteed savings"],
    required_disclaimer={"triggers": ["tax"], "text": "General information, not tax or financial advice."},
    other_tenant_terms=["ShiftWise", "Marcus Okafor", "GridMint"],
    source_texts=["Customers close books in 3 days on average, down from 12 days. 61% had a shortfall."],
)
GOOD = ("Most founders don't have a revenue problem. They have a timing problem.\n\n"
        "In a recent survey, 61% of small businesses had at least one cash-flow shortfall last year. "
        "The fix is boring: look at cash weekly, chase invoices early, and know your runway in weeks.\n\n"
        "Our customers close their books in 3 days. That's not magic, it's fewer manual steps. "
        "What's the one finance habit that changed your business?")


def page(pid):
    return next(p for p in PAGES if p["id"] == pid)


async def test_hidden_injection_is_stripped_not_quarantined():
    v = await scan(page("clt-pricing")["text"])
    assert v.hidden_injection and not v.quarantined
    assert "ignore all previous" not in v.clean_text.lower() and "​" not in v.clean_text
    assert "self-serve bookkeeping" in v.clean_text


async def test_visible_injection_is_quarantined():
    v = await scan(page("nursing-trends-2026")["text"])
    assert v.quarantined and v.score >= 0.8


async def test_clean_page_passes():
    v = await scan(page("smb-cash-report")["text"])
    assert not v.flagged and not v.quarantined


async def test_lead_signal_injection_flagged():
    p = next(x for x in SeededLeadSource().search(titles=["Finance Director", "Head of Finance"],
                                                   industries=["Marketing Agency"], limit=500)
             if dataset_case(x["id"]) == "injection")
    assert (await scan(p["signal"])).flagged


def test_good_post_passes():
    r = check_post(GOOD, ["#smallbusiness"], POLICY)
    assert r.passed, r.violations


@pytest.mark.parametrize("text,rule", [
    (GOOD + " Unlike ShiftWise, we focus on finance.", "cross_tenant_leak"),
    (GOOD + f" {CANARY}", "system_prompt_leak"),
    (GOOD + " Email me at priya@ledgerloop.example", "pii"),
    (GOOD + " Call 555-201-0100 today.", "pii"),
    (GOOD + " ClearTally is a scam and overpriced.", "competitor_disparagement"),
    (GOOD + " 83% of founders hate bookkeeping.", "uncited_stat"),
    (GOOD + " We also love crypto.", "banned_topic"),
    (GOOD + " Here is how to cut your tax bill.", "disclaimer_missing"),
])
def test_post_blocking_rules(text, rule):
    r = check_post(text, [], POLICY)
    assert not r.passed and rule in {v.rule for v in r.violations}


def test_near_duplicate_blocked():
    p = PolicyContext(**{**POLICY.__dict__, "previous_posts": [GOOD]})
    assert "near_duplicate" in {v.rule for v in check_post(GOOD, [], p).violations}


def test_warnings_do_not_block():
    r = check_post(GOOD + " This is a game-changer.", ["#a"] * 7, POLICY)
    assert r.passed and {"banned_phrase", "hashtags"} <= {w.rule for w in r.warnings}


def test_outreach_rules():
    ok = ("Hi Alex, saw that Cedarblue Creative just opened a second office in Manchester. Congrats. "
          "Growing teams often find month-end close stretching out. We help agencies close in days, not weeks. "
          "Would a 15-minute look at how that works be useful?")
    assert check_outreach(ok, "Cedarblue Creative", "opened a second office in Manchester", POLICY).passed
    bad = ok + " Great to meet you at the conference! ACT NOW, limited time."
    rules = {v.rule for v in check_outreach(bad, "Cedarblue Creative", "", POLICY).violations}
    assert {"false_familiarity", "spam_signal"} <= rules


def test_state_machine_refuses_illegal_moves():
    class P:
        status = "drafting"
    p = P()
    with pytest.raises(IllegalTransition):
        transition(p, "published", POST_TRANSITIONS)
    transition(p, "pending_approval", POST_TRANSITIONS)
    with pytest.raises(IllegalTransition):
        transition(p, "scheduled", POST_TRANSITIONS)  # must be approved first
    transition(p, "approved", POST_TRANSITIONS)
    transition(p, "scheduled", POST_TRANSITIONS)


def test_lead_source_hides_ground_truth():
    rows = SeededLeadSource().search(titles=["CFO"], industries=["E-commerce"], limit=5)
    assert rows and all("_case" not in r and "vertical" not in r for r in rows)


def test_lead_source_normalizes_country_names():
    """Found in a live run: the model sent 'United States' where the source stores 'US'."""
    src = SeededLeadSource()
    by_code = src.search(titles=["CFO"], industries=["E-commerce"], geos=["US"], limit=50)
    by_name = src.search(titles=["CFO"], industries=["E-commerce"], geos=["United States"], limit=50)
    assert by_code and [r["id"] for r in by_code] == [r["id"] for r in by_name]
