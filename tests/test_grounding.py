"""Factual grounding: claim-level checks, copy detection, and the regressions found in the first live run."""
import pytest
from sqlalchemy import select

from app.agents.schemas import Claim
from app.core.tenancy import tenant_scope
from app.db.models import Post
from app.guardrails.output_guard import PolicyContext, check_claims, check_copying, check_post

FACTS = "LedgerLoop serves 1,100 SMB customers across 12 countries. Month-end close time falls 83% in the first quarter."
SOURCES = {"client:verified-facts": FACTS, "https://www.smbfinanceweekly.example/survey":
           "Our survey of 2,400 small business owners found 61% had a cash-flow shortfall. Only 28% of owners trust their books."}
FOUNDER_SAMPLES = ["Most founders treat month-end close as an accounting chore.\n\nIt is a leading indicator. Late close, late decisions."]


def policy(**kw) -> PolicyContext:
    return PolicyContext("LedgerLoop", source_docs=SOURCES, source_texts=list(SOURCES.values()),
                         founder_samples=FOUNDER_SAMPLES, **kw)


def claim(num, quote, url="client:verified-facts", statement=""):
    return Claim(number=num, statement=statement or quote, source_url=url, source_quote=quote)


def rules(report_or_list) -> set[str]:
    vs = report_or_list if isinstance(report_or_list, list) else report_or_list.violations + report_or_list.warnings
    return {v.rule for v in vs}


# ---- the exact bug from the live run: a customer count presented as a market statistic ---------------------------
def test_regression_customer_count_used_as_market_size():
    text = "Here is the thing: the 1,100 SMBs still guessing their cash position lose weeks every quarter."
    c = [claim("1,100", "LedgerLoop serves 1,100 SMB customers across 12 countries")]
    assert "metric_misattributed" in rules(check_claims(text, c, policy()))


def test_same_number_used_for_what_it_actually_measures_passes():
    text = "We serve 1,100 SMB customers, and 12 countries have taught us the same lesson."
    c = [claim("1,100", "LedgerLoop serves 1,100 SMB customers across 12 countries"),
         claim("12", "LedgerLoop serves 1,100 SMB customers across 12 countries")]
    assert rules(check_claims(text, c, policy())) == set()


def test_percentage_claim_with_matching_quote_passes():
    text = "Close time falls 83% in the first quarter once the rules are written down."
    assert rules(check_claims(text, [claim("83%", "Month-end close time falls 83% in the first quarter")], policy())) == set()


@pytest.mark.parametrize("text,claims,expected", [
    ("Only 40% of teams do this.", [], "unclaimed_stat"),                                              # figure with no claim
    ("Close time falls 83% in a quarter.", [claim("83%", "close time drops by 83 percent")], "claim_quote_not_in_source"),  # invented quote
    ("About 61% had a shortfall.", [claim("61%", "had a shortfall 61%", "https://made-up.example/x")], "claim_unknown_source"),
    ("Close time falls 90% in a quarter.", [claim("90%", "Month-end close time falls 83% in the first quarter")], "claim_quote_missing_number"),
])
def test_claim_checks_block_fabrication(text, claims, expected):
    assert expected in rules(check_claims(text, claims, policy()))


def test_years_and_small_counts_are_not_claims():
    assert rules(check_claims("In 2026 I learned three lessons and 5 habits.", [], policy())) == set()


def test_human_edits_skip_claim_bookkeeping_but_keep_number_check():
    p = policy()
    ok = check_post("We close 83% faster. " * 20, [], p)           # exists in sources: no claim list demanded
    assert "unclaimed_stat" not in rules(ok)
    bad = check_post("We close 97% faster. " * 20, [], p)           # exists nowhere
    assert "uncited_stat" in rules(bad)


# ---- copied example hooks: the second finding from the live run ---------------------------------------------------
def test_regression_copied_founder_hook_is_blocked():
    text = "Most founders treat month-end close as an accounting chore.\n\nHere is what changed for us when we stopped."
    assert "copied_founder_hook" in rules(check_copying(text, policy()))


def test_hook_copied_from_voice_profile_examples_is_blocked():
    p = policy(example_hooks=["Nobody tells you how lonely the first hire is."])
    assert "copied_founder_hook" in rules(check_copying("Nobody tells you how lonely the first hire is.\n\nMine was a disaster.", p))


def test_new_hook_in_same_voice_is_allowed():
    assert rules(check_copying("Quarter-end is a fire drill only if nobody owns it.\n\nWe gave it an owner.", policy())) == set()


def test_post_overlapping_a_founder_sample_is_blocked():
    text = FOUNDER_SAMPLES[0] + "\n\nOne more line."
    assert "copies_founder_sample" in rules(check_copying(text, policy()))


# ---- end to end through the writer: planted flaws never reach the founder as passing drafts --------------------------
async def test_writer_claims_are_verified_in_pipeline(db):
    from app.agents.orchestrator.engine import Orchestrator
    from app.fixtures.seed import seed
    from app.llm.gateway import LLMGateway, set_gateway
    set_gateway(LLMGateway())
    ids = await seed(db)
    orch, tid = Orchestrator(), ids["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
    await orch.execute(tid, await orch.create_run(tid, "weekly", {}))
    with tenant_scope(tid):
        async with db.reader() as s:
            posts = (await s.execute(select(Post).where(Post.status == "pending_approval"))).scalars().all()
    assert posts
    from app.agents.blackboard import policy_context
    pol = await policy_context(tid, include_previous_posts=False)
    for p in posts:
        assert not [v for v in check_claims(p.text, [Claim(**c) for c in p.claims], pol)], p.text
        assert not check_copying(p.text, pol)
