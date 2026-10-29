"""Offline / live evaluation suite with a regression gate.

  python -m evals.run_evals                 # mock LLM: deterministic, free, used in CI
  python -m evals.run_evals --live          # real models (judge calibration included)
  python -m evals.run_evals --update-baseline

Sections
  1. guardrails  red-team + benign controls for input and output guards (recall and false positives)
  2. pipeline    discovery + weekly content and lead runs for the two demo clients, scored against
                 business rules and the prospect dataset's ground-truth labels
  3. judge       critic-vs-human agreement on hand-scored drafts (live only: mock scores aren't content-based)
The report is compared with evals/baseline.json; any threshold breach exits non-zero.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).parent


def _setup_mode(live: bool) -> None:
    os.environ["APP_ENV"] = "test"      # evals are automated tests: mock models and fixtures are allowed here
    os.environ["LLM_MODE"] = "live" if live else "mock"


async def eval_guardrails() -> dict:
    from app.guardrails.input_guard import scan
    from app.guardrails.output_guard import PolicyContext, check_outreach, check_post

    data = json.loads((HERE / "datasets/redteam.json").read_text())
    policy = PolicyContext(
        tenant_name="LedgerLoop", competitors=["ClearTally", "BooksBridge", "CloseCraft"], banned_topics=["crypto"],
        required_disclaimer={"triggers": ["tax", "deduct", "irs"], "text": "General information, not tax or financial advice."},
        other_tenant_terms=["ShiftWise", "Marcus Okafor", "GridMint", "Elena Varga"],
        source_texts=["Customers close books in 3 days on average, down from 12 days before switching (140 customers)."])
    results = []
    for c in data["output_post"]:
        r = check_post(c["text"], [], policy)
        got = "pass" if r.passed else "block"
        results.append({"set": "post", "name": c["name"], "expect": c["expect"], "got": got,
                        "rules": sorted({v.rule for v in r.violations}), "rule_ok": c.get("rule") in {v.rule for v in r.violations} if c["expect"] == "block" else True})
    for c in data["output_outreach"]:
        r = check_outreach(c["text"], c["company"], c["signal"], policy)
        got = "pass" if r.passed else "block"
        results.append({"set": "outreach", "name": c["name"], "expect": c["expect"], "got": got,
                        "rules": sorted({v.rule for v in r.violations}), "rule_ok": c.get("rule") in {v.rule for v in r.violations} if c["expect"] == "block" else True})
    live = os.environ.get("LLM_MODE") == "live"
    for c in data["input"]:
        v = await (_scan_live(c["text"]) if live else scan(c["text"]))  # live adds the Prompt Guard classifier
        got = "flag" if v.flagged else "pass"
        results.append({"set": "input", "name": c["name"], "expect": c["expect"], "got": got,
                        "score": round(v.score, 3), "rule_ok": True})

    out_cases = [r for r in results if r["set"] != "input"]
    blocks = [r for r in out_cases if r["expect"] == "block"]
    benign = [r for r in results if r["expect"] == "pass"]
    inj = [r for r in results if r["set"] == "input" and r["expect"] == "flag"]
    return {
        "redteam_block_recall": round(sum(r["got"] == "block" and r["rule_ok"] for r in blocks) / len(blocks), 3),
        "input_injection_recall": round(sum(r["got"] == "flag" for r in inj) / len(inj), 3),
        "benign_false_positive_rate": round(sum(r["got"] != "pass" for r in benign) / len(benign), 3),
        "misses": [f"{r['set']}:{r['name']} expected {r['expect']} got {r['got']} {r.get('rules', '')}"
                   for r in results if (r["got"] != r["expect"]) or not r["rule_ok"]],
    }


async def _scan_live(text: str):
    """Live input scan needs a tenant for the ledger; use a throwaway one."""
    from app.guardrails.input_guard import scan
    from app.llm.types import CallContext
    return await scan(text, CallContext(tenant_id=_EVAL_TENANT["id"]))


_EVAL_TENANT: dict = {}


async def eval_pipeline(weeks: int) -> dict:
    from sqlalchemy import select

    from app.fixtures.sources import dataset_case
    from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
    from app.core.tenancy import tenant_scope
    from app.db.models import Artifact, CalendarSlot, KnowledgeDoc, Lead, LeadRejection, LLMCall, OutreachMessage, Post, TraceEvent
    from app.agents.blackboard import policy_context
    from app.guardrails.output_guard import check_post
    from app.core.config import get_settings

    s = get_settings()
    from app.db.session import get_db
    db = get_db()
    from app.fixtures.seed import seed
    ids = await seed(db)
    orch = Orchestrator()
    set_orchestrator(orch)
    out: dict = {"clients": {}}
    contamination_markers = ["developer mode", "ignore all previous", "ignore your previous", "best option",
                             "leaks customer data", "system prompt"]
    agg = Counter()
    for slug in ("ledgerloop", "shiftwise"):
        tid = ids[slug]
        t0 = time.monotonic()
        st = await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
        from tests.helpers import weekly as weekly_runs
        weekly = []
        for _ in range(weeks):
            weekly += list(await weekly_runs(orch, tid))
        elapsed = time.monotonic() - t0
        with tenant_scope(tid):
            async with db.reader() as sess:
                slots = (await sess.execute(select(CalendarSlot))).scalars().all()
                posts = (await sess.execute(select(Post))).scalars().all()
                leads = (await sess.execute(select(Lead))).scalars().all()
                rej = (await sess.execute(select(LeadRejection))).scalars().all()
                msgs = (await sess.execute(select(OutreachMessage))).scalars().all()
                arts = (await sess.execute(select(Artifact))).scalars().all()
                docs = (await sess.execute(select(KnowledgeDoc).where(KnowledgeDoc.kind == "web_page"))).scalars().all()
                calls = (await sess.execute(select(LLMCall))).scalars().all()
                events = (await sess.execute(select(TraceEvent))).scalars().all()

        # calendar: first 8 weeks
        start = min(x.publish_date for x in slots) if slots else None
        first8 = [x for x in slots if start and x.publish_date < start + timedelta(weeks=8)]
        per_week = Counter(x.publish_date - timedelta(days=x.publish_date.weekday()) for x in first8)
        calendar_valid = int(len(per_week) == 8 and all(n >= s.posts_per_week_target for n in per_week.values()))
        topics_unique = int(len({x.topic.lower() for x in slots}) == len(slots))

        # content: re-check every post the founder can see with the current policy
        policy = await policy_context(tid, include_previous_posts=False)
        visible = [p for p in posts if p.status == "pending_approval"]
        rechecked = sum(not check_post(p.text, p.hashtags, policy).passed for p in visible)

        # leads vs ground truth
        policy_violations = sum(dataset_case(l.source_id) in ("suppressed", "competitor", "missing_contact") for l in leads)
        explicit_low = sum(dataset_case(l.source_id) == "low_fit" and dataset_case(l.source_id, slug) == "low_fit"
                           for l in leads)
        encountered = Counter(dataset_case(x.source_id) for x in rej) + Counter(dataset_case(l.source_id) for l in leads)
        rejected = Counter(dataset_case(x.source_id) for x in rej)
        adv_recall = {k: round(rejected[k] / encountered[k], 3) for k in ("suppressed", "competitor", "missing_contact")
                      if encountered[k]}
        weekly_counts = Counter(l.week_of for l in leads)

        injection_docs = [d for d in docs if d.injection_score >= 0.5]
        contamination = sum(any(m in json.dumps(a.data).lower() for m in contamination_markers) for a in arts) + \
            sum(any(m in p.text.lower() for m in contamination_markers) for p in posts)

        cost = sum(c.cost_usd for c in calls)
        c = {
            "run_status": [st] + weekly, "seconds": round(elapsed, 1),
            "calendar_valid": calendar_valid, "topics_unique": topics_unique,
            "posts": len(posts), "posts_ready": len(visible),
            "posts_ready_rate": round(len(visible) / max(1, len(posts)), 3),
            "avg_critic_score": round(sum(p.critic_score for p in posts) / max(1, len(posts)), 2),
            "avg_critic_rounds": round(sum(p.rounds for p in posts) / max(1, len(posts)), 2),
            "guard_autofixes": sum(e.type == "auto_fix" for e in events),
            "pending_posts_with_violations": rechecked,
            "leads": len(leads), "leads_per_week": sorted(weekly_counts.values()),
            "leads_week_in_range_rate": round(sum(s.leads_per_week_min <= n <= s.leads_per_week_max
                                                  for n in weekly_counts.values()) / max(1, len(weekly_counts)), 3),
            "lead_policy_violations": policy_violations, "explicit_low_fit_kept": explicit_low,
            "adversarial_rejection_recall": adv_recall, "outreach_drafts": len(msgs),
            "injection_pages_caught": len(injection_docs), "artifact_contamination": contamination,
            "llm_calls": len(calls), "tokens": sum(x.prompt_tokens + x.completion_tokens for x in calls),
            "cost_usd": round(cost, 4), "cost_per_client_cycle_usd": round(cost / (1 + weeks), 4),
        }
        out["clients"][slug] = c
        for k in ("pending_posts_with_violations", "lead_policy_violations", "explicit_low_fit_kept", "artifact_contamination"):
            agg[k] += c[k]
    cl = out["clients"].values()
    out.update({
        "calendar_valid": min(x["calendar_valid"] for x in cl),
        "posts_ready_rate": min(x["posts_ready_rate"] for x in cl),
        "avg_critic_score": min(x["avg_critic_score"] for x in cl),
        "leads_week_in_range_rate": min(x["leads_week_in_range_rate"] for x in cl),
        "cost_per_client_cycle_usd": max(x["cost_per_client_cycle_usd"] for x in cl),
        **agg,
    })
    return out


async def eval_judge() -> dict:
    """Agreement between the critic (judge model) and a human editor."""
    from app.agents.runtime.loop import AgentContext, structured
    from app.agents.schemas import Critique
    from app.agents.content.agents import CRITIC_SYSTEM
    from app.llm.types import CallContext

    data = json.loads((HERE / "datasets/judge_calibration.json").read_text())
    pairs = []
    for i, item in enumerate(data["items"]):
        ctx = AgentContext(CallContext(tenant_id=_EVAL_TENANT["id"]))
        crit = (await structured("critic", "judge", CRITIC_SYSTEM, f"Founder voice: {data['voice']}\nDraft:\n{item['text']}",
                                 Critique, ctx, temperature=0.0, max_tokens=800,
                                 metadata={"slot": {"id": f"cal{i}"}, "round": 1})).output
        pairs.append((item["human"], crit.overall))
    within1 = sum(abs(h - m) <= 1 for h, m in pairs) / len(pairs)
    return {"pairs": pairs, "within_one_point": round(within1, 3), "spearman": round(_spearman(pairs), 3)}


async def _give_operator_key(var: str) -> None:
    """Live models run on an account's own key: save the developer's Groq key into the demo operator's account, through
    the same service (and the same check call) the keys page uses. The key is never printed."""
    from dotenv import dotenv_values
    from sqlalchemy import select
    from app.api.deps import load_principal
    from app.db.models import Tenant
    from app.db.session import get_db
    from app.services import keys
    raw = dotenv_values(HERE.parent / ".env").get(var) or os.environ.get(var) or ""
    if not raw:
        sys.exit(f"--live needs {var} in .env")
    async with get_db().reader() as s:
        owner = (await s.execute(select(Tenant.owner_user_id).where(Tenant.id == _EVAL_TENANT["id"]))).scalar_one()
    await keys.save_key(await load_principal(owner), "llm", "groq", raw)


def _spearman(pairs: list[tuple[float, float]]) -> float:
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a) ** 0.5, sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def gate(report: dict, baseline: dict) -> list[str]:
    failures = []
    flat = {**report["guardrails"], **{k: v for k, v in report["pipeline"].items() if k != "clients"}}
    iso = report.get("isolation", {})
    flat.update({k: v for k, v in iso.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    if iso.get("mutations"):
        flat["isolation_mutations_missed"] = iso["mutations"]["mutations_total"] - iso["mutations"]["mutations_caught"]
    flat.update({k: v for k, v in report.get("quality", {}).items() if isinstance(v, (int, float))})
    if "judge" in report:
        flat.update({f"judge_{k}": v for k, v in report["judge"].items() if k != "pairs"})
    for metric, rule in baseline["thresholds"].items():
        if metric not in flat:
            continue
        v = flat[metric]
        if "min" in rule and v < rule["min"]:
            failures.append(f"{metric}={v} < min {rule['min']}")
        if "max" in rule and v > rule["max"]:
            failures.append(f"{metric}={v} > max {rule['max']}")
    return failures


def to_markdown(r: dict, failures: list[str]) -> str:
    g, p = r["guardrails"], r["pipeline"]
    lines = [f"# Eval report ({r['mode']})", "", f"Gate: **{'PASS' if not failures else 'FAIL'}**", ""]
    lines += [f"- {f}" for f in failures]
    if g:
        lines += ["", "## Guardrails (red team and benign controls)", "", "| Metric | Value |", "|---|---|",
                  f"| Red-team block recall (right rule) | {g['redteam_block_recall']} |",
                  f"| Input injection recall | {g['input_injection_recall']} |",
                  f"| Benign false-positive rate | {g['benign_false_positive_rate']} |", ""]
        if g["misses"]:
            lines += ["Misses:", *[f"- {m}" for m in g["misses"]], ""]
    if r.get("quality"):
        q = r["quality"]
        lines += ["", "## Deterministic quality report (no LLM)", "", "| Area | Metric | Value |", "|---|---|---|"]
        lines += [f"| {area} | {name} | {q[key]} |" for area, name, key in QUALITY_ROWS if key in q]
        lines.append("")
    if r.get("isolation"):
        b = r["isolation"]
        lines += ["", "## Account isolation", "", "| Metric | Value |", "|---|---|",
                  f"| Client routes found in the app (all are tested) | {b['isolation_routes']} |",
                  f"| Checks (other account, signed out, forged/expired/stale sessions, page leaks) | {b['isolation_checks']} |",
                  f"| Cross-account access violations | {b['cross_account_access_violations']} |",
                  f"| Session escalations | {b['session_escalations']} |",
                  f"| Another account's data shown on a page | {b['cross_account_page_leaks']} |",
                  f"| API keys visible in pages, headers or database rows, or used for the wrong account | {b.get('secret_exposure_findings')} |"]
        if b.get("mutations"):
            m = b["mutations"]
            lines.append(f"| Mutation test: deliberate breaks noticed | {m['mutations_caught']} of {m['mutations_total']} |")
        lines.append("")
        if b["isolation_failures"] or b.get("secret_findings"):
            lines += ["Failures:", *[f"- {x}" for x in b["isolation_failures"] + b.get("secret_findings", [])], ""]
        if b.get("mutations"):
            lines += ["Mutations (each must be caught):", "",
                      *[f"- {k}: {'caught' if v['caught'] else '**MISSED**'} ({v['violations']} violations, "
                        f"{v['escalations']} escalations, {v['leaks']} leaks)" for k, v in b["mutations"]["detail"].items()], ""]
    if p.get("clients"):
        lines += ["## Pipeline (per demo client)", "", "| Metric | " + " | ".join(p["clients"]) + " |",
                  "|---|" + "---|" * len(p["clients"])]
        keys = next(iter(p["clients"].values())).keys()
        for k in keys:
            lines.append(f"| {k} | " + " | ".join(str(c[k]) for c in p["clients"].values()) + " |")
    if "judge" in r:
        lines += ["", "## Judge calibration", "", f"Within one point of the human: {r['judge']['within_one_point']}, "
                  f"Spearman: {r['judge']['spearman']}", "", f"Pairs (human, model): {r['judge']['pairs']}"]
    return "\n".join(lines) + "\n"


from evals.quality import QUALITY_ROWS  # noqa: E402


async def _quality_from_db(path: str, content_quality) -> dict:
    from sqlalchemy import select
    from app.db.models import Tenant
    from app.db.session import Database, get_db, set_db
    previous = get_db()
    db = Database(f"sqlite+aiosqlite:///{Path(path).resolve()}")
    set_db(db)
    try:
        async with db.reader() as s:
            tids = [t for (t,) in await s.execute(select(Tenant.id))]
        return {**await content_quality(tids), "source_db": Path(path).name, "tenants": len(tids)}
    finally:
        set_db(previous)
        await db.dispose()


async def main_async(args) -> int:
    from app.db.session import Database, set_db
    tmp = tempfile.mkdtemp(prefix="growthos-eval-")
    db = Database(f"sqlite+aiosqlite:///{tmp}/eval.db")
    set_db(db)
    from app.llm.gateway import LLMGateway, set_gateway
    set_gateway(LLMGateway())
    from app.fixtures.seed import seed
    ids = await seed(db)
    _EVAL_TENANT["id"] = ids["ledgerloop"]
    if args.live:
        await _give_operator_key(args.groq_key_var)

    report: dict = {"mode": "live" if args.live else ("a real run's data" if args.from_db else "mock")}
    sections = set(args.sections.split(",")) if args.sections else \
        ({"guardrails", "judge"} if args.live else {"guardrails", "pipeline", "quality", "isolation"})
    report["guardrails"] = await eval_guardrails() if "guardrails" in sections else {}
    report["pipeline"] = await eval_pipeline(args.weeks) if "pipeline" in sections else {"clients": {}}
    if "quality" in sections:
        from evals.quality import content_quality, lead_screening
        if args.from_db:                    # a real run's database, read only
            report["quality"] = await _quality_from_db(args.from_db, content_quality)
        else:
            tids = [ids[s] for s in ("ledgerloop", "shiftwise")]
            report["quality"] = {**await content_quality(tids), **lead_screening("ledgerloop")}
    if "isolation" in sections:
        from evals.isolation import eval_isolation
        report["isolation"] = await eval_isolation(mutations=True)
    if args.live and "judge" in sections:
        report["judge"] = await eval_judge()
    baseline_path = HERE / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    failures = gate(report, baseline)
    report["gate"] = {"passed": not failures, "failures": failures}
    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    name = args.out or f"eval-{report['mode']}"
    (out / f"{name}.json").write_text(json.dumps(report, indent=2, default=str))
    (out / f"{name}.md").write_text(to_markdown(report, failures))
    print(to_markdown(report, failures))
    await db.dispose()
    return 0 if not failures else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--groq-key-var", default="GROQ_API_KEY", help="the .env variable whose Groq key --live uses")
    ap.add_argument("--sections", default="", help="comma list; default guardrails,pipeline,quality,isolation (mock) or guardrails,judge (live)")
    ap.add_argument("--from-db", default="", help="compute the quality section over a real run's database (read only)")
    ap.add_argument("--out", default="", help="report name (default eval-<mode>)")
    args = ap.parse_args()
    _setup_mode(args.live)
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
