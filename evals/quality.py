"""Deterministic quality report: factual accuracy, brand voice, output quality and safety, measured in code.

It complements the LLM judge (which scores voice and hook quality and is itself calibrated against a human): every
metric here is computed by the same deterministic checks production uses, over what a founder would actually see.

  python -m evals.run_evals --sections pipeline,quality          # CI: over the mock pipeline's drafts
  python -m evals.run_evals --sections quality --from-db PATH    # over a real live run's database (read only)

Lead screening is measured separately over the labelled fixture dataset: every suppressed, competitor and
contactless prospect must be screened out by code before a model sees it, and no good prospect may be.
"""
from __future__ import annotations

import re
from collections import Counter

DELIVERED = ("pending_approval", "approved", "scheduled", "published")
SAFETY_RULES = {"pii", "cross_tenant_leak", "system_prompt_leak", "agency_name_leak", "competitor_disparagement",
                "banned_topic"}
FACT_RULES = {"uncited_stat", "unclaimed_stat", "claim_unknown_source", "claim_quote_not_in_source",
              "claim_quote_missing_number", "metric_misattributed"}
VOICE_RULES = {"copied_founder_hook", "copies_founder_sample", "banned_phrase"}

QUALITY_ROWS = [
    ("Factual accuracy", "Delivered drafts with a figure not backed by a verbatim source quote", "quality_ungrounded_figure_posts"),
    ("Factual accuracy", "Delivered drafts that use figures (each checked)", "quality_posts_with_figures"),
    ("Factual accuracy", "Research claims kept as verified / flagged unverified", "quality_research_claims"),
    ("Factual accuracy", "Research figures labelled outdated (3+ years old)", "quality_outdated_flagged"),
    ("Brand voice", "Drafts using a banned phrase (global or the client's own)", "quality_banned_phrase_posts"),
    ("Brand voice", "Drafts using a word the voice profile says to avoid", "quality_avoided_word_posts"),
    ("Brand voice", "Drafts copying the founder's own sample posts or hooks", "quality_copied_sample_posts"),
    ("Output quality", "Drafts delivered for review", "quality_delivered_posts"),
    ("Output quality", "Share of delivered drafts at or above the editor's bar", "quality_at_bar_share"),
    ("Output quality", "Mean editor score of delivered drafts", "quality_mean_critic_score"),
    ("Output quality", "Pairs of delivered drafts that are near duplicates", "quality_near_duplicate_pairs"),
    ("Output quality", "Length within 300-3000 characters and at most 5 hashtags", "quality_format_compliance"),
    ("Safety", "Safety violations in delivered drafts (PII, leaks, disparagement, banned topics)", "quality_safety_violations_delivered"),
    ("Safety", "Attempts the guardrails blocked before a human saw them", "quality_blocked_attempts"),
    ("Lead screening", "Suppressed prospects screened out", "lead_screen_recall_suppressed"),
    ("Lead screening", "Competitors screened out", "lead_screen_recall_competitor"),
    ("Lead screening", "Contactless prospects screened out", "lead_screen_recall_missing_contact"),
    ("Lead screening", "Duplicate people kept twice", "lead_screen_duplicates_kept"),
    ("Lead screening", "Good prospects wrongly screened out", "lead_screen_fit_false_rejections"),
]


async def content_quality(tenant_ids: list[str]) -> dict:
    from sqlalchemy import select

    from app.agents.blackboard import latest_meta, policy_context
    from app.core.config import get_settings
    from app.core.tenancy import tenant_scope
    from app.db.models import Post
    from app.db.session import get_db
    from app.agents.schemas import Claim
    from app.guardrails.output_guard import check_post, jaccard

    bar = get_settings().critic_pass_score
    agg: Counter = Counter()
    scores: list[float] = []
    blocked_rules: Counter = Counter()
    research_verified = research_flagged = outdated = 0
    for tid in tenant_ids:
        with tenant_scope(tid):
            async with get_db().reader() as s:
                posts = (await s.execute(select(Post))).scalars().all()
        policy = await policy_context(tid, include_previous_posts=False)
        voice = (await latest_meta(tid, "voice_profile"))
        avoid = [w.lower() for w in ((voice.data if voice else {}) or {}).get("vocabulary_avoid", []) if len(w) > 2]
        delivered = [p for p in posts if p.status in DELIVERED]
        for p in [p for p in posts if p.status == "blocked"]:
            for v in (p.guardrail_report or {}).get("violations", []):
                blocked_rules[v["rule"]] += 1
        for p in delivered:
            text = p.approved_text or p.text
            edited_by_human = bool(p.approved_text and p.approved_text.strip() != p.text.strip())
            claims = None if edited_by_human else [Claim(**c) for c in (p.claims or [])]
            report = check_post(text, p.hashtags or [], policy, claims)
            rules = {v.rule for v in report.violations + report.warnings}
            agg["delivered"] += 1
            agg["with_figures"] += bool(p.claims) or bool(rules & FACT_RULES)
            agg["ungrounded"] += bool(rules & FACT_RULES)
            agg["banned_phrase"] += "banned_phrase" in rules
            agg["copied"] += bool(rules & {"copied_founder_hook", "copies_founder_sample"})
            agg["safety"] += len([v for v in report.violations if v.rule in SAFETY_RULES])
            agg["format_ok"] += 300 <= len(text) <= 3000 and len(p.hashtags or []) <= 5
            agg["avoided_word"] += any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text.lower()) for w in avoid)
            scores.append(p.critic_score)
        for i, a in enumerate(delivered):
            for b in delivered[i + 1:]:
                agg["near_dup"] += jaccard(a.approved_text or a.text, b.approved_text or b.text) >= 0.5
        for kind, field in (("market_report", "key_facts"), ("competitor_matrix", "competitors"), ("trend_report", "trends")):
            art = await latest_meta(tid, kind)
            if art:
                items = art.data.get(field, [])
                research_verified += sum(1 for x in items if x.get("verified"))
                research_flagged += len(art.data.get("_unverified", [])) + sum(1 for x in items if not x.get("verified"))
                outdated += sum(1 for x in items if x.get("status") == "outdated" or "years" in (x.get("note") or "")
                                or re.search(r"refers to \d{4}", x.get("note") or ""))
    n = max(1, agg["delivered"])
    return {
        "quality_delivered_posts": agg["delivered"], "quality_posts_with_figures": agg["with_figures"],
        "quality_ungrounded_figure_posts": agg["ungrounded"], "quality_banned_phrase_posts": agg["banned_phrase"],
        "quality_avoided_word_posts": agg["avoided_word"], "quality_copied_sample_posts": agg["copied"],
        "quality_safety_violations_delivered": agg["safety"], "quality_near_duplicate_pairs": agg["near_dup"],
        "quality_format_compliance": round(agg["format_ok"] / n, 3),
        "quality_at_bar_share": round(sum(x >= bar for x in scores) / n, 3),
        "quality_mean_critic_score": round(sum(scores) / n, 2) if scores else None,
        "quality_blocked_attempts": sum(blocked_rules.values()), "quality_blocked_by_rule": dict(blocked_rules),
        "quality_research_claims": f"{research_verified} / {research_flagged}",
        "quality_research_verified_share": round(research_verified / max(1, research_verified + research_flagged), 3),
        "quality_outdated_flagged": outdated,
    }


def lead_screening(slug: str = "ledgerloop") -> dict:
    """Run every labelled prospect of one demo client through the production screening function."""
    from app.agents.leads.agents import dedupe_key, screen_prospect
    from app.fixtures.clients import CLIENTS
    from app.fixtures.sources import _prospects

    client = next(c for c in CLIENTS if c["slug"] == slug)
    sup = client.get("suppression", [])
    sup_email = {x["value"].lower() for x in sup if x["kind"] == "email"}
    sup_domain = {x["value"].lower() for x in sup if x["kind"] == "domain"}
    sup_company = {x["value"].lower() for x in sup if x["kind"] == "company"}
    competitors = {c.lower() for c in client["intake"].get("known_competitors", [])}
    rows = [dict(p, requires_contact=True) for p in _prospects() if p["vertical"] == slug]
    seen: set[str] = set()
    kept_names: Counter = Counter()
    outcome: dict[str, Counter] = {}
    for p in rows:
        reason = screen_prospect(p, seen, sup_email, sup_domain, sup_company, competitors)
        outcome.setdefault(p["_case"], Counter())[reason or "kept"] += 1
        if reason is None:
            seen.add(dedupe_key(p))
            kept_names[p["full_name"].lower()] += 1

    def recall(case: str) -> float | None:
        c = outcome.get(case)
        return round(c[case] / sum(c.values()), 3) if c else None
    return {"lead_screen_recall_suppressed": recall("suppressed"), "lead_screen_recall_competitor": recall("competitor"),
            "lead_screen_recall_missing_contact": recall("missing_contact"),
            "lead_screen_duplicates_kept": sum(1 for n in kept_names.values() if n > 1),
            "lead_screen_fit_false_rejections": sum(v for k, v in outcome.get("fit", Counter()).items() if k != "kept"),
            "lead_screen_cases": {k: dict(v) for k, v in outcome.items()}}
