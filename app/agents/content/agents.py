"""Content engine: writer <-> critic loop, guardrail gate, bounded redrafts, and learning from founder edits.

compose_post(): one attempt
  writer drafts -> critic (judge model, different family) scores against a rubric
  -> revise with must_fix until score >= threshold or max rounds (early exit saves cost)
  -> output guard -> up to two automatic fixes for high-severity issues (never for critical)
draft_slot(): up to 3 attempts per slot until one passes the guardrails and the editor's bar, each redraft told why
  the last one fell short -> Post row in pending_approval (founder sees it) or blocked (ops sees it).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.blackboard import compact, latest, policy_context, put_artifact, source_pack
from app.agents.content.style import describe, style_card, template_problems
from app.agents.runtime.loop import AgentContext, structured
from app.agents.schemas import Critique, LearnedRules, PostDraft, VoiceProfile
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import audit, incident
from app.db.models import CalendarSlot, Feedback, Post
from app.db.session import get_db
from app.guardrails.output_guard import GuardReport, Violation, check_post
from app.guardrails.state import POST_TRANSITIONS, transition

WRITER_SYSTEM = (
    "You ghostwrite LinkedIn posts for one founder, as the founder of their own company; never mention any agency, tool or "
    "service that helped write it. Write the way THIS founder writes, judged by their own excerpts and the measured style "
    "card in the brief: their usual length, paragraph shape, sentence rhythm, how they open and how they close, person "
    "(I or we), vocabulary, punctuation and formality. Make one specific point. "
    "Do not use the generic LinkedIn template unless the style card says this founder does: no one-line hook followed by "
    "a line per sentence, no numbered listicle, no closing engagement question (\"Agree?\", \"What do you think?\"), no "
    "stock phrases (\"Here's what I learned\", \"Let that sink in\", \"The truth is\"), no emojis, no hashtag tail. "
    "Use a number only if it appears in the provided sources, and list those sources' URLs in sources_used. "
    "Every figure (a percentage, money, a multiplier, or any count above 10) needs an entry in claims: the figure exactly "
    "as written, the sentence using it, the source URL, and a verbatim quote copied from that source. Use each figure only "
    "for what the quote says it measures: a customer count is a customer count, never a market size or a problem statistic. "
    "The founder's excerpts are style references only: never reuse their sentences, examples, analogies, people or "
    "stories; find your own for this topic. "
    "Never invent personal anecdotes, customer stories, dates, results or quotes: write only from the provided sources, the "
    "founder's own samples and general reasoning. If the sources have no figure for a point, make the point without a number. "
    "No hashtag spam (0-3 hashtags). Never mention other companies the agency works with."
)
# Anchored rubric: the scale means the same as the hand-scored calibration set (1 = unusable, 5 = publish as is), so the
# 4.0 bar means "publishable after light edits". v2 (after the review): "hook" and "cta_fit" rewarded the generic
# LinkedIn template, so they became "opening" and "ending" judged against the founder's own excerpts. The critic is the
# editor's check inside the product, not evidence of quality: that comes from blind human ratings (evals/voice.py).
CRITIC_SYSTEM = (
    "You are an editor checking a ghostwritten LinkedIn draft against the founder's own writing before the founder sees "
    "it. You get excerpts of the founder's real posts and the draft. Score each dimension 1-5 with these anchors: 5 = "
    "exceptional, publish as is; 4 = good, publishable after light edits; 3 = needs a real revision; 2 = weak; 1 = unusable. "
    "voice_match: would a reader of the excerpts believe the same person wrote this (rhythm, length, vocabulary, person, "
    "formality)? specificity: concrete details rather than generic advice. opening: opens the way this founder opens, not "
    "with a generic one-line hook. insight: says something non-obvious. ending: ends the way this founder ends; a generic "
    "engagement question (\"Agree?\", \"Thoughts?\") the founder never uses scores 1. Score the draft as it stands: a solid "
    "draft with one thing to improve is a 4. Put an item in must_fix only if it is needed to reach 4, naming the exact "
    "difference from the founder's writing. The writer is not allowed to invent anecdotes, customers, events, numbers or "
    "quotes, and must not reuse the founder's own examples: never ask for those. Ask only for changes it can make from "
    "the sources, the brief and the founder's style."
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def relevant_sources(sources: list[dict], slot: CalendarSlot, k: int = 3) -> list[dict]:
    """The few sources that matter for this slot (the client's own facts first), instead of every page read."""
    want = _words(f"{slot.topic} {slot.angle} {slot.pillar} {slot.trend_ref}")

    def score(s: dict) -> int:
        return len(want & _words(s["title"] + " " + s["text"])) + (100 if s["url"] == "client:verified-facts" else 0)
    return sorted(sources, key=score, reverse=True)[:k]


def founder_excerpts(samples: list[str], seed: str, k: int = 2, chars: int = 900) -> str:
    """Two of the founder's own posts, rotated per slot so every post does not imitate the same two."""
    if not samples:
        return "(none)"
    start = sum(map(ord, seed)) % len(samples)
    picked = [samples[(start + i) % len(samples)] for i in range(min(k, len(samples)))]
    return "\n---\n".join(p[:chars] for p in picked)


async def _slot(tenant_id: str, slot_id: str) -> CalendarSlot:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot_id))).scalars().one()


@dataclass
class Composed:
    """One finished attempt at a slot: the final draft, the editor's last verdict and the guardrail report."""
    draft: PostDraft
    critique: Critique | None
    rounds: int
    report: GuardReport

    @property
    def score(self) -> float:
        return self.critique.overall if self.critique else 0.0

    def ready(self, bar: float) -> bool:
        return self.report.passed and self.score >= bar

    def rank(self, bar: float) -> tuple:
        return (self.report.passed, self.score >= bar, self.score)

    def why_not(self, bar: float) -> str:
        if not self.report.passed:
            return "blocked by guardrails (" + ", ".join(sorted({v.rule for v in self.report.violations})) + ")"
        return f"editor score {self.score:.1f} below {bar:.1f}"


def redraft_note(c: Composed, bar: float) -> str:
    if not c.report.passed:
        rules = "; ".join(f"{v.rule}: {v.detail}" for v in c.report.violations)[:600]
        return (f"The previous draft was blocked ({rules}). Write a new post: no statistics unless quoted from a source, "
                f"no invented stories, customers or results.")
    fixes = "; ".join(c.critique.must_fix or [c.critique.verdict]) if c.critique else ""
    return f"An editor scored the previous draft {c.score:.1f}/5 (bar {bar:.1f}). Write a stronger post. Fix: {fixes}"[:600]


async def draft_slot(tenant_id: str, slot_id: str, ctx: AgentContext, note: str = "", attempts: int | None = None,
                     deadline: float | None = None) -> Post:
    """Bounded redraft loop for one calendar slot.

    Up to `attempts` fresh drafts, each told why the previous one fell short, until one passes the guardrails and the
    editor's bar; no new attempt starts after `deadline` (time.monotonic). Blocked attempts are kept (agency-only) for
    the audit trail. If no attempt clears the bar, the best guardrail-passing draft still reaches the founder, labelled
    as below the editor's bar; a draft that never passed the guardrails never does."""
    bar = get_settings().critic_pass_score
    attempts = attempts or get_settings().draft_attempts
    best: Composed | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1 and deadline is not None and time.monotonic() > deadline:
            ctx.event("writer", "redraft_stopped", slot=slot_id, reason="time budget reached")
            break
        c = await compose_post(tenant_id, slot_id, ctx, note=note)
        if c.ready(bar):
            post = await save_post(tenant_id, slot_id, c, ctx, final=True)
            await ctx.flush()                 # progress is visible while the rest of the week is still drafting
            return post
        ctx.event("writer", "attempt_fell_short", slot=slot_id, attempt=attempt, reason=c.why_not(bar), score=c.score)
        if not c.report.passed:
            await save_post(tenant_id, slot_id, c, ctx, final=False)        # audit trail: what the guardrails stopped
        await ctx.flush()
        plateau = best is not None and best.report.passed and c.report.passed and c.score < best.score + 0.2
        if best is None or c.rank(bar) > best.rank(bar):
            best = c
        if plateau:                 # another attempt did not help: more of the same will not either
            ctx.event("writer", "redraft_stopped", slot=slot_id, reason="no improvement", best=best.score)
            break
        note = redraft_note(c, bar)
    assert best is not None
    if not best.report.passed:                 # nothing passed the guardrails: the last blocked attempt is the record
        return await _mark_slot_blocked(tenant_id, slot_id, ctx)
    return await save_post(tenant_id, slot_id, best, ctx, final=True)


async def compose_post(tenant_id: str, slot_id: str, ctx: AgentContext, note: str = "",
                       max_rounds: int | None = None) -> Composed:
    st = get_settings()
    slot = await _slot(tenant_id, slot_id)
    voice_full = await latest(tenant_id, "voice_profile") or {}
    voice = {k: v for k, v in voice_full.items() if k != "example_hooks"}   # hooks are style evidence, never wording to reuse
    strategy = await latest(tenant_id, "content_strategy") or {}
    sources = relevant_sources(await source_pack(tenant_id), slot)
    pillar = next((p for p in strategy.get("pillars", []) if p["name"] == slot.pillar), {})
    policy = await policy_context(tenant_id)
    card = style_card(policy.founder_samples)
    excerpts = founder_excerpts(policy.founder_samples, slot.id)
    src_text = "\n".join(f"[{s['url']}] {s['title']}: {s['text'][:700]}" for s in sources)
    brief = (f"Topic: {slot.topic}\nAngle: {slot.angle}\nFormat: {slot.format}\nPillar: {slot.pillar} - "
             f"{pillar.get('description', '')}\nTrend reference: {slot.trend_ref or 'none'}\n"
             f"Style card, measured from the founder's own posts:\n{describe(card)}\n"
             f"Excerpts of the founder's own writing (match how they write; never reuse their sentences or stories):\n"
             f"{excerpts}\nFounder voice notes: {compact(voice, 700)}\nSources you may cite:\n{src_text}")
    recent = [t.split("\n")[0] for t in policy.previous_posts[:6]]
    if recent:
        brief += "\nRecent openings (don't reuse these hooks or structures): " + " | ".join(recent)
    if note:
        brief += f"\nReviewer's note to address: {note}"
    meta = {"slot": {"id": slot.id, "topic": slot.topic, "angle": slot.angle, "pillar": slot.pillar,
                     "format": slot.format, "trend_ref": slot.trend_ref}, "voice": voice, "sources": sources,
            "note": note}

    draft = (await structured("writer", "big", WRITER_SYSTEM, brief, PostDraft, ctx, temperature=0.7, max_tokens=1000,
                              metadata={**meta, "round": 0})).output
    critique: Critique | None = None
    rounds = 0
    max_rounds = max_rounds or st.critic_max_rounds
    for rounds in range(1, max_rounds + 1):
        critique = (await structured("critic", "judge", CRITIC_SYSTEM,
                                     f"The founder's own writing:\n{excerpts}\nBrief: topic '{slot.topic}', angle '{slot.angle}'.\n"
                                     f"Draft:\n{draft.text}", Critique, ctx, temperature=0.1, max_tokens=400,
                                     metadata={**meta, "round": rounds, "draft": draft.text})).output
        generic = template_problems(draft.text, card)      # checked in code: the model proposes, code disposes
        if generic:
            critique.must_fix = (critique.must_fix + [f"Remove the generic LinkedIn pattern ({', '.join(generic)}): this "
                                                      f"founder's own posts never do this."])[:6]
        ctx.event("critic", "critic_round", slot=slot.id, round=rounds, score=critique.overall, must_fix=critique.must_fix,
                  template=generic)
        if (critique.overall >= st.critic_pass_score and not generic) or rounds == max_rounds:
            break
        draft = (await structured("writer", "big", WRITER_SYSTEM,
                                  f"{brief}\n\nYour previous draft:\n{draft.text}\n\nEditor's required fixes:\n- " +
                                  "\n- ".join(critique.must_fix or [critique.verdict]), PostDraft, ctx, temperature=0.6,
                                  max_tokens=1000,
                                  metadata={**meta, "round": rounds, "must_fix": critique.must_fix})).output

    report = check_post(draft.text, draft.hashtags, policy, draft.claims)
    if generic := template_problems(draft.text, card):
        report.warnings.append(Violation(rule="generic_template", severity="low",
                                         detail=", ".join(generic) + " (not in the founder's own posts)"))
    for fix_round in range(2):                      # up to two rewrites; critical violations are never auto-fixed
        if report.passed or report.critical:
            break
        ctx.event("output_guard", "auto_fix", slot=slot.id, attempt=fix_round + 1, violations=[v.rule for v in report.violations])
        draft = (await structured("writer", "big", WRITER_SYSTEM,
                                  f"{brief}\n\nYour draft:\n{draft.text}\n\nIt failed these policy checks. Rewrite to fix all of "
                                  f"them. If a figure cannot be tied to a source quote, remove the figure and the sentence around it; "
                                  f"do not invent anecdotes. Fix the claims list too:\n{report.instructions()}",
                                  PostDraft, ctx, temperature=0.4, max_tokens=1000,
                                  metadata={**meta, "round": 99, "violations": [v.model_dump() for v in report.violations]})).output
        report = check_post(draft.text, draft.hashtags, policy, draft.claims)
    return Composed(draft, critique, rounds, report)


async def save_post(tenant_id: str, slot_id: str, c: Composed, ctx: AgentContext, final: bool) -> Post:
    """Persist an attempt. `final` links it to the slot; blocked intermediate attempts are stored for the audit trail."""
    bar = get_settings().critic_pass_score
    report = c.report
    if report.passed and c.score < bar:       # the founder sees it, clearly labelled; it does not count toward the target
        report = GuardReport(passed=True, violations=[], warnings=report.warnings + [Violation(
            rule="below_quality_bar", severity="medium",
            detail=f"the editor scored this {c.score:.1f}/5, below the {bar:.1f} bar, after every redraft attempt")])
    slot = await _slot(tenant_id, slot_id)
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            post = Post(slot_id=slot.id, run_id=ctx.call.run_id, pillar=slot.pillar, text=c.draft.text,
                        hashtags=c.draft.hashtags[:5], sources=[u for u in c.draft.sources_used if u][:6],
                        claims=[x.model_dump() for x in c.draft.claims],
                        rationale=c.draft.rationale, critic_score=c.score,
                        critic_feedback=c.critique.model_dump() if c.critique else {}, rounds=c.rounds,
                        guardrail_report=report.model_dump(), status="drafting")
            s.add(post)
            await s.flush()
            transition(post, "pending_approval" if report.passed else "blocked", POST_TRANSITIONS)
            for v in report.violations:
                incident(s, "output", v.rule, v.severity, v.detail, "post", post.id, run_id=ctx.call.run_id)
            if final:
                db_slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot.id))).scalars().one()
                db_slot.status, db_slot.post_id = "drafted", post.id
            audit(s, "system:writer", "post.drafted", "post", post.id, status=post.status, score=post.critic_score,
                  final=final)
    return post


async def _mark_slot_blocked(tenant_id: str, slot_id: str, ctx: AgentContext) -> Post:
    """Every attempt was blocked: point the slot at the last blocked attempt so ops can see why."""
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            post = (await s.execute(select(Post).where(Post.slot_id == slot_id, Post.status == "blocked")
                                    .order_by(Post.created_at.desc()))).scalars().first()
            db_slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot_id))).scalars().one()
            db_slot.status, db_slot.post_id = "drafted", post.id
    return post


async def reflect_on_feedback(tenant_id: str, ctx: AgentContext) -> int:
    """Turn founder edits/rejections into durable voice rules (new voice_profile version)."""
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            fb = list((await s.execute(select(Feedback).where(Feedback.consumed.is_(False))
                                       .order_by(Feedback.created_at).limit(20))).scalars().all())
    useful = [f for f in fb if f.action in ("edited", "changes_requested", "rejected")]
    voice = await latest(tenant_id, "voice_profile")
    if useful and voice:
        pairs = "\n\n".join(f"ACTION: {f.action}\nNOTE: {f.note}\nDRAFT:\n{f.original[:900]}\nFOUNDER'S VERSION:\n{f.revised[:900]}"
                            for f in useful)
        rules = (await structured("voice_reflector", "small",
                                  "Compare drafts with what the founder changed or said. Infer concrete writing rules for "
                                  "future drafts (e.g. 'never open with a question'). Ignore one-off factual fixes.",
                                  f"Current rules: {voice.get('learned_rules', [])}\n\n{pairs}", LearnedRules, ctx, max_tokens=500,
                                  metadata={"feedback": [{"action": f.action, "note": f.note, "original": f.original,
                                                          "revised": f.revised} for f in useful]})).output
        merged = list(dict.fromkeys(voice.get("learned_rules", []) + rules.rules))[:15]
        await put_artifact(tenant_id, "voice_profile", VoiceProfile(**{**voice, "learned_rules": merged}).model_dump(),
                           ctx.call.run_id)
    if fb:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                for f in (await s.execute(select(Feedback).where(Feedback.id.in_([x.id for x in fb])))).scalars():
                    f.consumed = True
    return len(useful)


def edit_distance_ratio(a: str, b: str) -> float:
    """0 = unchanged, 1 = fully rewritten (word-level)."""
    from difflib import SequenceMatcher
    if a == b:
        return 0.0
    return round(1 - SequenceMatcher(None, a.split(), b.split()).ratio(), 3)


def now() -> datetime:
    return datetime.now(timezone.utc)
