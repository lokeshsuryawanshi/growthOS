"""Content engine: writer <-> critic loop, guardrail gate, and learning from founder edits.

draft_slot():
  writer drafts -> critic (judge model, different family) scores against a rubric
  -> revise with must_fix until score >= threshold or max rounds (early exit saves cost)
  -> output guard -> one automatic fix for high-severity issues (never for critical)
  -> Post row in pending_approval (founder sees it) or blocked (ops sees it).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.blackboard import compact, latest, policy_context, put_artifact, source_pack
from app.agents.runtime.loop import AgentContext, structured
from app.agents.schemas import Critique, LearnedRules, PostDraft, VoiceProfile
from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import audit, incident
from app.db.models import CalendarSlot, Feedback, Post
from app.db.session import get_db
from app.guardrails.output_guard import check_post
from app.guardrails.state import POST_TRANSITIONS, transition

WRITER_SYSTEM = (
    "You are a ghostwriter for a founder's LinkedIn. Write in their voice exactly as described. "
    "Open with a hook line, keep paragraphs short, make one specific point, end with a natural question or takeaway. "
    "Use a number only if it appears in the provided sources, and list those sources' URLs in sources_used. "
    "Every figure (a percentage, money, a multiplier, or any count above 10) needs an entry in claims: the figure exactly "
    "as written, the sentence using it, the source URL, and a verbatim quote copied from that source. Use each figure only "
    "for what the quote says it measures: a customer count is a customer count, never a market size or a problem statistic. "
    "The founder's sample posts are style references: never reuse their hooks, sentences or stories. "
    "Never invent personal anecdotes, customer stories, dates, results or quotes: write only from the provided sources, the "
    "founder's own samples and general reasoning. If the sources have no figure for a point, make the point without a number. "
    "No hashtag spam (0-3 hashtags). Never mention other companies the agency works with."
)
CRITIC_SYSTEM = (
    "You are a demanding LinkedIn editor. Score the draft 1-5 on: voice_match (does it sound like the founder's samples), "
    "specificity (concrete details, not generic advice), hook (would a busy buyer stop scrolling), insight (says something "
    "non-obvious), cta_fit. List must_fix items only for real problems. Be strict: 5 is rare."
)


async def _slot(tenant_id: str, slot_id: str) -> CalendarSlot:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot_id))).scalars().one()


async def draft_slot(tenant_id: str, slot_id: str, ctx: AgentContext, note: str = "") -> Post:
    st = get_settings()
    slot = await _slot(tenant_id, slot_id)
    voice_full = await latest(tenant_id, "voice_profile") or {}
    voice = {k: v for k, v in voice_full.items() if k != "example_hooks"}   # hooks are style evidence, never wording to reuse
    strategy = await latest(tenant_id, "content_strategy") or {}
    sources = await source_pack(tenant_id)
    pillar = next((p for p in strategy.get("pillars", []) if p["name"] == slot.pillar), {})
    policy = await policy_context(tenant_id)
    src_text = "\n".join(f"[{s['url']}] {s['title']}: {s['text'][:900]}" for s in sources[:8])
    brief = (f"Topic: {slot.topic}\nAngle: {slot.angle}\nFormat: {slot.format}\nPillar: {slot.pillar} - "
             f"{pillar.get('description', '')}\nTrend reference: {slot.trend_ref or 'none'}\n"
             f"Founder voice: {compact(voice, 1500)}\nSources you may cite:\n{src_text}")
    recent = [t.split("\n")[0] for t in policy.previous_posts[:6]]
    if recent:
        brief += "\nRecent openings (don't reuse these hooks or structures): " + " | ".join(recent)
    if note:
        brief += f"\nReviewer's note to address: {note}"
    meta = {"slot": {"id": slot.id, "topic": slot.topic, "angle": slot.angle, "pillar": slot.pillar,
                     "format": slot.format, "trend_ref": slot.trend_ref}, "voice": voice, "sources": sources[:8],
            "note": note}

    draft = (await structured("writer", "big", WRITER_SYSTEM, brief, PostDraft, ctx, temperature=0.7, max_tokens=1600,
                              metadata={**meta, "round": 0})).output
    critique: Critique | None = None
    rounds = 0
    for rounds in range(1, st.critic_max_rounds + 1):
        critique = (await structured("critic", "judge", CRITIC_SYSTEM,
                                     f"Founder voice: {compact(voice, 1200)}\nBrief: topic '{slot.topic}', angle '{slot.angle}'.\n"
                                     f"Draft:\n{draft.text}", Critique, ctx, temperature=0.1, max_tokens=800,
                                     metadata={**meta, "round": rounds, "draft": draft.text})).output
        ctx.event("critic", "critic_round", slot=slot.id, round=rounds, score=critique.overall, must_fix=critique.must_fix)
        if critique.overall >= st.critic_pass_score or rounds == st.critic_max_rounds:
            break
        draft = (await structured("writer", "big", WRITER_SYSTEM,
                                  f"{brief}\n\nYour previous draft:\n{draft.text}\n\nEditor's required fixes:\n- " +
                                  "\n- ".join(critique.must_fix or [critique.verdict]), PostDraft, ctx, temperature=0.6,
                                  max_tokens=1600,
                                  metadata={**meta, "round": rounds, "must_fix": critique.must_fix})).output

    report = check_post(draft.text, draft.hashtags, policy, draft.claims)
    for fix_round in range(2):                      # up to two rewrites; critical violations are never auto-fixed
        if report.passed or report.critical:
            break
        ctx.event("output_guard", "auto_fix", slot=slot.id, attempt=fix_round + 1, violations=[v.rule for v in report.violations])
        draft = (await structured("writer", "big", WRITER_SYSTEM,
                                  f"{brief}\n\nYour draft:\n{draft.text}\n\nIt failed these policy checks. Rewrite to fix all of "
                                  f"them. If a figure cannot be tied to a source quote, remove the figure and the sentence around it; "
                                  f"do not invent anecdotes. Fix the claims list too:\n{report.instructions()}",
                                  PostDraft, ctx, temperature=0.4, max_tokens=1600,
                                  metadata={**meta, "round": 99, "violations": [v.model_dump() for v in report.violations]})).output
        report = check_post(draft.text, draft.hashtags, policy, draft.claims)

    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            post = Post(slot_id=slot.id, run_id=ctx.call.run_id, pillar=slot.pillar, text=draft.text,
                        hashtags=draft.hashtags[:5], sources=[u for u in draft.sources_used if u][:6],
                        claims=[c.model_dump() for c in draft.claims],
                        rationale=draft.rationale, critic_score=critique.overall if critique else 0.0,
                        critic_feedback=critique.model_dump() if critique else {}, rounds=rounds,
                        guardrail_report=report.model_dump(), status="drafting")
            s.add(post)
            await s.flush()
            transition(post, "pending_approval" if report.passed else "blocked", POST_TRANSITIONS)
            for v in report.violations:
                incident(s, "output", v.rule, v.severity, v.detail, "post", post.id, run_id=ctx.call.run_id)
            db_slot = (await s.execute(select(CalendarSlot).where(CalendarSlot.id == slot.id))).scalars().one()
            db_slot.status, db_slot.post_id = "drafted", post.id
            audit(s, "system:writer", "post.drafted", "post", post.id, status=post.status, score=post.critic_score)
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
                                  f"Current rules: {voice.get('learned_rules', [])}\n\n{pairs}", LearnedRules, ctx, max_tokens=800,
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
